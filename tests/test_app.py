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
    text = (tmp_store.profiles_dir / "chains/ghk-burgenland.yml").read_text(encoding="utf-8")
    assert "# Gästehäuser & Küchen Burgenland" in text  # comment preserved
    assert "rule: franks-v2" in text
    assert tmp_store.document("ghk-burgenland/franks", "weekly").rule == "franks-v2"
    assert tmp_store.errors == {}


def test_invalid_profile_is_rejected(tmp_store):
    with pytest.raises(Exception):
        tmp_store.write_file("profiles/burgenland/eisenstadt/timeout.yml", "id: timeout\nname: x\n")  # missing index_url
    assert "timeout" in tmp_store.sources


def test_unknown_rule_reference_reported(tmp_store):
    path = tmp_store.profiles_dir / "burgenland/eisenstadt/timeout.yml"
    path.write_text(path.read_text(encoding="utf-8").replace("rule: timeout-wochenkarte", "rule: nope"))
    tmp_store.reload()
    assert "profiles/burgenland/eisenstadt/timeout.yml" in tmp_store.errors


def test_path_traversal_rejected(tmp_store):
    for name in ["profiles/../x.yml", "profiles/a/../../x.yml", "profiles/a/.x/b.yml", "rules/a/b.yml",
                 "other/x.yml", "profiles/X.yml", "profiles/a/b/c/d/x.yml", "profiles//x.yml"]:
        with pytest.raises(ValueError):
            tmp_store.path_for(name)
    assert tmp_store.path_for("profiles/burgenland/eisenstadt/x.yml") == tmp_store.profiles_dir / "burgenland/eisenstadt/x.yml"


SHORT = """id: wirt
name: Zum Wirt
index_url: https://example.org/menu
city: Eisenstadt
address: Hauptstraße 1, 7000 Eisenstadt
documents:
  - id: weekly
    match: {label: '^Wochenkarte'}
"""


def test_short_form_is_one_main_location(tmp_store):
    tmp_store.write_file("profiles/burgenland/eisenstadt/wirt.yml", SHORT)
    ref = tmp_store.location("wirt/main")
    assert ref.location.name == "Zum Wirt" and ref.location.city == "Eisenstadt"
    assert [d.id for d in ref.location.documents] == ["weekly"]
    assert "profiles/burgenland/eisenstadt/wirt.yml" in tmp_store.files()

    tmp_store.update_location("wirt", "main", {"geo": {"lat": 47.8, "lon": 16.5}}, comment="test")
    tmp_store.set_document_rule("wirt", "main", "weekly", "ghk-grid")
    text = (tmp_store.profiles_dir / "burgenland/eisenstadt/wirt.yml").read_text(encoding="utf-8")
    assert "locations" not in text and "geo: {lat: 47.8, lon: 16.5}  # test" in text and "rule: ghk-grid" in text
    assert tmp_store.location("wirt/main").location.geo.lat == 47.8

    with pytest.raises(Exception):  # a typo is not silently dropped
        tmp_store.write_file("profiles/burgenland/eisenstadt/wirt.yml", SHORT.replace("address:", "adress:"))


def test_defaults_are_merged_into_locations(tmp_store):
    naglreiter = tmp_store.sources["naglreiter"]
    gols = next(loc for loc in naglreiter.locations if loc.id == "gols")
    assert gols.website == "https://www.naglreiter.com/essen-beim-naglreiter"
    assert [d.match.url for d in gols.documents] == [
        r"/gols-mittagsmenue\.pdf$", r"/gols-speisekarte\.pdf$", r"/gols-fruehstueck\.pdf$"]
    hainburg = next(loc for loc in naglreiter.locations if loc.id == "hainburg")
    assert hainburg.documents[0].match.url == r"/hainburg-fmz-mittagsmenue\.pdf$"  # own list replaces defaults
    oberwart = tmp_store.location("billa-marktkueche/oberwart").location
    assert [d.id for d in oberwart.documents] == ["weekly"] and oberwart.info.startswith("Menü 1")

    # a rule assigned to an inherited document is written into defaults (all locations inheriting it)
    tmp_store.set_document_rule("billa-marktkueche", "neusiedl", "weekly", "ghk-grid")
    assert tmp_store.document("billa-marktkueche/eisenstadt-rusterstrasse", "weekly").rule == "ghk-grid"
    assert tmp_store.document("billa-marktkueche/oberwart", "weekly").rule == "billa-marktkueche"
    assert tmp_store.errors == {}


