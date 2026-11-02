"""Regression tests for web-page, image and additional PDF sources (fixtures from 2026-11-01)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.extract import ocr
from app.extract.common import Context
from app.extract.engine import load_document, run_rule
from app.extract.html import page_text
from app.extract.model import ExtractionError
from app.importer.fetch import extract_links
from app.importer.pipeline import Importer
from app.profiles import ProfileStore

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
FETCH_DATE = dt.date(2026, 11, 1)
needs_ocr = pytest.mark.skipif(not ocr.available(), reason="tesseract not installed")


@pytest.fixture(scope="module")
def store() -> ProfileStore:
    s = ProfileStore(ROOT / "profiles", ROOT / "rules")
    assert s.errors == {}
    return s


def run(store, key: str, doc_id: str, path: str, media: str, label: str | None = None):
    spec = store.document(key, doc_id)
    doc = load_document((FIX / path).read_bytes(), media, spec.selector, spec.ocr.model_dump())
    return run_rule(store.resolve_rule(spec.rule), doc, Context(fetch_date=FETCH_DATE, label=label, url=path))


def day(ex, date: str):
    return ex.days[dt.date.fromisoformat(date)].items


def test_all_locations_have_coordinates(store):
    missing = [r.key for r in store.locations() if r.location.geo is None]
    assert missing == []


def test_diealm_two_weeks_with_price_notes(store):
    ex = run(store, "diealm/eisenstadt", "weekly", "html/diealm-mittagsteller.html", "text/html")
    assert (ex.valid_from, ex.valid_to) == (dt.date(2026, 11, 2), dt.date(2026, 11, 13))
    assert [i.category for i in day(ex, "2026-11-02")] == ["Suppe", "Tagesteller", "Tagesteller"]
    assert day(ex, "2026-11-13")[-1].name.startswith("In Kräuterbutter und Wein pochiertes Welsfilet")
    assert any("Fisch-Tagesteller" in n for n in ex.notes)


def test_rabina_hofpassage_pdf_same_rule(store):
    ex = run(store, "rabina-hofpassage/eisenstadt", "weekly", "pdfs/rabina_mittagsteller_kw45.pdf", "application/pdf")
    assert day(ex, "2026-11-06")[1].name.startswith("Fritto Misto")


def test_bsh_tables_short_weekdays(store):
    ex = run(store, "bsh-eisenstadt/fred-sinowatz-haus", "weekly", "html/bsh-speiseplan.html", "text/html")
    assert ex.days[dt.date(2026, 11, 2)].closed == "Allerseelen"
    assert [i.category for i in day(ex, "2026-11-05")] == ["Suppe / Nachspeise", "Menü 1", "Menü 2"]
    assert not any(d.weekday() >= 5 for d in ex.days)
    assert dt.date(2026, 10, 19) in ex.days  # first table on the page


def test_oscars(store):
    ex = run(store, "oscars/eze", "weekly", "html/oscars.html", "text/html")
    assert [(i.category, i.name) for i in day(ex, "2026-11-03")][0] == ("Teller 1", "Puten Cordon Bleu mit Petersilkartoffeln")
    (pizza,) = ex.weekly_items
    assert (pizza.name, pizza.price_cents) == ("Margherita", 1090)
    assert "Mittagsteller € 10,90" in ex.notes


def test_schlosscafe_daily_and_weekly_card(store):
    ex = run(store, "schlosscafe/eisenstadt", "weekly", "html/schlosscafe-speisekarte.html", "text/html")
    assert ex.days[dt.date(2026, 10, 26)].closed
    (thu,) = day(ex, "2026-10-29")
    assert thu.price_cents == 1190 and "vegetarian" in thu.tags and "veggie" not in thu.name
    fish = next(i for i in ex.weekly_items if i.name == "Fish & Chips")
    assert fish.price_cents == 1650 and fish.description.startswith("Klassisch")


@pytest.mark.parametrize("pdf,weekly", [
    ("naglreiter_eisenstadt.pdf", 0), ("naglreiter_neusiedl_stammhaus.pdf", 2), ("naglreiter_hainburg.pdf", 1),
])
def test_naglreiter(store, pdf, weekly):
    ex = run(store, "naglreiter/eisenstadt", "weekly", f"pdfs/{pdf}", "application/pdf")
    mon = day(ex, "2026-11-02")
    assert [i.category for i in mon] == ["Suppe", "Menü I", "Menü II"]
    assert mon[0].allergens == ["A", "C", "F", "G", "L"]
    assert len(ex.weekly_items) == weekly
    assert ex.notes


def test_naglreiter_links_per_location(store):
    html = (FIX / "html/naglreiter.html").read_text(encoding="utf-8")
    src = store.sources["naglreiter"]
    for loc in src.locations:
        for spec in loc.documents:
            found = Importer._candidates(html, src.index_url, spec)
            assert len(found) == 1, (loc.id, spec.id)


def test_image_candidates_with_rewrite(store):
    html = (FIX / "html/kiss-menue.html").read_text(encoding="utf-8")
    spec = store.document("gasthaus-kiss/eisenstadt", "daily")
    (img,) = Importer._candidates(html, store.sources["gasthaus-kiss"].index_url, spec)
    assert "/transf/none/" in img.url and "i9ee5cf005edc92f4" in img.url
    html = (FIX / "html/karlwirt-wochenmenue.html").read_text(encoding="utf-8")
    spec = store.document("karlwirt/eisenstadt", "weekly")
    (img,) = Importer._candidates(html, store.sources["karlwirt"].index_url, spec)
    assert img.label.startswith("Wochenmenü 26. - 30. Oktober 2026")  # page title as label (week fallback)


def test_static_link_on_other_page(store):
    html = (FIX / "html/diealm-food-drink.html").read_text(encoding="utf-8")
    spec = store.document("diealm/eisenstadt", "speisekarte")
    (link,) = Importer._candidates(html, spec.page, spec)
    assert link.url.endswith("Speisekarte-Oktober-2026.pdf")


def test_page_hash_ignores_markup_noise():
    html = (FIX / "html/oscars.html").read_text(encoding="utf-8")
    noisy = html.replace("</body>", '<script>var nonce="abc123"</script><div style="x"></div></body>')
    assert page_text(html) == page_text(noisy)


def test_html_links_selector():
    html = (FIX / "html/rabina-hofpassage.html").read_text(encoding="utf-8")
    links = extract_links(html, "https://rabinas-hofpassage.at/")
    assert any("Mittagsteller-KW45" in l.url for l in links)


@needs_ocr
def test_kiss_daily_image(store):
    ex = run(store, "gasthaus-kiss/eisenstadt", "daily", "images/kiss_tagesmenue.png", "image/png")
    items = day(ex, "2026-11-02")
    assert [i.category.replace("Ü", "U") for i in items] == ["MENU", "VEGETARISCHER TELLER", "DIE WIRTIN EMPFIEHLT",
                                                              "DIE WIRTIN EMPFIEHLT", "Dessert"]  # OCR: MENU/MENÜ
    assert [i.price_cents for i in items] == [1290, 1390, 1890, 1720, 590]


@needs_ocr
def test_karlwirt_image_table(store):
    ex = run(store, "karlwirt/eisenstadt", "weekly", "images/karlwirt_wochenmenue.jpg", "image/jpeg",
             label="Wochenmenü 26. - 30. Oktober 2026")
    assert ex.days[dt.date(2026, 10, 26)].closed
    thu = day(ex, "2026-10-29")
    assert [i.category for i in thu] == ["Suppe", "Hauptspeise", "Hauptspeise", "Dessert"]
    assert thu[0].name == "Leberknödelsuppe"


@needs_ocr
def test_altesbackhaus_image(store):
    ex = run(store, "altesbackhaus/eisenstadt", "weekly", "images/altesbackhaus_wochenkarte.jpg", "image/jpeg")
    assert ex.valid_from == dt.date(2026, 11, 2)
    assert len(ex.days) == 5 and all(len(d.items) == 3 for d in ex.days.values())
    thu = day(ex, "2026-11-05")
    assert "vegan" in thu[2].tags and not thu[2].name.endswith("V+")


def test_billa_two_weeks_rows_and_veggie_marker(store):
    ex = run(store, "billa-marktkueche/oberwart", "weekly", "pdfs/billa_marktkueche_kw45_46.pdf", "application/pdf")
    assert (ex.valid_from, ex.valid_to) == (dt.date(2026, 11, 2), dt.date(2026, 11, 14))
    assert len(ex.days) == 10 and all(len(d.items) == 2 for d in ex.days.values())
    veg, meat = day(ex, "2026-11-03")
    assert (veg.category, veg.name) == ("Menü 1", "Gebackener Emmentaler mit Preiselbeeren & Salatbouquet")
    assert "vegetarian" in veg.tags and "vegetarian" not in meat.tags
    assert day(ex, "2026-11-04")[0].name.startswith("Brokkoli-Käsestrudel")  # "veggie" on its own line
    assert day(ex, "2026-11-05")[1].name.startswith("Faschierte Laibchen")
    assert "vegetarian" not in day(ex, "2026-11-11")[0].tags  # Schupfnudeln mit Speck


def test_billa_same_pdf_for_all_markets(store):
    html = (FIX / "html/billa-marktkueche.html").read_text(encoding="utf-8")
    src = store.sources["billa-marktkueche"]
    urls = set()
    for loc in src.locations:
        (link,) = Importer._candidates(html, src.index_url, loc.documents[0])
        urls.add(link.url)
    assert len(urls) == 1 and urls.pop().endswith("Mittagsmenue_KW_45_46_Intra.pdf")


def test_henis_month_page_table(store):
    html = (FIX / "html/henis-start.html").read_text(encoding="utf-8")
    spec = store.document("henis-imbiss/eisenstadt", "monthly")
    links = Importer._candidates(html, store.sources["henis-imbiss"].index_url, spec)
    assert [link.label for link in links] == ["NOVEMBER", "DEZEMBER"]
    ex = run(store, "henis-imbiss/eisenstadt", "monthly", "html/henis-november.html", "text/html")
    assert (ex.valid_from, ex.valid_to) == (dt.date(2026, 11, 2), dt.date(2026, 11, 30))
    assert len(ex.days) == 21 and all(len(d.items) == 1 for d in ex.days.values())
    (thu,) = day(ex, "2026-11-05")
    assert thu.name == "Vegateller: Geb. Käse, Karfiol, Champion mit Pommes und Sauce Tartare"  # wrapped cell
    assert thu.price_cents == 950 and "vegetarian" in thu.tags


def test_henis_unplanned_month_fails(store):
    with pytest.raises(ExtractionError):
        run(store, "henis-imbiss/eisenstadt", "monthly", "html/henis-dezember.html", "text/html")


def test_haydnbraeu_consecutive_tables_and_wrapped_lines(store):
    html = (FIX / "html/haydnbraeu-tagesmenue.html").read_text(encoding="utf-8")
    spec = store.document("haydnbraeu/eisenstadt", "weekly")
    links = Importer._candidates(html, store.sources["haydnbraeu"].index_url, spec)
    assert [link.url for link in links] == ["https://www.haydnbraeu.at/tagesmenue.php",
                                            "https://www.haydnbraeu.at/tagesmenue.php?m=1"]
    ex = run(store, "haydnbraeu/eisenstadt", "weekly", "html/haydnbraeu-tagesmenue.html", "text/html")
    assert (ex.valid_from, ex.valid_to) == (dt.date(2026, 11, 2), dt.date(2026, 11, 6))
    soup, veg, meat = day(ex, "2026-11-04")
    assert soup.category == "Suppe"
    assert soup.name == "Altwiener Erdäpfelrahmsuppe mit Lauch und gerösteten Semmelscheiben"  # wrapped cell
    assert [veg.price_cents, meat.price_cents] == [1090, 1190]
    assert day(ex, "2026-11-05")[2].category == "Tagesmenü"  # "Suppengemüse" is not a soup
    assert ex.notes == ["ZUM Mitnehmen plus 0,50 Euro für Mitnahmegeschirr"]
    with pytest.raises(ExtractionError):  # next week not published yet
        run(store, "haydnbraeu/eisenstadt", "weekly", "html/haydnbraeu-naechste-woche.html", "text/html")


def test_theresianum_week_pages(store):
    html = (FIX / "html/theresianum-fnb.html").read_text(encoding="utf-8")
    spec = store.document("theresianum/eisenstadt", "weekly")
    links = Importer._candidates(html, store.sources["theresianum"].index_url, spec)
    assert [link.label for link in links] == ["KW 43", "KW 45"]
    ex = run(store, "theresianum/eisenstadt", "weekly", "html/theresianum-kw45.html", "text/html")
    assert ex.days[dt.date(2026, 11, 2)].closed == "Allerseelen"
    assert [(i.category, i.name) for i in day(ex, "2026-11-03")] == [
        ("Suppe", "Tomatencremesuppe mit Croutons natur"), ("Menü", "Kaiserschmarren mit Apfelmus")]
    assert [i.category for i in day(ex, "2026-11-05")] == ["Menü", "Dessert"]  # Pfirsichkompott
    ex = run(store, "theresianum/eisenstadt", "weekly", "html/theresianum-kw43.html", "text/html")
    assert day(ex, "2026-10-22")[1].name == (
        "Gemüseschnitzel mit Bio-Erdäpfelschmarren und Eisbergsalat-Joghurtkräuterdressing")
