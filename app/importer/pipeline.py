"""Import pipeline: fetch pages, archive matched documents (PDF, HTML, images), extract menus (rule → LLM)."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import logging
import re
import threading
from zoneinfo import ZoneInfo

import httpx
from ruamel.yaml import YAML
from sqlalchemy import delete, select

from .. import db
from ..config import Settings
from ..extract.common import Context, validate
from ..extract.engine import PLUGIN_PREFIX, load_document, run_rule
from ..extract.html import page_text
from ..extract.model import Extraction, ExtractionError, StaleDocument
from ..extract.pdf import PdfDoc
from ..extract.rules import CommonRule, SingleRule
from ..llm.base import LLMError, Provider
from ..llm.service import extract_menu, suggest_rule
from ..profiles import DocumentSpec, ProfileStore
from . import fetch

log = logging.getLogger(__name__)

BLOCKED_BACKOFF = dt.timedelta(hours=24)  # pause scheduled imports after a 401/403/429
WARNING_MARK = " – warning: "  # in a processing result: the menu is used, but something looks off


class Importer:
    def __init__(self, settings: Settings, store: ProfileStore, provider: Provider | None):
        self.settings = settings
        self.store = store
        self.provider = provider
        self.tz = ZoneInfo(settings.timezone)
        self._lock = threading.Lock()  # one import at a time keeps SQLite writes simple

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def today(self) -> dt.date:
        return dt.datetime.now(self.tz).date()

    # --- sources -----------------------------------------------------------------------------------------

    def run_all(self, trigger: str = "schedule") -> None:
        for source_id in list(self.store.sources):
            self.run_source(source_id, trigger)

    def run_source(self, source_id: str, trigger: str = "schedule", force: bool = False) -> int | None:
        source = self.store.sources.get(source_id)
        if not source or not source.enabled:
            return None
        if trigger == "schedule" and self.recently_blocked(source_id):
            log.info("skipping %s: the website refused automated requests within the last %s", source_id,
                     BLOCKED_BACKOFF)
            return None
        with self._lock:
            with db.session() as s:
                run = db.ImportRun(source_id=source_id, trigger=trigger)
                s.add(run)
                s.flush()
                run_id = run.id
            lines: list[str] = []
            status = "ok"
            blocked = fetched = 0
            pages: dict[str, str] = {}
            files: dict[str, tuple[bytes, str]] = {}  # one download per run, e.g. a PDF shared by locations
            try:
                with fetch.client(self.settings.site.base_url, source.user_agent) as http:
                    for loc in source.locations:
                        location_key = f"{source.id}/{loc.id}"
                        for spec in loc.documents:
                            prefix = f"{location_key}/{spec.id}"
                            page_url = spec.page or source.index_url
                            try:
                                if page_url not in pages:
                                    pages[page_url] = fetch.fetch_text(http, page_url)
                                html = pages[page_url]
                                candidates = self._candidates(html, page_url, spec)
                                if not candidates:
                                    if spec.optional:
                                        lines.append(f"{prefix}: not published at the moment (optional)")
                                    else:
                                        lines.append(f"{prefix}: nothing matched on {page_url}")
                                        status = "warning"
                                    continue
                                fetched += 1
                                for link in candidates:
                                    msg = self._handle(http, source.id, location_key, spec, link, html, force,
                                                       files)
                                    lines.append(f"{prefix}: {link.label or link.url} → {msg}")
                                    if WARNING_MARK in msg:
                                        status = "warning"
                            except httpx.HTTPStatusError as e:
                                code = e.response.status_code
                                hint = ""
                                if code in (401, 403, 429):
                                    blocked += 1
                                    hint = (" – the website refuses automated requests; scheduled imports pause for "
                                            "24 h, upload the menu in the admin UI instead")
                                log.warning("import of %s: HTTP %s for %s", prefix, code, e.request.url)
                                lines.append(f"{prefix}: ERROR HTTP {code} for {e.request.url}{hint}")
                                status = "warning"
                            except Exception as e:  # noqa: BLE001 — one broken document must not stop the run
                                log.exception("import of %s failed", prefix)
                                lines.append(f"{prefix}: ERROR {e}")
                                status = "warning"
            except Exception as e:  # noqa: BLE001
                log.exception("import of source %s failed", source_id)
                lines.append(f"FAILED: {e}")
                status = "failed"
            if blocked and not fetched:
                status = "blocked"
            with db.session() as s:
                run = s.get(db.ImportRun, run_id)
                run.finished_at = db.utcnow()
                run.status = status
                run.log = "\n".join(lines)
            return run_id

    def recently_blocked(self, source_id: str) -> bool:
        with db.session() as s:
            last = s.scalars(select(db.ImportRun).where(db.ImportRun.source_id == source_id)
                             .order_by(db.ImportRun.started_at.desc()).limit(1)).first()
        return bool(last and last.status == "blocked" and last.started_at > db.utcnow() - BLOCKED_BACKOFF)

    def upload(self, location_key: str, doc_id: str, data: bytes, media_type: str, url: str | None = None,
               label: str | None = None) -> tuple[int, str]:
        """Admin upload of a document the importer cannot fetch itself (e.g. a website that blocks automated
        requests): saved from the browser or pasted, then processed exactly like a fetched one."""
        spec = self.store.document(location_key, doc_id)
        ref = self.store.location(location_key)
        if spec is None or ref is None or spec.kind == "static":
            raise ValueError(f"unknown document {location_key}/{doc_id}")
        if media_type == "text/html":
            text = page_text(data, spec.selector)
            if not text:
                raise ValueError("the uploaded page contains no text")
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        else:
            sha = hashlib.sha256(data).hexdigest()
        now = db.utcnow()
        with db.session() as s:
            doc = s.scalars(select(db.Document).where(
                db.Document.location_key == location_key, db.Document.doc_id == doc_id, db.Document.sha256 == sha,
            )).first()
            if doc is None:
                path = self.file_path(sha, media_type)
                if not path.exists():
                    path.write_bytes(data)
                doc = db.Document(source_id=ref.source.id, location_key=location_key, doc_id=doc_id, kind=spec.kind,
                                  url=url or spec.page or ref.source.index_url, label=label or "manual upload",
                                  sha256=sha, media_type=media_type, first_seen=now, last_seen=now)
                s.add(doc)
                s.flush()
            else:
                doc.last_seen = now
            doc_pk = doc.id
        return doc_pk, self.process_document(doc_pk)

    @staticmethod
    def _candidates(html: str, page_url: str, spec: DocumentSpec) -> list[fetch.Link]:
        """The document(s) a spec refers to on its page."""
        if spec.target == "page":
            found = [fetch.Link(page_url, fetch.page_title(html))]
        elif spec.target == "image":
            title = fetch.page_title(html)
            found = [
                fetch.Link(img.url, img.label or title)
                for img in fetch.extract_images(html, page_url, spec.selector)
                if spec.match is None or spec.match.model_copy(update={"pdf_only": False}).matches(img.label, img.url)
            ]
        else:
            found = [
                link for link in fetch.extract_links(html, page_url, spec.selector)
                if spec.match.matches(link.label, link.url)
            ]
        unique, seen = [], set()
        for link in found:
            if link.url not in seen:
                seen.add(link.url)
                unique.append(link)
        if spec.index is not None:
            unique = unique[spec.index:spec.index + 1]
        if spec.url_rewrite:
            unique = [fetch.Link(re.sub(spec.url_rewrite.pattern, spec.url_rewrite.replace, link.url), link.label)
                      for link in unique]
        return unique

    def _handle(self, http, source_id: str, location_key: str, spec: DocumentSpec, link: fetch.Link, html: str,
                force: bool, files: dict[str, tuple[bytes, str]] | None = None) -> str:
        now = db.utcnow()
        if spec.kind == "static":
            with db.session() as s:
                doc = s.scalars(select(db.Document).where(
                    db.Document.location_key == location_key,
                    db.Document.doc_id == spec.id,
                    db.Document.kind == "static",
                )).first()
                if doc is None:
                    doc = db.Document(source_id=source_id, location_key=location_key, doc_id=spec.id, kind="static",
                                      url=link.url, status="static")
                    s.add(doc)
                doc.url, doc.label, doc.last_seen = link.url, link.label, now
            return "static link"

        if spec.target == "page":
            data, media = html.encode("utf-8"), "text/html"
            # pages change constantly (nonces, cookie banners): only the visible text decides
            sha = hashlib.sha256(page_text(html, spec.selector).encode("utf-8")).hexdigest()
        else:
            if files is not None and link.url in files:
                data, media = files[link.url]
            else:
                data, media = fetch.fetch_document(http, link.url)
                if files is not None:
                    files[link.url] = (data, media)
            if media == "text/html":  # a linked web page (e.g. one per week): only the visible text decides
                sha = hashlib.sha256(page_text(data, spec.selector).encode("utf-8")).hexdigest()
            else:
                sha = hashlib.sha256(data).hexdigest()
        with db.session() as s:
            doc = s.scalars(select(db.Document).where(
                db.Document.location_key == location_key,
                db.Document.doc_id == spec.id,
                db.Document.sha256 == sha,
            )).first()
            if doc is not None:
                doc.last_seen, doc.url, doc.label = now, link.url, link.label
                if not force:
                    return f"unchanged ({doc.status})"
            else:
                path = self.file_path(sha, media)
                if not path.exists():
                    path.write_bytes(data)
                doc = db.Document(source_id=source_id, location_key=location_key, doc_id=spec.id, kind=spec.kind,
                                  url=link.url, label=link.label, sha256=sha, media_type=media, first_seen=now,
                                  last_seen=now)
                s.add(doc)
                s.flush()
            doc_pk = doc.id
        return self.process_document(doc_pk)

    # --- documents ---------------------------------------------------------------------------------------

    def file_path(self, sha: str, media_type: str | None):
        return self.settings.files_dir / f"{sha}.{fetch.EXTENSIONS.get(media_type or '', 'bin')}"

    def file_bytes(self, doc: db.Document) -> bytes:
        return self.file_path(doc.sha256, doc.media_type).read_bytes()

    def load(self, doc: db.Document) -> PdfDoc:
        spec = self.store.document(doc.location_key, doc.doc_id)
        return load_document(
            self.file_bytes(doc), doc.media_type or "application/pdf",
            spec.selector if spec else None, spec.ocr.model_dump() if spec else None,
        )

    def context(self, doc: db.Document) -> Context:
        fetched = doc.first_seen.replace(tzinfo=dt.UTC).astimezone(self.tz).date()
        return Context(fetch_date=fetched, label=doc.label, url=doc.url)

    def process_document(self, document_id: int, mode: str = "auto") -> str:
        """mode: auto (rule, then LLM fallback) | rule (no LLM) | llm (skip the rule)."""
        with db.session() as s:
            doc = s.get(db.Document, document_id)
            spec = self.store.document(doc.location_key, doc.doc_id)
            ref = self.store.location(doc.location_key)
            data = self.file_bytes(doc)
            media = doc.media_type or "application/pdf"
            ctx = self.context(doc)
            kind = doc.kind
        ex: Extraction | None = None
        origin = error = None
        rule_spec = spec.rule if spec else None
        try:
            pdfdoc = self.load(doc)
        except ExtractionError as e:  # e.g. OCR not installed — the LLM may still read the file
            pdfdoc, error = PdfDoc([]), f"load: {e}"

        if mode != "llm" and rule_spec is not None and pdfdoc.pages:
            try:
                rule = self.store.resolve_rule(rule_spec)
                ex = run_rule(rule, pdfdoc, ctx)
                origin = "plugin" if isinstance(rule, str) else "rule"
            except StaleDocument as e:
                return self._finish(document_id, None, None, "stale", str(e))
            except ExtractionError as e:
                error = f"rule: {e}"
            except Exception as e:  # noqa: BLE001 — broken plugin
                log.exception("rule failed for document %s", document_id)
                error = f"rule crashed: {e}"
        elif rule_spec is None:
            error = error or "no rule configured"

        llm_allowed = self.provider is not None and (spec is None or spec.llm)
        if ex is None and mode != "rule" and llm_allowed:
            location_name = ref.location.name if ref else doc.location_key
            attachment = (data, media) if media == "application/pdf" or media.startswith("image/") else None
            try:
                ex = extract_menu(self.provider, attachment, pdfdoc, ctx, location_name, kind)
                check = SingleRule(type="single") if kind == "special" else CommonRule(validate={"min_days": 1})
                validate(ex, check, ctx)
                origin = "llm"
            except StaleDocument as e:
                return self._finish(document_id, None, None, "stale", str(e))
            except (LLMError, ExtractionError) as e:
                error = f"{error}; llm: {e}" if error else f"llm: {e}"
                ex = None
            if ex is not None and pdfdoc.pages and not (isinstance(rule_spec, str)
                                                        and rule_spec.startswith(PLUGIN_PREFIX)):
                self._suggest(document_id, pdfdoc, ex, ctx, rule_spec, error)

        if ex is None:
            return self._finish(document_id, None, None, "failed", error)
        return self._finish(document_id, ex, origin, "ok", error)

    def suggest_for_document(self, document_id: int) -> str:
        """Admin action: ask the LLM for a rule for an already processed document."""
        if self.provider is None:
            return "no LLM configured"
        with db.session() as s:
            doc = s.get(db.Document, document_id)
            spec = self.store.document(doc.location_key, doc.doc_id)
            ref = self.store.location(doc.location_key)
            data, media, ctx, kind = self.file_bytes(doc), doc.media_type or "application/pdf", self.context(doc), doc.kind
            stored = Extraction.from_dict(json.loads(doc.extraction_json)) if doc.extraction_json else None
            origin, error = doc.origin, doc.error
        try:
            pdfdoc = self.load(doc)
        except ExtractionError as e:
            return f"cannot read document: {e}"
        expected = stored if stored and origin == "llm" else None
        if expected is None:
            attachment = (data, media) if media == "application/pdf" or media.startswith("image/") else None
            try:
                expected = extract_menu(self.provider, attachment, pdfdoc, ctx, ref.location.name if ref else "", kind)
            except LLMError as e:
                return f"LLM extraction failed: {e}"
        result = self._suggest(document_id, pdfdoc, expected, ctx, spec.rule if spec else None, error)
        return "suggestion created" if result else "no suggestion"

    def _suggest(self, document_id, pdfdoc, expected, ctx, rule_spec, error) -> bool:
        current = None
        if rule_spec is not None:
            buf = io.StringIO()
            YAML().dump({"rule": rule_spec}, buf)
            current = buf.getvalue()
        try:
            sug = suggest_rule(self.provider, pdfdoc, expected, ctx, self.settings.rules_dir, current, error)
        except Exception:  # noqa: BLE001 — suggestions are best effort
            log.exception("rule suggestion failed")
            return False
        if not sug:
            return False
        with db.session() as s:
            s.add(db.RuleSuggestion(document_id=document_id, rule_yaml=sug.rule_yaml, score=sug.score,
                                    report=sug.report, model=f"{self.provider.name}:{self.provider.model}"))
        return True

    def _finish(self, document_id: int, ex: Extraction | None, origin: str | None, status: str,
                error: str | None) -> str:
        warnings, prior_error = (ex.warnings if ex is not None else []), error
        if warnings:  # the menu is used, the document is listed for attention in the admin UI
            error = "; ".join([*([error] if error else []), *(f"warning: {w}" for w in warnings)])
        with db.session() as s:
            doc = s.get(db.Document, document_id)
            doc.processed_at = db.utcnow()
            doc.status, doc.origin, doc.error = status, origin, error
            s.execute(delete(db.MenuItem).where(db.MenuItem.document_id == doc.id))
            s.execute(delete(db.DayStatus).where(db.DayStatus.document_id == doc.id))
            if ex is None:
                doc.extraction_json = None
                doc.notes = None
                return f"{status}: {error}"
            doc.valid_from, doc.valid_to = ex.valid_from, ex.valid_to
            doc.extraction_json = json.dumps(ex.to_dict(), ensure_ascii=False)
            doc.notes = "\n".join(ex.notes) or None
            open_days = sorted(d for d, m in ex.days.items() if not m.closed)
            if not open_days and ex.valid_from and ex.valid_to:
                span = (ex.valid_to - ex.valid_from).days + 1
                open_days = [ex.valid_from + dt.timedelta(days=i) for i in range(span)
                             if (ex.valid_from + dt.timedelta(days=i)).weekday() < 5]
            self._supersede(s, doc, set(ex.days) | set(open_days))
            for date, menu in ex.days.items():
                if menu.closed:
                    s.add(db.DayStatus(document_id=doc.id, location_key=doc.location_key, date=date,
                                       closed=menu.closed))
                for pos, item in enumerate(menu.items):
                    s.add(self._row(doc, date, pos, item, weekly=item.weekly))
            for pos, item in enumerate(ex.weekly_items, start=100):
                for date in open_days:
                    s.add(self._row(doc, date, pos, item, weekly=True))
            count = sum(len(m.items) for m in ex.days.values())
            msg = f"{status} via {origin}: {count} items, {ex.valid_from}–{ex.valid_to}"
            msg += f" ({prior_error})" if prior_error else ""
            return msg + WARNING_MARK + "; ".join(warnings) if warnings else msg

    @staticmethod
    def _row(doc: db.Document, date: dt.date, pos: int, item, weekly: bool) -> db.MenuItem:
        return db.MenuItem(
            document_id=doc.id, location_key=doc.location_key, date=date, position=pos,
            category=item.category, name=item.name, description=item.description, price_cents=item.price_cents,
            allergens=",".join(item.allergens), tags=",".join(item.tags), special=item.special, weekly=weekly,
        )

    @staticmethod
    def _supersede(s, doc: db.Document, dates: set[dt.date]) -> None:
        """A newer version of the same document replaces older ones on the days it covers ("..._v2.pdf", an
        updated web page). Files that are still linked are kept: link labels are not reliable, two different
        files may cover the same week."""
        older = s.scalars(select(db.Document).where(
            db.Document.location_key == doc.location_key,
            db.Document.doc_id == doc.doc_id,
            db.Document.id != doc.id,
            db.Document.status == "ok",
            db.Document.first_seen <= doc.first_seen,
            db.Document.last_seen < doc.last_seen - dt.timedelta(minutes=10),
        )).all()
        for o in older:
            s.execute(delete(db.MenuItem).where(db.MenuItem.document_id == o.id, db.MenuItem.date.in_(dates)))
            s.execute(delete(db.DayStatus).where(db.DayStatus.document_id == o.id, db.DayStatus.date.in_(dates)))
            s.flush()
            remaining = s.scalar(select(db.MenuItem.id).where(db.MenuItem.document_id == o.id).limit(1))
            if remaining is None:
                o.status = "superseded"
