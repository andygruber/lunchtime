"""Grid layouts: weekdays along one axis, categories along the other.

Steps per page:
1. Find the header line (weekday names, or category names for days_in_rows) → column x-intervals.
2. Find row labels in the left margin (categories, or weekdays for days_in_rows) → row y-intervals,
   split at drawn separators or the largest vertical whitespace between two labels.
3. Assign every word below the header and above the end marker to a cell and turn cells into items.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from statistics import median

from .common import Context, build_item, clean_lines, resolve_week, validate, weekly_items
from .model import Extraction, ExtractionError
from .pdf import Page, PdfDoc, Word, group_lines, joined_with_spans, words_in_span
from .rules import GridRule
from .text import is_allergen_token, is_noise, match_weekday


@dataclass
class _Band:
    key: object  # weekday index (int) or category name (str)
    lo: float
    hi: float
    top: float = 0.0
    bottom: float = 0.0


def parse_grid(doc: PdfDoc, rule: GridRule, ctx: Context) -> Extraction:
    ex = Extraction()
    week = resolve_week(doc.text, rule, ctx)
    ex.valid_from, ex.valid_to = week
    mon = week[0] - dt.timedelta(days=week[0].weekday())
    found_any = False
    for page in doc.pages:
        try:
            cells = _parse_page(page, rule)
        except ExtractionError:
            if len(doc.pages) == 1:
                raise
            continue
        found_any = True
        for (weekday, category), lines in cells:
            day = ex.day(mon + dt.timedelta(days=weekday))
            whole = " ".join(clean_lines(lines, rule))
            if whole and re.search(rule.closed, whole):
                day.closed = day.closed or whole
                continue
            parts = [lines]
            if rule.item_separator:  # "dish A / oder / dish B" in one cell
                sep = re.compile(rule.item_separator)
                parts = [[]]
                for line in lines:
                    if sep.search(line.strip()):
                        parts.append([])
                    else:
                        parts[-1].append(line)
            for part in parts:
                texts = clean_lines(part, rule)
                joined = " ".join(texts)
                if not texts:
                    continue
                if re.search(rule.closed, joined):
                    day.closed = day.closed or joined
                    continue
                item = build_item(texts, rule, category)
                if item:
                    day.items.append(item)
        ex.weekly_items += weekly_items(page.text, rule)
    if not found_any:
        raise ExtractionError("no grid found on any page")
    for day in ex.days.values():
        if day.items:
            day.closed = None  # partially closed (e.g. only dinner) is still open
    ex.days = {d: m for d, m in ex.days.items() if m.items or m.closed}
    return validate(ex, rule, ctx)


def _parse_page(page: Page, rule: GridRule) -> list[tuple[tuple[int, str | None], list[str]]]:
    words = page.words
    if not words:
        raise ExtractionError("page has no text")
    size = median(w.size for w in words)
    days_cols = rule.orientation == "days_in_columns"

    header = _find_weekday_header(words, rule.weekdays) if days_cols else _find_category_header(words, rule)
    if len(header) < 2:
        raise ExtractionError("no header row with weekdays/categories found")
    header_bottom = max(b.bottom for b in header)
    end_y = page.height
    for line in group_lines([w for w in words if w.top > header_bottom]):
        if re.search(rule.end_marker, line.text):
            end_y = line.top
            break

    body = [w for w in words if w.top >= header_bottom - 1 and w.bottom <= end_y + 1]
    columns = _column_intervals(header, [w for w in body if w.top > header_bottom], rule.column_align, page.width)
    grid_left = columns[0].lo
    margin = sorted([w for w in body if w.x1 <= grid_left + 1], key=lambda w: (w.top, w.x0))
    content = [w for w in body if w.x1 > grid_left + 1 and w.cy > header_bottom]

    if rule.row_split == "separators":
        rows = _separator_rows(page.separators, content, header_bottom, end_y, rule)
    else:
        if days_cols:
            labels = _match_categories(margin, rule) if rule.categories else _auto_labels(margin, size)
        else:
            labels = [
                _Band(wd[0], 0, 0, w.top, w.bottom)
                for w in margin
                if (wd := match_weekday(w.text, rule.weekdays))
            ]
        if not labels:
            raise ExtractionError("no row labels found in the left margin")
        rows = _row_intervals(labels, content, page.separators, rule.row_split, header_bottom, end_y, size)

    cells: dict[tuple[int, int], list[Word]] = {}
    for w in content:
        ri = next((i for i, r in enumerate(rows) if r.lo <= w.cy < r.hi), None)
        ci = next((i for i, c in enumerate(columns) if c.lo <= w.cx < c.hi), None)
        if ri is not None and ci is not None:
            cells.setdefault((ri, ci), []).append(w)

    out = []
    for (ri, ci), ws in sorted(cells.items()):
        lines = [line.text for line in group_lines(ws)]
        if days_cols:
            out.append(((columns[ci].key, rows[ri].key), lines))
        else:
            out.append(((rows[ri].key, columns[ci].key), lines))
    return out


def _find_weekday_header(words: list[Word], mode: str) -> list[_Band]:
    best: list[_Band] = []
    for line in group_lines([w for w in words if match_weekday(w.text, mode)]):
        bands, seen = [], set()
        for w in line.words:
            idx = match_weekday(w.text, mode)[0]
            if idx not in seen:
                seen.add(idx)
                bands.append(_Band(idx, w.x0, w.x1, w.top, w.bottom))
        if len(bands) > len(best):
            best = bands
    return sorted(best, key=lambda b: b.lo)


def _find_category_header(words: list[Word], rule: GridRule) -> list[_Band]:
    best: list[_Band] = []
    for line in group_lines(words):
        text, spans = joined_with_spans(line.words)
        bands = []
        for cat in rule.categories:
            m = re.search(cat.match, text)
            if m:
                ws = words_in_span(line.words, spans, m.start(), m.end())
                bands.append(_Band(cat.name, min(w.x0 for w in ws), max(w.x1 for w in ws),
                                   min(w.top for w in ws), max(w.bottom for w in ws)))
        if len(bands) > len(best):
            best = bands
    return sorted(best, key=lambda b: b.lo)


def _column_intervals(header: list[_Band], below: list[Word], align: str, page_width: float) -> list[_Band]:
    if align == "left":
        bounds = [h.lo - 2 for h in header]
    elif align == "center":
        centers = [(h.lo + h.hi) / 2 for h in header]
        bounds = [centers[0] - (centers[1] - centers[0]) / 2] + [(a + b) / 2 for a, b in zip(centers, centers[1:])]
    else:
        # Split at the widest vertical whitespace channel between two header centers, right where the next
        # column's text starts (allergen codes trailing a dish belong to the left column, so they are ignored).
        # The first column starts after the rightmost channel left of the first header (margin labels | content).
        centers = [(h.lo + h.hi) / 2 for h in header]
        text = [w for w in below if not is_allergen_token(w.text)]
        first = _min_coverage_run(text, 0, header[0].lo, rightmost=True)
        bounds = [first - 1 if first is not None else header[0].lo - 2]
        for a, b in zip(centers, centers[1:]):
            end = _min_coverage_run(text, a, b)
            bounds.append(end - 1 if end is not None else (a + b) / 2)
    bounds.append(page_width)
    return [_Band(h.key, bounds[i], bounds[i + 1]) for i, h in enumerate(header)]


def _min_coverage_run(words: list[Word], lo: float, hi: float, rightmost: bool = False) -> float | None:
    """End of the widest (or rightmost) run of least-covered x positions in [lo, hi)."""
    lo_i, hi_i = int(lo), int(hi)
    if hi_i - lo_i < 2:
        return None
    coverage = [0] * (hi_i - lo_i)
    for w in words:
        for x in range(max(lo_i, int(w.x0)), min(hi_i, int(w.x1) + 1)):
            coverage[x - lo_i] += 1
    least = min(coverage)
    runs, start = [], None
    for i, c in enumerate(coverage + [None]):
        if c == least and start is None:
            start = i
        elif c != least and start is not None:
            runs.append((start, i))
            start = None
    if rightmost:
        runs = [r for r in runs if r[1] - r[0] >= 3] or runs
        run = runs[-1]
    else:
        run = max(runs, key=lambda r: r[1] - r[0])
    return lo_i + run[1]


def _match_categories(margin: list[Word], rule: GridRule) -> list[_Band]:
    """Match configured category labels in reading order against the margin text."""
    text, spans = joined_with_spans(margin)
    labels, pos = [], 0
    for cat in rule.categories:
        m = re.compile(cat.match).search(text, pos)
        if not m:
            continue
        ws = words_in_span(margin, spans, m.start(), m.end())
        if not ws:
            continue
        labels.append(_Band(cat.name, 0, 0, min(w.top for w in ws), max(w.bottom for w in ws)))
        pos = m.end()
    return labels


def _auto_labels(margin: list[Word], size: float) -> list[_Band]:
    clusters: list[list[Word]] = []
    for w in margin:
        if clusters and w.top - max(x.bottom for x in clusters[-1]) < 0.8 * size:
            clusters[-1].append(w)
        else:
            clusters.append([w])
    out = []
    for c in clusters:
        text = " ".join(w.text for w in sorted(c, key=lambda w: (round(w.top), w.x0)))
        if is_noise(text):
            continue
        out.append(_Band(text, 0, 0, min(w.top for w in c), max(w.bottom for w in c)))
    return out


def _row_intervals(labels: list[_Band], content: list[Word], separators: list[float], mode: str,
                   top: float, bottom: float, size: float) -> list[_Band]:
    labels = sorted(labels, key=lambda b: b.top)
    bounds = [top]
    for a, b in zip(labels, labels[1:]):
        if mode == "top":
            bounds.append(b.top - 0.3 * size)
            continue
        lo, hi = (a.top + a.bottom) / 2, (b.top + b.bottom) / 2
        # allergen-only lines trail their dish, so they belong to the upper row: ignore them for the gap
        text = [w for w in content if w.bottom > lo and w.top < hi and not is_allergen_token(w.text)]
        gap = _largest_gap(text, lo, hi)
        crossing = [s for s in separators if lo < s < hi
                    and not any(w.top + 0.5 < s < w.bottom - 0.5 for w in content)]
        if crossing:
            target = (gap[0] + gap[1]) / 2 if gap else (lo + hi) / 2
            bounds.append(min(crossing, key=lambda s: abs(s - target)))
        elif gap:
            bounds.append(gap[1] - 0.5)
        else:
            bounds.append((a.bottom + b.top) / 2)
    bounds.append(bottom)
    return [_Band(lbl.key, bounds[i], bounds[i + 1]) for i, lbl in enumerate(labels)]


def _largest_gap(words: list[Word], lo: float, hi: float) -> tuple[float, float] | None:
    spans = sorted((max(w.top, lo), min(w.bottom, hi)) for w in words)
    best, cur = None, lo
    for a, b in spans:
        if a > cur and (best is None or a - cur > best[1] - best[0]):
            best = (cur, a)
        cur = max(cur, b)
    if hi > cur and (best is None or hi - cur > best[1] - best[0]):
        best = (cur, hi)
    return best


def _separator_rows(separators: list[float], content: list[Word], top: float, bottom: float,
                    rule: GridRule) -> list[_Band]:
    """Bands between drawn lines that contain text, named by the configured categories in order."""
    cuts = [top] + [y for y in separators if top < y < bottom] + [bottom]
    bands = []
    for lo, hi in zip(cuts, cuts[1:]):
        letters = sum(sum(c.isalpha() for c in w.text) for w in content if lo <= w.cy < hi)
        if letters >= 3:
            bands.append((lo, hi))
    if not rule.categories:
        raise ExtractionError("row_split 'separators' needs categories (one name per row)")
    if not bands:
        raise ExtractionError("no table rows between separators")
    return [_Band(cat.name, lo, hi) for cat, (lo, hi) in zip(rule.categories, bands)]
