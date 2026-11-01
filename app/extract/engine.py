"""Rule dispatch: YAML rules (grid/sections/single) and Python plugins, for PDFs, HTML pages and images."""

from __future__ import annotations

import importlib
import re
from dataclasses import replace
from typing import Any, Callable

from pydantic import ValidationError

from .common import Context, apply_category_prices
from .grid import parse_grid
from .html import render_html
from .model import Extraction, ExtractionError, Item, StaleDocument
from .ocr import ocr_image
from .pdf import Page, PdfDoc, group_lines, load_pdf
from .rules import CommonRule, GridRule, Rule, RuleDoc, SectionsRule, SingleRule
from .sections import parse_sections
from .single import parse_single
from .text import dedupe

PLUGIN_PREFIX = "python:"

PluginFunc = Callable[[PdfDoc, Context], Extraction]


def parse_rule(spec: dict[str, Any]) -> Rule:
    try:
        return RuleDoc(rule=spec).rule
    except ValidationError as e:
        raise ExtractionError(f"invalid rule: {e}") from e


def load_plugin(ref: str) -> PluginFunc:
    """'python:plugins.example:parse' → callable(doc, ctx) -> Extraction."""
    target = ref[len(PLUGIN_PREFIX):]
    module_name, _, func_name = target.partition(":")
    try:
        return getattr(importlib.import_module(module_name), func_name or "parse")
    except (ImportError, AttributeError) as e:
        raise ExtractionError(f"cannot load plugin {target}: {e}") from e


def load_document(data: bytes, media_type: str = "application/pdf", selector: str | None = None,
                  ocr: dict[str, Any] | None = None) -> PdfDoc:
    """Any supported document as positioned words. `ocr`: psm/threshold for images."""
    if media_type == "text/html":
        return render_html(data, selector)
    if media_type.startswith("image/"):
        return ocr_image(data, **(ocr or {}))
    try:
        return load_pdf(data)
    except Exception as e:  # noqa: BLE001 — pdfminer raises many types
        raise ExtractionError(f"cannot read PDF: {e}") from e


def _parse(rule: Rule, doc: PdfDoc, ctx: Context) -> Extraction:
    if isinstance(rule, GridRule):
        return parse_grid(doc, rule, ctx)
    if isinstance(rule, SectionsRule):
        return parse_sections(doc, rule, ctx)
    if isinstance(rule, SingleRule):
        return parse_single(doc, rule, ctx)
    raise ExtractionError(f"unsupported rule {rule!r}")


def split_weeks(doc: PdfDoc, pattern: str) -> list[PdfDoc]:
    """Cut a document at lines matching `pattern` (one segment per week); text before the first match is dropped."""
    rx = re.compile(pattern)
    segments: list[PdfDoc] = []
    for page in doc.pages:
        starts = [line.top - 0.1 for line in group_lines(page.words) if rx.search(line.text)]
        for i, top in enumerate(starts):
            bottom = starts[i + 1] if i + 1 < len(starts) else page.height + 1
            words = [w for w in page.words if top <= w.top < bottom]
            seps = [s for s in page.separators if top <= s < bottom]
            segments.append(PdfDoc([Page(page.width, page.height, words, seps, page.source)]))
    return segments


def run_rule(rule: Rule | str, doc: bytes | PdfDoc, ctx: Context) -> Extraction:
    if not isinstance(doc, PdfDoc):
        doc = load_document(doc)
    if isinstance(rule, str):
        return load_plugin(rule)(doc, ctx)
    ex = _run_weeks(rule, doc, ctx) if rule.week_split else _parse(rule, doc, ctx)
    postprocess(ex, doc.text, rule)
    return ex


def _run_weeks(rule: Rule, doc: PdfDoc, ctx: Context) -> Extraction:
    segments = split_weeks(doc, rule.week_split)
    if not segments:
        raise ExtractionError("week_split matched no line")
    merged = Extraction()
    errors, stale = [], 0
    for seg in segments:
        try:
            ex = _parse(rule, seg, ctx)
        except StaleDocument:
            stale += 1
            continue
        except ExtractionError as e:
            errors.append(str(e))
            continue
        open_days = [d for d, m in ex.days.items() if not m.closed]
        for item in ex.weekly_items:  # all-week dishes belong to this segment's week only
            for date in open_days:
                ex.days[date].items.append(_weekly_copy(item))
        merged.days.update(ex.days)
        merged.valid_from = min(d for d in (merged.valid_from, ex.valid_from) if d)
        merged.valid_to = max(d for d in (merged.valid_to, ex.valid_to) if d)
        merged.warnings += ex.warnings
    if not merged.days:
        if stale and not errors:
            raise StaleDocument("all weeks on the page are outside the accepted date range")
        raise ExtractionError("; ".join(errors) or "no week could be parsed")
    merged.warnings += [f"week skipped: {e}" for e in errors]
    return merged


def _weekly_copy(item: Item) -> Item:
    return replace(item, weekly=True, allergens=list(item.allergens), tags=list(item.tags))


def postprocess(ex: Extraction, text: str, rule: CommonRule) -> None:
    """Steps shared by all rule types: category prices, skipped categories, diet markers, notes."""
    apply_category_prices(ex, text, rule)
    if not rule.weekend:
        ex.days = {d: m for d, m in ex.days.items() if d.weekday() < 5}
    if rule.skip_categories:
        skip = re.compile(rule.skip_categories)
        for day in ex.days.values():
            day.items = [i for i in day.items if not (i.category and skip.search(i.category))]
        ex.weekly_items = [i for i in ex.weekly_items if not (i.category and skip.search(i.category))]
    items = [i for d in ex.days.values() for i in d.items] + ex.weekly_items
    for marker in rule.tags:
        rx = re.compile(marker.match)
        for item in items:
            if not (rx.search(item.name) or (item.description and rx.search(item.description))):
                continue
            item.tags = dedupe([*item.tags, marker.tag] + (["vegetarian"] if marker.tag == "vegan" else []))
            if marker.strip:
                item.name = "\n".join(p.strip() for p in rx.sub("", item.name).split("\n") if p.strip())
                if item.description:
                    item.description = rx.sub("", item.description).strip() or None
    for pattern in rule.notes:
        for m in re.finditer(pattern, text):
            note = re.sub(r"\s+", " ", m.groupdict().get("note") or m.group(0)).strip()
            if note and note not in ex.notes:
                ex.notes.append(note)
