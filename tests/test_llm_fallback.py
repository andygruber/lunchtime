"""LLM fallback flow with a fake provider: extraction → stored with origin 'llm' → verified rule suggestion."""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from app import db
from app.config import get_settings
from app.extract.common import Context
from app.extract.engine import run_rule
from app.importer.pipeline import Importer
from app.llm.schema import LLMItem, LLMMenu, LLMRule
from app.profiles import ProfileStore

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "tests/fixtures/pdfs/ghk_franks_kw45_02_06_nov.pdf"
GOOD_RULE = (ROOT / "rules/ghk-franks.yml").read_text(encoding="utf-8")


class FakeProvider:
    name, model = "fake", "fake-1"

    def __init__(self, rule_answers: list[str]):
        self.rule_answers = list(rule_answers)
        self.calls: list[str] = []

    def complete(self, system, prompt, schema, attachment=None, effort="medium"):
        self.calls.append(schema.__name__)
        if schema is LLMMenu:
            store = ProfileStore(ROOT / "profiles", ROOT / "rules")
            ex = run_rule(store.resolve_rule("ghk-franks"), PDF.read_bytes(), Context(fetch_date=dt.date(2026, 11, 1)))

            def item(i):
                return LLMItem(category=i.category, name=i.name, description=i.description,
                               price_cents=i.price_cents, allergens=i.allergens, vegan=False, vegetarian=False,
                               special=i.special)

            return LLMMenu(
                valid_from=ex.valid_from.isoformat(), valid_to=ex.valid_to.isoformat(), notes=None,
                days=[{"date": d.isoformat(), "closed": m.closed, "items": [item(i) for i in m.items]}
                      for d, m in ex.days.items()],
                weekly_items=[item(i) for i in ex.weekly_items],
            )
        return LLMRule(rule_yaml=self.rule_answers.pop(0), explanation="test")


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "profiles", tmp_path / "profiles")
    shutil.copytree(ROOT / "rules", tmp_path / "rules")
    # Franks without a rule → only the LLM can handle it
    p = tmp_path / "profiles/chains/ghk-burgenland.yml"
    p.write_text(p.read_text(encoding="utf-8").replace("rule: ghk-franks", "rule:"), encoding="utf-8")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("RULES_DIR", str(tmp_path / "rules"))
    get_settings.cache_clear()
    settings = get_settings()
    db.init_db(settings.db_url)
    store = ProfileStore(settings.profiles_dir, settings.rules_dir)
    pdf = PDF.read_bytes()
    sha = hashlib.sha256(pdf).hexdigest()
    (settings.files_dir / f"{sha}.pdf").write_bytes(pdf)
    with db.session() as s:
        doc = db.Document(source_id="ghk-burgenland", location_key="ghk-burgenland/franks", doc_id="weekly",
                          kind="weekly", url="https://example.org/franks.pdf", label="Menüplan Franks KW45",
                          sha256=sha, first_seen=dt.datetime(2026, 11, 1, 9), last_seen=dt.datetime(2026, 11, 1, 9))
        s.add(doc)
        s.flush()
        doc_id = doc.id
    yield settings, store, doc_id
    get_settings.cache_clear()


def test_llm_fallback_and_verified_suggestion(setup):
    settings, store, doc_id = setup
    # first proposal is broken, second is the real rule → the loop retries with feedback
    provider = FakeProvider(["type: grid\n", GOOD_RULE])
    result = Importer(settings, store, provider).process_document(doc_id)
    assert result.startswith("ok via llm"), result
    assert provider.calls == ["LLMMenu", "LLMRule", "LLMRule"]
    with db.session() as s:
        doc = s.get(db.Document, doc_id)
        assert doc.origin == "llm" and doc.error == "no rule configured"
        assert len(doc.items) > 10
        sug = s.scalars(select(db.RuleSuggestion)).one()
        assert sug.score == pytest.approx(1.0)
        assert "attempt 2" in sug.report


def test_without_llm_document_fails_cleanly(setup):
    settings, store, doc_id = setup
    result = Importer(settings, store, None).process_document(doc_id)
    assert result.startswith("failed")
    with db.session() as s:
        assert s.get(db.Document, doc_id).status == "failed"