def test_defaults_need_locations(tmp_store):
    with pytest.raises(Exception):
        tmp_store.write_file("profiles/x.yml", SHORT + "defaults: {website: https://example.org}\n")


def test_folder_warning_and_cleanup(tmp_store):
    assert tmp_store.warnings == {}
    tmp_store.write_file("profiles/burgenland/oberwart/wirt.yml", SHORT)  # an Eisenstadt restaurant
    assert "main (Eisenstadt)" in tmp_store.warnings["profiles/burgenland/oberwart/wirt.yml"]
    tmp_store.delete_file("profiles/burgenland/oberwart/wirt.yml")
    assert not (tmp_store.profiles_dir / "burgenland/oberwart").exists()
    assert (tmp_store.profiles_dir / "burgenland/eisenstadt").exists()
    assert tmp_store.warnings == {}


def test_duplicate_source_id_across_folders(tmp_store):
    path = tmp_store.profiles_dir / "burgenland/x.yml"
    path.write_text(SHORT.replace("id: wirt", "id: timeout"), encoding="utf-8")
    tmp_store.reload()
    assert "burgenland/eisenstadt/timeout.yml" in tmp_store.errors["profiles/burgenland/x.yml"]


def test_slugify():
    from app.profiles import slugify

    assert slugify("Neusiedl am See") == "neusiedl-am-see"
    assert slugify("Güssing") == "guessing"
    assert slugify("Hainburg an der Donau") == "hainburg-an-der-donau"


def test_renamed_location_keys(tmp_path):
    from app import db

    db.init_db(f"sqlite:///{tmp_path / 'x.db'}")
    with db.session() as s:
        s.add(db.Document(source_id="timeout", location_key="timeout/technologiezentrum", doc_id="weekly",
                          kind="weekly", url="https://example.org/w.pdf"))
    db.init_db(f"sqlite:///{tmp_path / 'x.db'}")  # the next start renames it
    with db.session() as s:
        assert [d.location_key for d in s.query(db.Document)] == ["timeout/main"]
    old = Prefs(restaurants=["timeout/technologiezentrum", "timeout/main", "ghk-burgenland/franks"]).encode()
    assert Prefs.decode(old).restaurants == ["timeout/main", "ghk-burgenland/franks"]


def test_prefs_roundtrip_and_garbage():
    p = Prefs(restaurants=["a/b", "c/d"], lang="en", ok=True)
    assert Prefs.decode(p.encode()) == p
    assert Prefs.decode("not base64 !!") == Prefs()
    assert Prefs.decode(None) == Prefs()
    full = Prefs(restaurants=["a/b"], theme="dark", view="all", sort="distance", fav_first=False, diet="vegan")
    assert Prefs.decode(full.encode()) == full


def test_prefs_unknown_values_fall_back():
    import base64
    import json

    raw = base64.urlsafe_b64encode(json.dumps({"t": "neon", "v": "x", "s": "price", "d": "keto"}).encode()).decode()
    p = Prefs.decode(raw)
    assert (p.theme, p.view, p.sort, p.diet, p.fav_first) == (None, None, "city", None, True)


