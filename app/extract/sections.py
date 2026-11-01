"""Section layouts: day headings followed by dishes, optionally in several text columns.

Headings are weekdays ("MONTAG", "Montag, 26. Oktober", "MO") or, with a custom `day_heading`, any line
containing a date ("Gasthaus Kiss, 2. November 2026"). Works on PDFs, rendered HTML and OCR'd images.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from statistics import median

from .common import Context, build_item, clean_lines, resolve_week, validate, weekly_items
from .model import Extraction, ExtractionError, Item
from .pdf import Line, Page, PdfDoc, Word, group_lines
from .rules import SectionsRule
from .text import find_dates, is_date_only, match_weekday, parse_heading, parse_price, strip_price


@dataclass
class _Entry:
    line: Line
    text: str
    category: str | None
    weekly: bool
    ocr: bool = False  # from an image: font sizes are estimates


def parse_sections(doc: PdfDoc, rule: SectionsRule, ctx: Context) -> Extraction:
    ex = Extraction()
    heading_rx = re.compile(rule.day_heading) if rule.day_heading else None
    category_rx = re.compile(rule.category_heading) if rule.category_heading else None
    weekly_rx = re.compile(rule.weekly_heading) if rule.weekly_heading else None
    end_rx = re.compile(rule.end_marker) if rule.end_marker else None
    ignore_rx = [re.compile(p) for p in rule.ignore_patterns()]

    columns: list[tuple[list[Line], bool]] = []
    for page in doc.pages:
        columns += [(group_lines(ws), page.source == "image") for ws in _split_columns(page, rule.text_columns)]
    flow_text = "\n".join(line.text for col, _ in columns for line in col)

    # day key: an absolute date, or a weekday index resolved against the menu week below
    days: dict[dt.date | int, list[_Entry]] = {}
    weekly: list[_Entry] = []
    current: dt.date | int | None = None
    category: str | None = None
    in_weekly = False
    for col, ocr in columns:
        in_preamble = True
        for line in col:
            text = line.text.strip()
            if end_rx and end_rx.search(text):
                break
            if any(rx.search(text) for rx in ignore_rx):
                continue
            heading = _heading(text, rule, heading_rx, ctx)
            if heading is not None:
                current, rest = heading
                in_preamble, in_weekly, category = False, False, None
                days.setdefault(current, [])
                if rest and (rule.day_heading_inline or heading_rx):
                    days[current].append(_Entry(line, rest, category, False, ocr))
                continue
            if weekly_rx and (m := weekly_rx.search(text)):
                in_weekly, in_preamble = True, False
                category = (m.groupdict().get("category") or "").strip().rstrip(":").strip() or None
                continue
            if category_rx and (m := category_rx.search(text)):
                category = (m.groupdict().get("category") or text).strip().rstrip(":").strip()
                continue
            if in_weekly:
                weekly.append(_Entry(line, text, category, True, ocr))
                continue
            if current is None or (in_preamble and rule.column_preamble == "drop"):
                continue
            if is_date_only(text, ctx.fetch_date):
                continue  # "02.11.2026" below a weekday heading
            days[current].append(_Entry(line, text, category, False, ocr))

    if not days and not weekly:
        raise ExtractionError("no day headings found")

    try:
        week = resolve_week(doc.text, rule, ctx)
    except ExtractionError:
        dated = sorted(k for k in days if isinstance(k, dt.date))
        if not dated:
            raise
        week = (dated[0], dated[-1])
    ex.valid_from, ex.valid_to = week
    mon = week[0] - dt.timedelta(days=week[0].weekday())

    html = all(p.source == "html" for p in doc.pages)
    entries_all = [e for es in days.values() for e in es] + weekly
    threshold = _gap_threshold([e.line for e in entries_all], rule.gap_factor, html)
    for key, entries in days.items():
        date = key if isinstance(key, dt.date) else mon + dt.timedelta(days=key)
        day = ex.day(date)
        for item in _items(entries, rule, threshold):
            if isinstance(item, str):
                day.closed = item
            else:
                day.items.append(item)
        if day.items:
            day.closed = None
    ex.weekly_items = [i for i in _items(weekly, rule, threshold) if isinstance(i, Item)]
    ex.weekly_items += weekly_items(flow_text, rule)
    ex.days = {d: m for d, m in ex.days.items() if m.items or m.closed}
    if days and all(isinstance(k, dt.date) for k in days) and ex.days:
        # every heading carried its own date (e.g. a daily menu): valid exactly for those days
        ex.valid_from, ex.valid_to = min(ex.days), max(ex.days)
    elif ex.days:
        ex.valid_from, ex.valid_to = min(ex.valid_from, *ex.days), max(ex.valid_to, *ex.days)
    return validate(ex, rule, ctx)


def _heading(text: str, rule: SectionsRule, rx: re.Pattern | None, ctx: Context):
    """(day key, rest of line) if `text` is a day heading."""
    if rx:
        m = rx.search(text)
        if not m:
            return None
        part = m.groupdict().get("day") or m.group(0)
        dates = find_dates(part, ctx.fetch_date) or find_dates(text, ctx.fetch_date)
        rest = text[m.end():].strip(" ,:-–")
        if is_date_only(rest, ctx.fetch_date):
            rest = ""
        if dates:
            return dates[0], rest
        wd = match_weekday(part, "both") or next(
            (match_weekday(t, "both") for t in part.split() if match_weekday(t, "both")), None
        )
        return (wd[0], rest) if wd else None
    parsed = parse_heading(text, rule.weekdays, ctx.fetch_date)
    if parsed is None:
        return None
    weekday, date, rest = parsed
    if weekday is None:
        return None  # a bare date line is not a heading
    if rest and not rule.day_heading_inline:
        return None  # "Montag bis Freitag 11–14 Uhr" is not a heading
    return (date if date else weekday), rest


def _items(entries: list[_Entry], rule: SectionsRule, threshold: float) -> list[Item | str]:
    """Group entries into dishes. A closed marker returns its text instead of an item."""
    out: list[Item | str] = []
    groups: list[list[_Entry]] = []
    end_rx = re.compile(rule.item_end) if rule.item_end else None
    closed_group = False
    for e in entries:
        if rule.item_split == "end":
            if not groups or closed_group or e.category != groups[-1][-1].category:
                groups.append([e])
            else:
                groups[-1].append(e)
            closed_group = bool(end_rx and end_rx.search(e.text))
            continue
        new_group = (
            not groups
            or rule.item_split == "line"
            or e.category != groups[-1][-1].category
            # after a gap a new dish starts — unless the line is in a smaller font than the dish name
            # (description/price paragraphs below a heading-sized name on web pages)
            or (e.line.top - groups[-1][-1].line.bottom > threshold
                and e.line.size >= groups[-1][0].line.size - 0.75)
            or e.line is groups[-1][-1].line  # inline heading text and its line
        )
        if new_group:
            groups.append([e])
        else:
            groups[-1].append(e)
    for group in groups:
        texts = clean_lines([e.text for e in group], rule)
        if not texts:
            continue
        joined = " ".join(texts)
        if re.search(rule.closed, joined) and len(texts) <= 2:
            out.append(joined)
            continue
        item = _make_item(group, texts, rule)
        if item:
            out.append(item)
        elif (price := parse_price(joined, rule.price)) is not None and not strip_price(joined, rule.price).strip(" -–,.€"):
            # a price on its own line/paragraph belongs to the dish above
            prev = next((i for i in reversed(out) if isinstance(i, Item)), None)
            if prev and prev.price_cents is None:
                prev.price_cents = price
    return out


def _split_columns(page: Page, n: int) -> list[list[Word]]:
    if n == 1:
        return [page.words]
    narrow = [w for w in page.words if w.x1 - w.x0 < page.width * 0.4]
    cuts = []
    for j in range(1, n):
        target = page.width * j / n
        lo, hi = int(target - page.width * 0.15), int(target + page.width * 0.15)
        coverage = {x: 0 for x in range(lo, hi)}
        for w in narrow:
            for x in range(max(lo, int(w.x0)), min(hi, int(w.x1) + 1)):
                coverage[x] += 1
        least = min(coverage.values())
        xs = [x for x, c in coverage.items() if c == least]
        # middle of the widest run of least-covered positions
        runs, start = [], xs[0]
        for a, b in zip(xs, xs[1:] + [None]):
            if b != a + 1:
                runs.append((start, a))
                start = b
        best = max(runs, key=lambda r: r[1] - r[0])
        cuts.append((best[0] + best[1]) / 2)
    bounds = [float("-inf"), *cuts, float("inf")]
    return [[w for w in page.words if bounds[i] <= w.cx < bounds[i + 1]] for i in range(n)]


def _gap_threshold(lines: list[Line], factor: float, html: bool) -> float:
    if html:
        return 1.0  # rendered HTML: paragraphs are separated by real gaps, <br> lines by none
    gaps = sorted(b.top - a.bottom for a, b in zip(lines, lines[1:]) if b.top - a.bottom > 0)
    if not gaps:
        return float("inf")
    p25 = gaps[len(gaps) // 4]
    size = median(line.size for line in lines)
    return max(factor * p25, 0.35 * size)


def _make_item(group: list[_Entry], texts: list[str], rule: SectionsRule) -> Item | None:
    category = group[0].category or rule.default_category
    # bigger font = dish name, smaller font = description (e.g. "Rucola | Parmesan")
    sizes = [e.line.size for e in group]
    name_lines, desc_lines = texts, []
    if len(group) == len(texts) and max(sizes) - min(sizes) > 0.75 and not any(e.ocr for e in group):
        big = max(sizes)
        name_lines = [t for t, e in zip(texts, group) if e.line.size >= big - 0.75]
        desc_lines = [t for t, e in zip(texts, group) if e.line.size < big - 0.75]
        if not name_lines:
            name_lines, desc_lines = texts, []
    first = name_lines[0]
    price = None
    for lp in rule.lines:
        m = re.search(lp.match, first)
        if m:
            g = m.groupdict()
            category = g.get("category") or lp.category or category
            if g.get("price"):
                price = parse_price("€ " + g["price"])
            if g.get("name"):
                first = g["name"].strip()
            break
    item = build_item([first, *name_lines[1:]], rule, category, desc_lines)
    if item:
        item.weekly = group[0].weekly
        if price is not None:
            item.price_cents = price
    return item

