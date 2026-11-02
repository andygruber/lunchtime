"""Regression tests: every configured rule against the archived PDFs and index pages in tests/fixtures."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.extract.common import Context
from app.extract.engine import run_rule
from app.extract.model import ExtractionError, StaleDocument
from app.importer.fetch import extract_links, match_source
from app.profiles import ProfileStore

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
FETCH_DATE = dt.date(2026, 11, 1)


@pytest.fixture(scope="module")
def store() -> ProfileStore:
    s = ProfileStore(ROOT / "profiles", ROOT / "rules")
    assert s.errors == {}
    return s


def run(store, location_key: str, doc_id: str, pdf: str, label: str | None = None):
    rule = store.resolve_rule(store.document(location_key, doc_id).rule)
    ctx = Context(fetch_date=FETCH_DATE, label=label, url=f"https://example.org/{pdf}")
    return run_rule(rule, (FIX / "pdfs" / pdf).read_bytes(), ctx)


def names(ex, date: str) -> list[str]:
    return [i.name for i in ex.days[dt.date.fromisoformat(date)].items]


def test_ghk_links_match_expected_locations(store):
    html = (FIX / "html" / "ghk-burgenland.html").read_text(encoding="utf-8")
    links = extract_links(html, store.sources["ghk-burgenland"].index_url)
    matched = match_source(links, store.sources["ghk-burgenland"])
    counts = {k: len(v) for k, v in matched.items()}
    assert counts == {
        ("eisenstadt-kaiserallee", "weekly"): 1,
        ("eisenstadt-mensa", "weekly"): 1,
        ("eisenstadt-mensa", "buffet"): 0,  # not online yet when the page was archived
        ("hotel-kultura", "weekly"): 1,
        ("franks", "weekly"): 1,
        ("stoob", "weekly"): 2,
        ("oberwart", "weekly"): 3,
        ("pinkafeld-mensa", "weekly"): 2,
        ("pinkafeld-mensa", "buffet"): 1,
        ("pinkafeld-gaestehaus", "weekly"): 2,
        ("podersdorf", "normalkost"): 2,
        ("podersdorf", "vegan"): 2,
        ("stegersbach", "weekly"): 2,
    }


def test_timeout_links_deduplicated(store):
    html = (FIX / "html" / "timeout-technologiezentrum.html").read_text(encoding="utf-8")
    links = extract_links(html, store.sources["timeout"].index_url)
    matched = match_source(links, store.sources["timeout"])
    assert len(matched[("technologiezentrum", "weekly")]) == 1
    assert len(matched[("technologiezentrum", "a-la-carte")]) == 1


def test_hotel_kultura(store):
    ex = run(store, "ghk-burgenland/hotel-kultura", "weekly", "ghk_Menue_KW_45_Hotelkultura.pdf")
    assert ex.valid_from == dt.date(2026, 11, 2)
    assert names(ex, "2026-11-03")[0] == "Cremesuppe von der Zucchini\nNougatknödel mit Butterbrösel\nBeerenragout"
    item = ex.days[dt.date(2026, 11, 3)].items[0]
    assert item.allergens == ["A", "C", "G", "H", "L"]
    assert all("abendessen" not in (i.category or "").lower() for d in ex.days.values() for i in d.items)


def test_eisenstadt_mensa_buffet_and_saturday(store):
    ex = run(store, "ghk-burgenland/eisenstadt-mensa", "weekly",
             "ghk_Menueplan_3_KW45_02.11.-06.11.2026_-_FH_Mensa.pdf")
    assert all(i.special for i in ex.days[dt.date(2026, 11, 4)].items)
    assert dt.date(2026, 11, 7) in ex.days
    assert "Lachscurry mit Wokgemüse und Duftreis" in names(ex, "2026-11-03")


def test_stoob_stale_document_rejected(store):
    with pytest.raises(StaleDocument):
        run(store, "ghk-burgenland/stoob", "weekly", "ghk_Menueplan_KW_44.pdf")


def test_stoob(store):
    ex = run(store, "ghk-burgenland/stoob", "weekly", "ghk_Menueplan_1_KW_43_Internat.pdf")
    cats = [i.category for i in ex.days[dt.date(2026, 10, 19)].items]
    assert cats == ["Tagessuppe", "Hauptspeise Traditionell", "Hauptspeise Vegetarisch", "Dessert"]


def test_pinkafeld_gaestehaus_closed_monday(store):
    ex = run(store, "ghk-burgenland/pinkafeld-gaestehaus", "weekly", "ghk_Menueplan_kw45_pkfld_2nov_6nov_v2.pdf")
    assert "Schulfrei" in ex.days[dt.date(2026, 11, 2)].closed
    assert names(ex, "2026-11-03")[0] == "Knoblauchcremesuppe\nPasta mit Sauce Carbonara\nBlattsalat"


def test_pinkafeld_mensa(store):
    ex = run(store, "ghk-burgenland/pinkafeld-mensa", "weekly", "ghk_Menueplan_Mensa_pkfld_Woche_3_KW45.pdf",
             label="Pinkafeld Mensa Speiseplan 02.11. - 06.11.")
    monday = ex.days[dt.date(2026, 11, 2)].items
    assert [i.category for i in monday] == ["Tagessuppe", "Menü 1", "Menü 2"]
    assert monday[1].price_cents == 950 and monday[2].price_cents == 980
    assert all(i.special for i in ex.days[dt.date(2026, 11, 3)].items)
    weekly = {i.category: i for i in ex.weekly_items}
    assert weekly["Wochenteller"].price_cents == 1550
    assert weekly["Lieblingsteller"].allergens == ["A", "E", "F", "H"]


def test_pinkafeld_buffet(store):
    ex = run(store, "ghk-burgenland/pinkafeld-mensa", "buffet", "ghk_Buffet_Pinkafeld_Mensa.pdf")
    (item,) = ex.days[dt.date(2026, 10, 6)].items
    assert item.special and item.price_cents == 1380
    assert item.name == "Bayerische Schmankerl"
    assert "Knuspriger Schweinsbraten" in item.description


def test_eisenstadt_mensa_buffet_flyer(store):
    from app.importer.fetch import Link

    # the link label carries a wrong date (October); only the flyer's own date counts
    link = Link("https://www.ghk-burgenland.at/fileadmin/user_upload/pdfs/Menueplaene_Eisenstadt/"
                "Wald_und_Wiese_04112026_ohne_Allergene.pdf", 'Buffet Mensa Eisenstadt "Wald und Wiese" 04.10.2026')
    matched = match_source([link], store.sources["ghk-burgenland"])
    assert [k for k, v in matched.items() if v] == [("eisenstadt-mensa", "buffet")]

    ex = run(store, "ghk-burgenland/eisenstadt-mensa", "buffet", "ghk_Wald_und_Wiese_04112026_ohne_Allergene.pdf",
             label=link.label)
    assert ex.valid_from == ex.valid_to == dt.date(2026, 11, 4)
    (item,) = ex.days[dt.date(2026, 11, 4)].items
    assert item.special and item.name == "Wald & Wiese" and item.price_cents == 1380
    assert "Hirschragout mit Preiselbeeren" in item.description
    assert "7000 Eisenstadt" not in item.description and "Weihnachtszauber" not in item.description


@pytest.mark.parametrize("pdf", ["ghk_Menue_KW.45._Kids_mit_Dessert.pdf", "ghk_Speiseplan_Stegersbach_KW43.pdf"])
def test_combit(store, pdf):
    loc = "oberwart" if "Kids" in pdf else "stegersbach"
    ex = run(store, f"ghk-burgenland/{loc}", "weekly", pdf)
    for day in ex.days.values():
        assert [i.category for i in day.items] == ["Menü 1", "Menü 2", "Dessert"]


def test_stegersbach_rows(store):
    ex = run(store, "ghk-burgenland/stegersbach", "weekly", "ghk_Speiseplan_Stegersbach_KW43.pdf")
    assert names(ex, "2026-10-19") == [
        "Klare Gemüsesuppe mit Grießnockerl\nGermknödel mit Vanillesauce",
        "Klare Gemüsesuppe mit Grießnockerl\nKürbis Lasagne mit Vitamin\nKick Salat",
        "Saisonales Obst",
    ]


def test_podersdorf(store):
    ex = run(store, "ghk-burgenland/podersdorf", "normalkost",
             "ghk_Speiseplan_Schule_Kindergarten_Normalkost_KW44.pdf")
    assert ex.days[dt.date(2026, 10, 26)].closed == "Ruhetag"
    tue = ex.days[dt.date(2026, 10, 27)].items
    assert [i.category for i in tue] == ["Suppe", "Menü 1", "Menü 2", "Dessert"]
    assert tue[0].allergens == ["G", "L"]
    vegan = run(store, "ghk-burgenland/podersdorf", "vegan",
                "ghk_Speiseplan_Schule_Kindergarten_-_Allergie-Vegan_KW43.pdf")
    assert "vegan" in vegan.days[dt.date(2026, 10, 20)].items[2].tags


def test_franks(store):
    ex = run(store, "ghk-burgenland/franks", "weekly", "ghk_franks_kw45_02_06_nov.pdf")
    assert ex.valid_to == dt.date(2026, 11, 6)
    mon = ex.days[dt.date(2026, 11, 2)].items
    assert [(i.category, i.price_cents) for i in mon] == [("Suppe", None), ("Menü 1", 1290), ("Menü 2", 1190)]
    assert {i.name for i in ex.weekly_items} == {"Grüner Salat", "Gemischter Salat"}


def test_timeout(store):
    ex = run(store, "timeout/technologiezentrum", "weekly", "timeout_wochenkarte.pdf")
    tue = ex.days[dt.date(2026, 11, 3)].items
    assert [(i.name, i.description) for i in tue] == [
        ("Klare Suppe vom Schneebergrind", "Frittaten | Gemüse"),
        ("Rückensteak vom Schneeberg Landschwein", "Pfeffersauce | Kroketten | Röstgemüse"),
        ("Kaspressknödel", "Tsatsiki | bunter Salat"),
    ]
    assert len(ex.days[dt.date(2026, 11, 6)].items) == 4
    (special,) = ex.weekly_items
    assert special.name == "Gebackene Hühnerleber" and special.price_cents == 1290


def test_missing_rule_raises(store):
    from app.extract.rules import GridRule

    with pytest.raises(ExtractionError):
        run_rule(GridRule(type="grid"), (FIX / "pdfs" / "timeout_wochenkarte.pdf").read_bytes(),
                 Context(fetch_date=FETCH_DATE))
