"""Admin UI: status, documents, rule test bench, link discovery, YAML editing and LLM rule suggestions."""

from __future__ import annotations

import html as html_lib
import io
import json
import re
import threading
import time
from typing import Any
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from ruamel.yaml import YAML
from sqlalchemy import func, select

from .. import db
from ..extract.engine import parse_rule, run_rule
from ..extract.model import Extraction
from ..extract.html import page_text
from ..extract.pdf import layout_text
from ..importer import fetch
from ..profiles import MAIN_LOCATION
from .common import render, safe_next

router = APIRouter(prefix="/admin")
SESSION_COOKIE = "lunchtime_admin"
SESSION_MAX_AGE = 12 * 3600
MAX_UPLOAD = 25 * 1024 * 1024
_failed_logins: dict[str, list[float]] = {}


# --- auth ------------------------------------------------------------------------------------------------

def _serializer(request: Request) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(request.app.state.settings.secret_key, salt="admin")


def _authed(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        return _serializer(request).loads(token, max_age=SESSION_MAX_AGE) == "admin"
    except BadSignature:
        return False


class NotAuthenticated(Exception):
    pass


def require_admin(request: Request) -> None:
    if not _authed(request):
        raise NotAuthenticated()


def _page(request: Request, template: str, **ctx: Any) -> Response:
    require_admin(request)
    return render(request, f"admin/{template}", msg=request.query_params.get("msg"), **ctx)


def _back(target: str, msg: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in target else "?"
    return RedirectResponse(target + (f"{sep}msg={quote(msg)}" if msg else ""), status_code=303)


def _background(request: Request, fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


@router.get("/login")
def login_form(request: Request, next: str = "/admin"):
    enabled = bool(request.app.state.settings.admin_password_hash)
    return render(request, "admin/login.html", enabled=enabled, next=next, error=None)


@router.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/admin")):
    settings = request.app.state.settings
    ip = request.client.host if request.client else "?"
    now = time.time()
    attempts = [t for t in _failed_logins.get(ip, []) if now - t < 900]
    if len(attempts) >= 10:
        return render(request, "admin/login.html", status_code=429, enabled=True, next=next,
                      error="Zu viele Versuche – bitte später erneut versuchen.")
    ok = False
    if settings.admin_password_hash:
        try:
            ok = PasswordHasher().verify(settings.admin_password_hash, password)
        except (VerificationError, InvalidHashError):
            ok = False
    if not ok:
        _failed_logins[ip] = [*attempts, now]
        time.sleep(1)
        return render(request, "admin/login.html", status_code=401, enabled=bool(settings.admin_password_hash),
                      next=next, error="Falsches Passwort.")
    _failed_logins.pop(ip, None)
    response = RedirectResponse(safe_next(next, "/admin"), status_code=303)
    response.set_cookie(SESSION_COOKIE, _serializer(request).dumps("admin"), max_age=SESSION_MAX_AGE, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/admin")
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


# --- dashboard -------------------------------------------------------------------------------------------

@router.get("")
def dashboard(request: Request):
    require_admin(request)
    state = request.app.state
    with db.session() as s:
        runs = s.scalars(select(db.ImportRun).order_by(db.ImportRun.started_at.desc()).limit(15)).all()
        last_runs = {}
        for run in runs:
            last_runs.setdefault(run.source_id, run)
        attention = s.scalars(
            select(db.Document)
            .where(db.Document.status.in_(["failed", "pending"]) | (db.Document.origin == "llm"))
            .order_by(db.Document.last_seen.desc()).limit(30)
        ).all()
        counts = dict(s.execute(select(db.Document.status, func.count()).group_by(db.Document.status)).all())
        open_suggestions = s.scalar(select(func.count()).where(db.RuleSuggestion.status == "open"))
    scheduler = state.scheduler
    return _page(
        request, "dashboard.html",
        sources=list(state.store.sources.values()), last_runs=last_runs, runs=runs, attention=attention,
        counts=counts, open_suggestions=open_suggestions, profile_errors=state.store.errors,
        profile_warnings=state.store.warnings,
        next_runs=scheduler.next_runs() if scheduler else {}, busy=state.importer.busy,
        llm=state.importer.provider, scheduler_enabled=scheduler is not None,
    )


@router.post("/import")
def run_import(request: Request, source: str = Form(...), force: bool = Form(False)):
    require_admin(request)
    importer = request.app.state.importer
    if importer.busy:
        return _back("/admin", "Es läuft bereits ein Import.")
    _background(request, importer.run_source, source, "manual", force)
    return _back("/admin", f"Import von {source} gestartet – Seite in ein paar Sekunden neu laden.")


@router.post("/reload")
def reload_profiles(request: Request):
    require_admin(request)
    _reload(request)
    return _back("/admin", "Profile neu geladen.")


def _reload(request: Request) -> None:
    request.app.state.store.reload()
    if request.app.state.scheduler:
        request.app.state.scheduler.reschedule()


# --- documents -------------------------------------------------------------------------------------------

@router.get("/documents")
def documents(request: Request, status: str = "", location: str = ""):
    require_admin(request)
    with db.session() as s:
        q = select(db.Document).order_by(db.Document.last_seen.desc())
        if status:
            q = q.where(db.Document.status == status)
        if location:
            q = q.where(db.Document.location_key == location)
        docs = s.scalars(q.limit(300)).all()
    return _page(request, "documents.html", docs=docs, status=status, location=location)


def _load_doc(doc_id: int) -> db.Document:
    with db.session() as s:
        doc = s.get(db.Document, doc_id)
        if doc is None:
            raise KeyError(doc_id)
        return doc


@router.get("/documents/{doc_id}")
def document(request: Request, doc_id: int):
    require_admin(request)
    state = request.app.state
    doc = _load_doc(doc_id)
    with db.session() as s:
        suggestions = s.scalars(select(db.RuleSuggestion).where(db.RuleSuggestion.document_id == doc_id)
                                .order_by(db.RuleSuggestion.created_at.desc())).all()
    ex = Extraction.from_dict(json.loads(doc.extraction_json)) if doc.extraction_json else None
    spec = state.store.document(doc.location_key, doc.doc_id)
    layout = None
    if doc.sha256:
        try:
            layout = layout_text(state.importer.load(doc))
        except Exception as e:  # noqa: BLE001
            layout = f"(cannot read PDF: {e})"
    return _page(request, "document.html", doc=doc, ex=ex, spec=spec, layout=layout, suggestions=suggestions,
                 llm=state.importer.provider, busy=state.importer.busy)


@router.get("/documents/{doc_id}/pdf")
def document_pdf(request: Request, doc_id: int):
    require_admin(request)
    doc = _load_doc(doc_id)
    importer = request.app.state.importer
    path = importer.file_path(doc.sha256, doc.media_type)
    media = doc.media_type or "application/pdf"
    if media == "text/html":
        media = "text/plain; charset=utf-8"  # never render archived third-party HTML on our origin
    return FileResponse(path, media_type=media, filename=f"{doc.location_key.replace('/', '_')}.{path.suffix[1:]}",
                        content_disposition_type="inline")


@router.post("/documents/{doc_id}/process")
def process(request: Request, doc_id: int, mode: str = Form("auto")):
    require_admin(request)
    importer = request.app.state.importer
    if mode == "suggest":
        _background(request, importer.suggest_for_document, doc_id)
        return _back(f"/admin/documents/{doc_id}", "Regelvorschlag wird erstellt – Seite in ca. einer Minute neu laden.")
    if mode == "llm":
        _background(request, importer.process_document, doc_id, "llm")
        return _back(f"/admin/documents/{doc_id}", "KI-Extraktion gestartet – Seite gleich neu laden.")
    result = importer.process_document(doc_id, mode)
    return _back(f"/admin/documents/{doc_id}", result)


# --- manual upload ---------------------------------------------------------------------------------------

def _upload_targets(request: Request) -> list[tuple[str, str, str]]:
    """(value, label, page url) of every non-static document spec."""
    out = []
    for ref in request.app.state.store.locations():
        for spec in ref.location.documents:
            if spec.kind != "static":
                out.append((f"{ref.key}|{spec.id}", f"{ref.location.name} – {spec.id}", spec.page or ref.source.index_url))
    return sorted(out, key=lambda t: t[1].lower())


@router.get("/upload")
def upload_form(request: Request, target: str = ""):
    require_admin(request)
    return _page(request, "upload.html", targets=_upload_targets(request), target=target, error=None)


@router.post("/upload")
async def upload(request: Request, target: str = Form(...), url: str = Form(""), pasted: str = Form(""),
                 file: UploadFile | None = File(None)):
    require_admin(request)
    importer = request.app.state.importer
    location_key, _, doc_id = target.partition("|")
    data, label = b"", "manual upload"
    if file is not None and file.filename:
        data = await file.read(MAX_UPLOAD + 1)
        label = f"upload: {file.filename}"
    elif pasted.strip():
        data, label = _pasted_html(pasted).encode("utf-8"), "upload: pasted text"
    error = None
    if not data:
        error = "Bitte eine Datei wählen oder den Seiteninhalt einfügen."
    elif len(data) > MAX_UPLOAD:
        error = "Die Datei ist zu groß (max. 25 MB)."
    else:
        media = fetch.sniff_media_type(data)
        if media not in fetch.EXTENSIONS:
            error = f"Nicht unterstütztes Format ({media}) – PDF, HTML, PNG, JPEG, GIF oder WebP."
    if error:
        return _page(request, "upload.html", targets=_upload_targets(request), target=target, error=error)
    try:
        doc_id_pk, result = importer.upload(location_key, doc_id, data, media, url or None, label)
    except ValueError as e:
        return _page(request, "upload.html", targets=_upload_targets(request), target=target, error=str(e))
    return _back(f"/admin/documents/{doc_id_pk}", f"Hochgeladen – {result}")


def _pasted_html(text: str) -> str:
    """Pasted page source stays HTML; pasted visible text becomes one paragraph per line."""
    if re.search(r"(?is)<(html|body|table|div|p)\b", text):
        return text
    lines = [html_lib.escape(line.strip()) for line in text.splitlines()]
    return "<html><body>" + "".join(f"<p>{line}</p>" for line in lines if line) + "</body></html>"


# --- rule test bench -------------------------------------------------------------------------------------

def _rule_yaml(spec: Any) -> str:
    buf = io.StringIO()
    YAML().dump(spec, buf)
    return buf.getvalue()


@router.get("/testbench")
def testbench(request: Request, document: int | None = None):
    require_admin(request)
    state = request.app.state
    rule_text, doc = "", None
    if document:
        doc = _load_doc(document)
        spec = state.store.document(doc.location_key, doc.doc_id)
        if spec and isinstance(spec.rule, str) and spec.rule in state.store.rules:
            rule_text = _rule_yaml({k: v for k, v in state.store.rules[spec.rule].items() if k != "id"})
        elif spec and isinstance(spec.rule, dict):
            rule_text = _rule_yaml(spec.rule)
    return _page(request, "testbench.html", docs=_recent_docs(), document=doc, rule_text=rule_text or "type: grid\n",
                 result=None, error=None)


def _recent_docs() -> list[db.Document]:
    with db.session() as s:
        return s.scalars(select(db.Document).where(db.Document.sha256.is_not(None))
                         .order_by(db.Document.last_seen.desc()).limit(200)).all()


@router.post("/testbench")
def testbench_run(request: Request, document: int = Form(...), rule_text: str = Form(...)):
    require_admin(request)
    state = request.app.state
    doc = _load_doc(document)
    result = error = None
    try:
        spec = YAML(typ="safe").load(rule_text) or {}
        base = spec.pop("extends", None) if isinstance(spec, dict) else None
        if base:
            spec = {**state.store.rules[base], **spec}
        spec.pop("id", None)
        result = run_rule(parse_rule(spec), state.importer.load(doc), state.importer.context(doc))
    except Exception as e:  # noqa: BLE001 — show any error to the admin
        error = f"{type(e).__name__}: {e}"
    return _page(request, "testbench.html", docs=_recent_docs(), document=doc, rule_text=rule_text, result=result,
                 error=error)


@router.post("/testbench/save")
def testbench_save(request: Request, document: int = Form(...), rule_text: str = Form(...), rule_id: str = Form(...),
                   assign: bool = Form(False)):
    require_admin(request)
    state = request.app.state
    doc = _load_doc(document)
    try:
        spec = YAML(typ="safe").load(rule_text) or {}
        spec.pop("id", None)
        name = state.store.save_rule(rule_id, spec, header=f"Created in the admin test bench for {doc.location_key}")
        if assign:
            source_id, loc_id = doc.location_key.split("/", 1)
            state.store.set_document_rule(source_id, loc_id, doc.doc_id, rule_id)
        _reload(request)
    except Exception as e:  # noqa: BLE001
        return _back(f"/admin/testbench?document={document}", f"Speichern fehlgeschlagen: {e}")
    if assign:
        state.importer.process_document(document, "rule")
    return _back(f"/admin/documents/{document}", f"{name} gespeichert" + (" und zugewiesen" if assign else ""))


# --- discover --------------------------------------------------------------------------------------------

@router.get("/discover")
def discover(request: Request, url: str = "", all: bool = False, selector: str = ""):
    require_admin(request)
    links, images, error, matches, text = [], [], None, {}, ""
    if url:
        try:
            with fetch.client(request.app.state.settings.site.base_url) as http:
                html = fetch.fetch_text(http, url)
            links = fetch.extract_links(html, url, selector or None)
            images = fetch.extract_images(html, url, selector or None)
            text = page_text(html, selector or None)[:3000]
        except Exception as e:  # noqa: BLE001
            error = str(e)
        for source in request.app.state.store.sources.values():
            if source.index_url.rstrip("/") == url.rstrip("/"):
                for (loc_id, doc_id), matched in fetch.match_source(links, source).items():
                    for link in matched:
                        matches.setdefault(link.url, []).append(f"{loc_id}/{doc_id}")
        if not all:
            links = [l for l in links if l.is_pdf]
    rows = [(l, fetch.suggest_label_pattern(l.label), matches.get(l.url, [])) for l in links]
    return _page(request, "discover.html", url=url, rows=rows, images=images, text=text, error=error, show_all=all,
                 selector=selector)


# --- files -----------------------------------------------------------------------------------------------

@router.get("/files")
def files(request: Request):
    require_admin(request)
    store = request.app.state.store
    groups: dict[str, list[tuple[str, str]]] = {}
    for name in store.files():
        folder, _, filename = name.rpartition("/")
        groups.setdefault(folder, []).append((name, filename))
    return _page(request, "files.html", groups=groups, errors=store.errors, warnings=store.warnings)


@router.get("/files/edit")
def file_edit(request: Request, name: str):
    require_admin(request)
    store = request.app.state.store
    path = store.path_for(name)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return _page(request, "file_edit.html", name=name, text=text, error=store.errors.get(name), exists=path.exists(),
                 warning=store.warnings.get(name))


@router.post("/files/edit")
def file_save(request: Request, name: str = Form(...), text: str = Form(...)):
    require_admin(request)
    store = request.app.state.store
    try:
        store.write_file(name, text.replace("\r\n", "\n"))
        _reload(request)
    except Exception as e:  # noqa: BLE001
        return _page(request, "file_edit.html", name=name, text=text, error=str(e), exists=True)
    return _back(f"/admin/files/edit?name={name}", "Gespeichert – nicht vergessen, die Änderung zu committen.")


@router.post("/files/delete")
def file_delete(request: Request, name: str = Form(...)):
    require_admin(request)
    request.app.state.store.delete_file(name)
    _reload(request)
    return _back("/admin/files", f"{name} gelöscht.")


# --- suggestions -----------------------------------------------------------------------------------------

@router.get("/suggestions")
def suggestions(request: Request, status: str = "open"):
    require_admin(request)
    with db.session() as s:
        rows = s.scalars(select(db.RuleSuggestion).where(db.RuleSuggestion.status == status)
                         .order_by(db.RuleSuggestion.created_at.desc()).limit(100)).all()
        docs = {r.document_id: s.get(db.Document, r.document_id) for r in rows}
    return _page(request, "suggestions.html", rows=rows, docs=docs, status=status)


@router.post("/suggestions/{sid}")
def suggestion_action(request: Request, sid: int, action: str = Form(...), rule_id: str = Form("")):
    require_admin(request)
    state = request.app.state
    with db.session() as s:
        sug = s.get(db.RuleSuggestion, sid)
        doc = s.get(db.Document, sug.document_id)
        if action == "dismiss":
            sug.status = "dismissed"
            return _back("/admin/suggestions", "Vorschlag verworfen.")
        rule_yaml, location_key, doc_id, document_id = sug.rule_yaml, doc.location_key, doc.doc_id, doc.id
    if action == "test":
        return _page(request, "testbench.html", docs=_recent_docs(), document=_load_doc(document_id),
                     rule_text=rule_yaml, result=None, error=None)
    try:
        spec = YAML(typ="safe").load(rule_yaml) or {}
        spec.pop("id", None)
        source_id, loc_id = location_key.split("/", 1)
        rule_id = rule_id.strip() or f"{source_id if loc_id == MAIN_LOCATION else loc_id}-{doc_id}"
        state.store.save_rule(rule_id, spec, header=f"Suggested by the LLM for {location_key}, accepted in the admin UI")
        state.store.set_document_rule(source_id, loc_id, doc_id, rule_id)
        _reload(request)
    except Exception as e:  # noqa: BLE001
        return _back("/admin/suggestions", f"Übernehmen fehlgeschlagen: {e}")
    with db.session() as s:
        s.get(db.RuleSuggestion, sid).status = "accepted"
    result = state.importer.process_document(document_id, "rule")
    return _back(f"/admin/documents/{document_id}",
                 f"Regel rules/{rule_id}.yml gespeichert und zugewiesen – {result}. Bitte committen.")
