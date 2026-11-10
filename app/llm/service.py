"""LLM fallback: extract a menu from a PDF, then propose a YAML rule that reproduces the result."""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML

from ..extract.common import Context
from ..extract.engine import parse_rule, run_rule
from ..extract.model import DayMenu, Extraction, ExtractionError
from ..extract.pdf import PdfDoc, layout_text
from ..extract.rules import RuleDoc
from .base import LLMError, Provider
from .schema import LLMMenu, LLMRule, to_extraction

log = logging.getLogger(__name__)

EXTRACT_SYSTEM = """You turn restaurant lunch menu documents (mostly Austrian/German weekly menu PDFs) into JSON.

- Copy dish texts as printed (keep the language, fix only broken hyphenation). Don't invent or translate dishes.
- One item per printed menu cell / dish line. If one cell holds several dishes of the same menu (e.g. soup and
  main course), keep them in one item with the dishes separated by a newline.
- category: the row/column label as printed (e.g. "Menü 1", "Suppe", "Tagessuppe", "Dessert"), or null.
- Dates: take them from the document. If the year is missing, use the year that puts the date closest to the
  fetch date. The weekday columns/headings determine the date of each day.
- Allergens: the letter codes printed next to a dish (A–R), uppercase, one letter per entry.
- Prices: euro cents, only if printed for that item or its category.
- Mark a day as closed (with the printed reason) if the document says so, e.g. "Ruhetag", "Feiertag".
- Dishes offered all week ("Wochenteller", "Spezialität der Woche") go into weekly_items.
- A flyer for a single date (e.g. a buffet) is one special item on that date, the dishes in description.
- Skip dinner ("Abendessen") rows, footers, opening hours, contact data and allergen legends.
"""

RULE_SYSTEM = """You write extraction rules for a deterministic menu-PDF parser. A rule is YAML matching the
JSON schema below. The parser:

- grid: finds a header line of weekday names (days_in_columns) or of category labels (days_in_rows), splits
  columns at whitespace channels, finds row labels in the left margin (configured `categories` regexes are
  matched in order against the margin words joined by single spaces; without categories, label blocks are
  detected automatically), splits rows at drawn lines / whitespace (row_split auto) or at label tops
  (row_split top), and stops at the first line matching end_marker.
- sections: weekday heading lines followed by dishes; text_columns splits the page into columns; item_split gap
  starts a new dish at larger vertical gaps, item_split line makes every line a dish; `lines` regexes can take
  category/name from a line.
- single: a one-date flyer becomes one special item.
- Common: allergen codes and "€ 9,50" prices are detected automatically; `weekly` regexes run over the whole
  text (lines joined with \\n) for all-week dishes; `category_prices` fills prices printed once per category;
  `ignore` drops matching lines; `skip_categories` drops categories (e.g. dinner).

Prefer the simplest rule that works; only add options the layout needs. Use single-quoted YAML strings for
regexes. Existing rules for other layouts, for reference:

{examples}

Rule JSON schema:
{schema}
"""


@dataclass
class Suggestion:
    rule_yaml: str
    score: float
    report: str
    attempts: list[str] = field(default_factory=list)


def describe_context(ctx: Context, location: str, kind: str) -> str:
    return (
        f"Restaurant: {location}\nDocument kind: {kind}\nLink text: {ctx.label or '-'}\n"
        f"URL: {ctx.url or '-'}\nFetch date: {ctx.fetch_date.isoformat()}"
    )


def extract_menu(provider: Provider, attachment: tuple[bytes, str] | None, doc: PdfDoc, ctx: Context,
                 location: str, kind: str) -> Extraction:
    text = layout_text(doc) if doc.pages else "(no text layer — read the attached document)"
    prompt = (
        f"{describe_context(ctx, location, kind)}\n\n"
        "Positioned text of the document (y = vertical position, x = horizontal position, s = font size, "
        f"b = bold; from OCR for images, so expect recognition errors):\n{text}\n\nExtract the menu."
    )
    menu = provider.complete(EXTRACT_SYSTEM, prompt, LLMMenu, attachment=attachment, effort="medium")
    try:
        return to_extraction(menu)
    except ValueError as e:
        raise LLMError(f"invalid dates in LLM output: {e}") from e


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", " ", s.lower()).strip()


