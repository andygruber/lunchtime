"""Single-date flyers (e.g. a buffet): the whole document becomes one special item."""

from __future__ import annotations

import re

from .common import Context, clean_lines, validate
from .model import Extraction, ExtractionError, Item
from .pdf import PdfDoc
from .rules import SingleRule
from .text import dedupe, detect_tags, find_dates, join_lines, parse_price, split_allergens, strip_price


def parse_single(doc: PdfDoc, rule: SingleRule, ctx: Context) -> Extraction:
    text = doc.text
    dates = find_dates(text, ctx.fetch_date)
    if not dates and ctx.label:
        dates = find_dates(ctx.label, ctx.fetch_date)
    if not dates:
        raise ExtractionError("no date found in flyer")
    date = dates[0]

    title = rule.title
    if rule.title_pattern and (m := re.search(rule.title_pattern, text, re.S)):
        title = re.sub(r"\s+", " ", m.group("title")).strip()

    price = parse_price(text, rule.price)
    lines, allergens = [], []
    for line in clean_lines(text.splitlines(), rule):
        if find_dates(line, ctx.fetch_date) and len(line) < 20:
            continue  # the date line itself
        line = strip_price(line, rule.price)
        line, found = split_allergens(line)
        allergens += found
        if line and title.lower() not in line.lower():
            lines.append(line)

    ex = Extraction(valid_from=date, valid_to=date)
    ex.day(date).items.append(
        Item(
            name=title,
            category=rule.category,
            description=" · ".join(join_lines(lines)) or None,
            price_cents=price,
            allergens=sorted(dedupe(allergens)),
            tags=detect_tags(title, *lines),
            special=True,
        )
    )
    return validate(ex, rule, ctx)
