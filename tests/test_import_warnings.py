"""Import runs against a mocked website: warnings for unexpected results, silence for optional documents."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import db
from app.extract.common import Context
from app.extract.engine import run_rule
from app.extract.pdf import load_pdf
from app.importer import fetch
from app.profiles import ProfileStore, Source

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
PASSWORD = "correct horse battery"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", "test")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash(PASSWORD))
    monkeypatch.setattr(db, "utcnow", lambda: dt.datetime(2026, 11, 9, 7))  # fetch date of the fixtures
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def serve(monkeypatch, pages: dict[str, bytes], pdf: bytes | None = None) -> None:
    """Answer every request from `pages` (by URL) or with `pdf` for *.pdf URLs."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in pages:
            return httpx.Response(200, content=pages[url], headers={"content-type": "text/html; charset=utf-8"})
        if pdf is not None and request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    monkeypatch.setattr(fetch, "client", lambda *a, **k: httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("pdf, fetched, expected", [
    ("timeout_wochenkarte.pdf", dt.date(2026, 11, 2),
     [("Gebackene Hühnerleber", "Kartoffel-Vogerl-Salat | Sauce Tartare", 1290)]),
    # two dishes; the description wraps and the price is on its last line, in the bigger font of the names
    ("timeout_wochenkarte_kw46.pdf", dt.date(2026, 11, 9),
     [("¼ knuspriges Gansl", "Rotkraut | Erdäpfelknödel | Maroni | Preiselbeeren", 2990),
      ("Indisches Butter Chicken", "Basmatireis | Fladenbrot", 1390)]),
])
def test_timeout_weekly_special(pdf, fetched, expected):
    store = ProfileStore(ROOT / "profiles", ROOT / "rules")
    ex = run_rule(store.resolve_rule("timeout-wochenkarte"), load_pdf(FIX / "pdfs" / pdf), Context(fetch_date=fetched))
    assert [(i.name, i.description, i.price_cents) for i in ex.weekly_items] == expected
    assert {i.category for i in ex.weekly_items} == {"Spezialität der Woche"}
    assert ex.warnings == []
    assert [i.name for i in ex.days[ex.valid_from].items][0].endswith("suppe")  # days unaffected


def test_missing_weekly_special_is_a_warning(client, monkeypatch):
    state = client.app.state
    timeout = state.store.sources["timeout"]
    # simulate a changed layout: the weekly special is not found any more
    state.store.rules["timeout-wochenkarte"] = {**state.store.rules["timeout-wochenkarte"],
                                                "weekly_heading": "^nothing like this$"}
    serve(monkeypatch, {timeout.index_url: (FIX / "html/timeout-technologiezentrum.html").read_bytes()},
          (FIX / "pdfs/timeout_wochenkarte_kw46.pdf").read_bytes())

    run_id = state.importer.run_source("timeout", "manual")
    with db.session() as s:
        run = s.get(db.ImportRun, run_id)
        doc = s.query(db.Document).filter_by(location_key="timeout/main", doc_id="weekly").one()
        items = s.query(db.MenuItem).filter_by(document_id=doc.id).count()
    assert run.status == "warning" and "0 all-week dish(es) found, 1 expected" in run.log
    assert doc.status == "ok" and items > 0  # the menu itself is still shown
    assert doc.error.startswith("warning: 0 all-week dish(es)")

    client.post("/admin/login", data={"password": PASSWORD})
    html = client.get("/admin").text
    assert "0 all-week dish(es) found" in html  # listed for attention

    run_id = state.importer.run_source("timeout", "manual")  # unchanged file: no new warning
    with db.session() as s:
        assert s.get(db.ImportRun, run_id).status == "ok"


def test_optional_document_without_match_is_no_warning(client, monkeypatch):
    store = client.app.state.store
    for source_id, optional in [("flyer-optional", True), ("flyer-required", False)]:
        store.sources[source_id] = Source.model_validate({
            "id": source_id, "name": source_id, "index_url": f"https://example.org/{source_id}",
            "documents": [{"id": "buffet", "kind": "special", "match": {"label": "Buffet"}, "optional": optional}],
        })
    serve(monkeypatch, {f"https://example.org/{s}": b"<html><body>no flyer this week</body></html>"
                        for s in ("flyer-optional", "flyer-required")})

    importer = client.app.state.importer
    with db.session() as s:
        optional = s.get(db.ImportRun, importer.run_source("flyer-optional", "manual"))
        required = s.get(db.ImportRun, importer.run_source("flyer-required", "manual"))
    assert optional.status == "ok" and "not published at the moment (optional)" in optional.log
    assert required.status == "warning" and "nothing matched" in required.log


def test_ghk_buffets_are_optional():
    store = ProfileStore(ROOT / "profiles", ROOT / "rules")
    for key in ["ghk-burgenland/eisenstadt-mensa", "ghk-burgenland/pinkafeld-mensa"]:
        assert store.document(key, "buffet").optional
        assert not store.document(key, "weekly").optional