def compare(expected: Extraction, actual: Extraction) -> tuple[float, list[str]]:
    """Share of expected dishes the rule reproduced (fuzzy by day), plus a readable diff for the LLM."""
    notes = []
    total = matched = 0
    for date in sorted(set(expected.days) | set(actual.days)):
        exp = [(_norm(i.name), i) for i in expected.days.get(date, DayMenu(date)).items]
        act = [_norm(i.name) for i in actual.days.get(date, DayMenu(date)).items]
        for norm, item in exp:
            total += 1
            best = max((difflib.SequenceMatcher(None, norm, a).ratio() for a in act), default=0.0)
            if best >= 0.8:
                matched += 1
            else:
                notes.append(f"{date}: missing or different: {item.category or '-'}: {item.name!r}")
        extra = len(act) - len(exp)
        if extra > 0:
            notes.append(f"{date}: rule produced {extra} more item(s) than expected")
    exp_weekly = {_norm(i.name) for i in expected.weekly_items}
    act_weekly = {_norm(i.name) for i in actual.weekly_items}
    total += len(exp_weekly)
    matched += len(exp_weekly & act_weekly)
    for w in exp_weekly - act_weekly:
        notes.append(f"weekly item missing: {w!r}")
    return (matched / total if total else 0.0), notes


def _examples(rules_dir: Path, limit: int = 6) -> str:
    parts = []
    for path in sorted(rules_dir.glob("*.yml"))[:limit]:
        parts.append(f"# --- {path.name}\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(parts)


def suggest_rule(
    provider: Provider,
    doc: PdfDoc,
    expected: Extraction,
    ctx: Context,
    rules_dir: Path,
    current_rule: str | None = None,
    current_error: str | None = None,
    max_attempts: int = 3,
    threshold: float = 0.9,
) -> Suggestion | None:
    schema = json.dumps(RuleDoc.model_json_schema(by_alias=True)["$defs"], ensure_ascii=False)
    system = RULE_SYSTEM.format(examples=_examples(rules_dir), schema=schema)
    base_prompt = (
        f"Fetch date: {ctx.fetch_date.isoformat()}\nLink text: {ctx.label or '-'}\n\n"
        f"Positioned text of the PDF:\n{layout_text(doc)}\n\n"
        f"Expected result (JSON):\n{json.dumps(expected.to_dict(), ensure_ascii=False)}\n"
    )
    if current_rule:
        base_prompt += f"\nThe current rule failed:\n{current_rule}\nError: {current_error}\n"
    feedback = ""
    best: Suggestion | None = None
    attempts: list[str] = []
    for attempt in range(1, max_attempts + 1):
        prompt = base_prompt + feedback + "\nWrite a rule that reproduces the expected result."
        try:
            answer = provider.complete(system, prompt, LLMRule, effort="high")
        except LLMError as e:
            log.warning("rule suggestion attempt %d failed: %s", attempt, e)
            break
        rule_yaml = re.sub(r"^```(?:ya?ml)?\s*|\s*```$", "", answer.rule_yaml.strip())
        attempts.append(rule_yaml)
        try:
            spec = YAML(typ="safe").load(rule_yaml)
            if not isinstance(spec, dict):
                raise ExtractionError("rule is not a mapping")
            spec.pop("id", None)
            actual = run_rule(parse_rule(spec), doc, ctx)
            score, notes = compare(expected, actual)
        except Exception as e:  # noqa: BLE001 — every failure is fed back to the model
            score, notes = 0.0, [f"rule failed: {e}"]
        report = f"attempt {attempt}: agreement {score:.0%}\n" + "\n".join(notes[:30])
        if best is None or score > best.score:
            best = Suggestion(rule_yaml=rule_yaml, score=score, report=report)
        if score >= threshold:
            break
        feedback = f"\nYour previous rule:\n{rule_yaml}\nResult: {report}\nFix the rule.\n"
    if best:
        best.attempts = attempts
    return best
