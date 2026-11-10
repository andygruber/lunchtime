"""Manual uploads (for websites that block automated requests) and the back-off after a block."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app import db
from app.web.admin import _pasted_html

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", "test")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash("correct horse battery"))
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def visible_text(path: str) -> str:
    """What an admin gets when selecting the page in the browser and copying it."""
    soup = BeautifulSoup((FIX / path).read_text(encoding="utf-8"), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def test_pasted_text_becomes_paragraphs():
    html = _pasted_html("MONTAG\nSuppe <scharf>\n\nGulasch")
    assert html.count("<p>") == 3 and "&lt;scharf&gt;" in html
    assert _pasted_html("<html><body><p>x</p></body></html>").startswith("<html>")


def test_upload_pasted_text(client):
    importer = client.app.state.importer
    data = _pasted_html(visible_text("html/diealm-mittagsteller.html")).encode()
    doc_id, result = importer.upload("diealm/main", "weekly", data, "text/html")
    assert result.startswith("ok via rule"), result
    with db.session() as s:
        doc = s.get(db.Document, doc_id)
        assert (doc.valid_from, doc.valid_to) == (dt.date(2026, 11, 2), dt.date(2026, 11, 13))
        assert doc.label == "manual upload" and doc.url == "https://www.diealm.at/mittagsteller/"
    # the same content again is recognised, not stored twice
    assert importer.upload("diealm/main", "weekly", data, "text/html")[0] == doc_id


def test_upload_saved_page_with_tables(client):
    importer = client.app.state.importer
    data = (FIX / "html/bsh-speiseplan.html").read_bytes()
    _, result = importer.upload("bsh-eisenstadt/main", "weekly", data, "text/html")
    assert result.startswith("ok via rule"), result


def test_upload_rejects_static_documents(client):
    with pytest.raises(ValueError):
        client.app.state.importer.upload("timeout/main", "a-la-carte", b"<p>x</p>", "text/html")


def test_admin_upload_form(client):
    assert client.get("/admin/upload", follow_redirects=False).status_code == 303  # login required
    client.post("/admin/login", data={"password": "correct horse battery", "next": "/admin"})
    assert "Schlosscafe – weekly" in client.get("/admin/upload").text
    text = visible_text("html/schlosscafe-speisekarte.html")
    r = client.post("/admin/upload", data={"target": "schlosscafe/main|weekly", "pasted": text},
                    follow_redirects=False)
    assert r.status_code == 303 and "/admin/documents/" in r.headers["location"]
    assert "ok via rule" in r.headers["location"] or "ok%20via%20rule" in r.headers["location"]
    r = client.post("/admin/upload", data={"target": "schlosscafe/main|weekly"},
                    files={"file": ("menu.txt", b"\x00\x01binary", "application/octet-stream")})
    assert "Nicht unterstütztes Format" in r.text


def test_blocked_source_pauses_scheduled_imports(client):
    importer = client.app.state.importer
    with db.session() as s:
        s.add(db.ImportRun(source_id="diealm", trigger="schedule", status="blocked",
                           started_at=db.utcnow() - dt.timedelta(hours=2)))
    assert importer.recently_blocked("diealm")
    assert importer.run_source("diealm", "schedule") is None  # skipped without a request
    with db.session() as s:
        s.add(db.ImportRun(source_id="schlosscafe", trigger="schedule", status="blocked",
                           started_at=db.utcnow() - dt.timedelta(hours=30)))
    assert not importer.recently_blocked("schlosscafe")


def test_user_agent_modes():
    from app.importer.fetch import request_headers

    assert "LunchtimeBot" in request_headers()["User-Agent"]
    assert "lunch.example.at" not in request_headers("https://lunch.example.at")["User-Agent"]  # placeholder
    assert "lunch.gruber.at" in request_headers("https://lunch.gruber.at")["User-Agent"]
    browser = request_headers("https://lunch.gruber.at", "browser")
    assert "LunchtimeBot" not in browser["User-Agent"] and "Accept-Language" in browser
    assert request_headers(None, "MyAgent/1.0") == {"User-Agent": "MyAgent/1.0"}