def test_diet_tags_from_category():
    from app.db import MenuItem
    from app.web.menus import diet_tags

    assert diet_tags(MenuItem(name="Linsencurry", category="Menü Vegetarisch", tags="")) == {"vegetarian"}
    assert diet_tags(MenuItem(name="Bowl", category="VEGAN", tags="")) == {"vegan", "vegetarian"}
    assert diet_tags(MenuItem(name="Bowl", category="Vegetarisches Wochengericht", tags="")) == {"vegetarian"}
    assert diet_tags(MenuItem(name="Schnitzel", category="Menü 1", tags="")) == set()
    assert diet_tags(MenuItem(name="Tofu", category=None, tags="vegan,vegetarian")) == {"vegan", "vegetarian"}


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
    for url in ["/", "/d/2026-11-03", "/restaurants", "/restaurants?q=pinkafeld", "/allergene", "/impressum",
                "/datenschutz", "/healthz", "/admin/login"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/d/not-a-date", follow_redirects=False).status_code == 303
    assert client.get("/admin", follow_redirects=False).headers["location"].startswith("/admin/login")
    assert 'href="https://github.com/andygruber/lunchtime"' in client.get("/").text  # footer source link


def test_selection_cookie_and_language(client):
    r = client.post("/prefs/toggle", data={"key": "timeout/main", "next": "/restaurants"},
                    follow_redirects=False)
    assert r.status_code == 303
    prefs = Prefs.decode(client.cookies.get(COOKIE))
    assert prefs.restaurants == ["timeout/main"] and prefs.ok
    client.post("/prefs/toggle", data={"key": "unknown/thing"})
    assert Prefs.decode(client.cookies.get(COOKIE)).restaurants == ["timeout/main"]
    client.post("/prefs/lang", data={"lang": "en", "next": "/"})
    assert "Lunch menus" in client.get("/d/2026-11-03").text or "Today" in client.get("/").text


def test_theme_and_view_prefs(client):
    r = client.post("/prefs/theme", data={"theme": "dark", "next": "/restaurants"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/restaurants"
    assert Prefs.decode(client.cookies.get(COOKIE)).theme == "dark"
    assert '<html lang="de" data-theme="dark">' in client.get("/", headers={"accept-language": "de"}).text
    client.post("/prefs/theme", data={"theme": "auto"})
    assert Prefs.decode(client.cookies.get(COOKIE)).theme is None

    client.post("/prefs/view", data={"view": "all", "sort": "distance", "fav_first": "0", "diet": "vegan"})
    p = Prefs.decode(client.cookies.get(COOKIE))
    assert (p.view, p.sort, p.fav_first, p.diet) == ("all", "distance", False, "vegan")
    client.post("/prefs/view", data={"diet": "all", "sort": "bogus"})  # only the given fields change
    p = Prefs.decode(client.cookies.get(COOKIE))
    assert (p.view, p.sort, p.diet) == ("all", "distance", None)


def test_background_prefs_request_gets_no_redirect(client):
    r = client.post("/prefs/toggle", data={"key": "timeout/main"}, headers={"x-requested-with": "fetch"},
                    follow_redirects=False)
    assert r.status_code == 204
    assert Prefs.decode(client.cookies.get(COOKIE)).restaurants == ["timeout/main"]


def test_favourites_view_and_all_view(client):
    client.post("/prefs/toggle", data={"key": "timeout/main"})
    html = client.get("/d/2026-11-03").text
    assert html.count('<article class="card') == 1 and 'aria-pressed="true">★' in html  # favourites tab active
    client.post("/prefs/view", data={"view": "all"})
    html = client.get("/d/2026-11-03").text
    assert "Favoriten zuerst" in html or "Favourites first" in html


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
        doc = db.Document(source_id="timeout", location_key="timeout/main", doc_id="weekly",
                          kind="weekly", url="https://example.org/w.pdf", label="Wochenkarte", sha256=sha,
                          first_seen=dt.datetime(2026, 11, 1, 10), last_seen=dt.datetime(2026, 11, 1, 10))
        s.add(doc)
        s.flush()
        doc_id = doc.id
    assert importer.process_document(doc_id).startswith("ok via rule")
    html = client.get("/d/2026-11-03").text
    assert "Rückensteak vom Schneeberg Landschwein" in html
    assert "Gebackene Hühnerleber" in html  # weekly special

    client.post("/prefs/toggle", data={"key": "timeout/main"})
    client.post("/prefs/view", data={"diet": "vegetarian"})
    html = client.get("/d/2026-11-03").text
    assert "Rückensteak vom Schneeberg Landschwein" not in html  # not marked vegetarian
    assert "data-fav=\"1\"" in html  # the favourite's card stays, with a note


def test_special_replaces_menu(client):
    """A buffet flyer with replaces_menu hides the weekly plan's "BUFFET" placeholders on its date."""
    import hashlib

    from app import db

    importer = client.app.state.importer
    docs = {"weekly": "ghk_Menueplan_3_KW45_02.11.-06.11.2026_-_FH_Mensa.pdf",
            "buffet": "ghk_Wald_und_Wiese_04112026_ohne_Allergene.pdf"}
    for doc_id, name in docs.items():
        pdf = (ROOT / "tests/fixtures/pdfs" / name).read_bytes()
        sha = hashlib.sha256(pdf).hexdigest()
        (importer.settings.files_dir / f"{sha}.pdf").write_bytes(pdf)
        with db.session() as s:
            doc = db.Document(source_id="ghk-burgenland", location_key="ghk-burgenland/eisenstadt-mensa",
                              doc_id=doc_id, kind="special" if doc_id == "buffet" else "weekly",
                              url=f"https://example.org/{name}", label=doc_id, sha256=sha,
                              first_seen=dt.datetime(2026, 11, 1, 10), last_seen=dt.datetime(2026, 11, 1, 10))
            s.add(doc)
            s.flush()
            new_id = doc.id
        assert importer.process_document(new_id).startswith("ok via rule")
    client.post("/prefs/toggle", data={"key": "ghk-burgenland/eisenstadt-mensa"})

    html = client.get("/d/2026-11-04").text
    assert "Wald &amp; Wiese" in html and "BUFFET" not in html
    assert "<li>Hirschragout mit Preiselbeeren</li>" in html and "11:30 – 13:30 Uhr" in html
    assert "Lachscurry mit Wokgemüse und Duftreis" in client.get("/d/2026-11-03").text  # other days unchanged


def test_static_urls_carry_content_hash(client):
    import re

    html = client.get("/").text
    css = re.search(r'href="(/static/css/app\.css\?v=[0-9a-f]{10})"', html).group(1)
    assert client.get(css).status_code == 200


def test_site_settings_from_env_override_site_yml(tmp_path):
    from app.config import load_site

    (tmp_path / "site.yml").write_text("site_name: Mittag\nimpressum:\n  owner: Aus der Datei\n  phone: '+43 1'\n")
    site = load_site(tmp_path, {"IMPRESSUM_OWNER": "Aus .env", "IMPRESSUM_EMAIL": "a@b.at", "SITE_BASE_URL": " ",
                                "SITE_HOSTING": "OCI Frankfurt"})
    assert (site.site_name, site.impressum.owner, site.impressum.email) == ("Mittag", "Aus .env", "a@b.at")
    assert site.impressum.phone == "+43 1" and site.base_url is None  # kept from the file / empty = unset
    assert "impressum.address" in site.placeholders() and "hosting" not in site.placeholders()
    assert load_site(tmp_path / "missing", {"SITE_NAME": "X"}).site_name == "X"  # no site.yml at all


def test_repository_site_yml_has_no_operator_data():
    from app.config import load_site

    site = load_site(ROOT / "config", {})
    assert {"hosting", "impressum.owner", "impressum.address", "impressum.email"} <= set(site.placeholders())
