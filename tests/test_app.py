"""Profile editing, preferences cookie and the web app (with a temporary data directory)."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.profiles import ProfileStore
from app.web.common import COOKIE, Prefs

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def tmp_store(tmp_path) -> ProfileStore:
    shutil.copytree(ROOT / "profiles", tmp_path / "profiles")
    shutil.copytree(ROOT / "rules", tmp_path / "rules")
    return ProfileStore(tmp_path / "profiles", tmp_path / "rules")


def test_save_rule_and_assign_keeps_comments(tmp_store):
    tmp_store.save_rule("franks-v2", {"type": "sections", "item_split": "line"}, header="test")
    tmp_store.set_document_rule("ghk-burgenland", "franks", "weekly", "franks-v2")
    text = (tmp_store.profiles_dir / "ghk-burgenland.yml").read_text(encoding="utf-8")
    assert "# Gästehäuser & Küchen Burgenland" in text  # comment preserved
    assert "rule: franks-v2" in text
    assert tmp_store.document("ghk-burgenland/franks", "weekly").rule == "franks-v2"
    assert tmp_store.errors == {}


def test_invalid_profile_is_rejected(tmp_store):
    with pytest.raises(Exception):
        tmp_store.write_file("profiles/timeout.yml", "id: timeout\nname: x\n")  # missing index_url/locations
    assert "timeout" in tmp_store.sources


def test_unknown_rule_reference_reported(tmp_store):
    path = tmp_store.profiles_dir / "timeout.yml"
    path.write_text(path.read_text(encoding="utf-8").replace("rule: timeout-wochenkarte", "rule: nope"))
    tmp_store.reload()
    assert "profiles/timeout.yml" in tmp_store.errors


def test_path_traversal_rejected(tmp_store):
    for name in ["profiles/../x.yml", "rules/a/b.yml", "other/x.yml", "profiles/X.yml"]:
        with pytest.raises(ValueError):
            tmp_store.path_for(name)


def test_prefs_roundtrip_and_garbage():
    p = Prefs(restaurants=["a/b", "c/d"], lang="en", ok=True)
    assert Prefs.decode(p.encode()) == p
    assert Prefs.decode("not base64 !!") == Prefs()
    assert Prefs.decode(None) == Prefs()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", "test")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_pages(client):
    for url in ["/", "/d/2026-11-03", "/restaurants", "/restaurants?q=pinkafeld", "/impressum", "/datenschutz",
                "/healthz", "/admin/login"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/d/not-a-date", follow_redirects=False).status_code == 303
    assert client.get("/admin", follow_redirects=False).headers["location"].startswith("/admin/login")


def test_selection_cookie_and_language(client):
    r = client.post("/prefs/toggle", data={"key": "timeout/technologiezentrum", "next": "/restaurants"},
                    follow_redirects=False)
    assert r.status_code == 303
    prefs = Prefs.decode(client.cookies.get(COOKIE))
    assert prefs.restaurants == ["timeout/technologiezentrum"] and prefs.ok
    client.post("/prefs/toggle", data={"key": "unknown/thing"})
    assert Prefs.decode(client.cookies.get(COOKIE)).restaurants == ["timeout/technologiezentrum"]
    client.post("/prefs/lang", data={"lang": "en", "next": "/"})
    assert "Lunch menus" in client.get("/d/2026-11-03").text or "Today" in client.get("/").text


def test_open_redirect_blocked(client):
    r = client.post("/prefs/consent", data={"next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_accept_language(client):
    assert "Heute" in client.get("/", headers={"accept-language": "de-AT,de;q=0.9"}).text
    assert "Today" in client.get("/", headers={"accept-language": "en-US,en;q=0.8"}).text


def test_day_view_with_imported_document(client, tmp_path):
    """Store a fixture PDF through the pipeline (without network) and render it."""
    from app import db

    importer = client.app.state.importer
    pdf = (ROOT / "tests/fixtures/pdfs/timeout_wochenkarte.pdf").read_bytes()
    import hashlib

    sha = hashlib.sha256(pdf).hexdigest()
    (importer.settings.files_dir / f"{sha}.pdf").write_bytes(pdf)
    with db.session() as s:
        doc = db.Document(source_id="timeout", location_key="timeout/technologiezentrum", doc_id="weekly",
                          kind="weekly", url="https://example.org/w.pdf", label="Wochenkarte", sha256=sha,
                          first_seen=dt.datetime(2026, 11, 1, 10), last_seen=dt.datetime(2026, 11, 1, 10))
        s.add(doc)
        s.flush()
        doc_id = doc.id
    assert importer.process_document(doc_id).startswith("ok via rule")
    html = client.get("/d/2026-11-03").text
    assert "Rückensteak vom Schneeberg Landschwein" in html
    assert "Gebackene Hühnerleber" in html  # weekly special
