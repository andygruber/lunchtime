"""Helpers shared by all rule types."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from .model import Extraction, ExtractionError, Item, StaleDocument
from .rules import CommonRule
from .text import (
    dedupe,
    detect_tags,
    find_week,
    is_allergen_token,
    is_noise,
    join_lines,
    parse_price,
    split_allergens,
    strip_letter_runs,
    strip_price,
)


@dataclass
class Context:
    fetch_date: dt.date
    label: str | None = None  # link text on the index page
    url: str | None = None


def clean_lines(texts: list[str], rule: CommonRule) -> list[str]:
    patterns = [re.compile(p) for p in rule.ignore_patterns()]
    out = []
    for t in texts:
        t = strip_letter_runs(re.sub(r"\s+", " ", t).strip())
        if not t or any(p.search(t) for p in patterns):
            continue
        if is_noise(t, rule.price):
            continue
        if rule.min_letters and sum(c.isalpha() for c in t) < rule.min_letters and not all(
            is_allergen_token(tok) for tok in t.split()
        ):
            continue
        out.append(t)
    return out


def build_item(
    lines: list[str],
    rule: CommonRule,
    category: str | None = None,
    description_lines: list[str] | None = None,
) -> Item | None:
    allergens: list[str] = []
    rest: list[str] = []
    price = None
    marked: list[str] = []  # a diet marker on a line of its own (small print next to a wrapped name)
    for marker in rule.tags:
        rx = re.compile(marker.match)
        if any(rx.fullmatch(line.strip()) for line in lines):
            lines = [line for line in lines if not rx.fullmatch(line.strip())]
            marked += [marker.tag] + (["vegetarian"] if marker.tag == "vegan" else [])
    for line in lines:
        if price is None and (p := parse_price(line, rule.price)) is not None:
            price = p
            line = strip_price(line, rule.price)
        text, found = split_allergens(line)
        allergens += found
        if text:
            rest.append(text)
    desc = None
    if description_lines:
        desc_rest = []
        for line in description_lines:
            if price is None and (p := parse_price(line, rule.price)) is not None:
                price = p
                line = strip_price(line, rule.price)
            text, found = split_allergens(line)
            allergens += found
            if text:
                desc_rest.append(text)
        desc = " ".join(join_lines(desc_rest)) or None
    parts = join_lines(rest)
    if not parts:
        return None
    name = "\n".join(parts)
    return Item(
        name=name,
        category=category,
        description=desc,
        price_cents=price,
        allergens=sorted(dedupe(allergens)),
        tags=dedupe(detect_tags(name, desc) + marked),
        special=bool(re.search(rule.special, name)),
    )


def weekly_items(text: str, rule: CommonRule) -> list[Item]:
    items = []
    for wp in rule.weekly:
        for m in re.finditer(wp.pattern, text, re.S):
            g = m.groupdict()
            name = re.sub(r"\s+", " ", g.get("name") or "").strip()
            if not name:
                continue
            name, allergens = split_allergens(name)
            if g.get("allergens"):
                allergens += re.findall(r"[A-Z]", g["allergens"])
            desc = re.sub(r"\s+", " ", g["description"]).strip() if g.get("description") else None
            price = parse_price("€ " + g["price"], None) if g.get("price") else None
            items.append(
                Item(
                    name=name,
                    category=g.get("category") or wp.category,
                    description=desc,
                    price_cents=price,
                    allergens=sorted(dedupe(allergens)),
                    tags=detect_tags(name, desc),
                    special=bool(re.search(rule.special, name)),
                )
            )
    return items


def resolve_week(doc_text: str, rule: CommonRule, ctx: Context) -> tuple[dt.date, dt.date]:
    """Dates come from the PDF content; link label and file name are only fallbacks."""
    week = find_week(doc_text, ctx.fetch_date, rule.date_range)
    if not week and ctx.label:
        week = find_week(ctx.label, ctx.fetch_date)
    if not week and ctx.url:
        week = find_week(unquote(urlparse(ctx.url).path.rsplit("/", 1)[-1]), ctx.fetch_date)
    if not week:
        raise ExtractionError("no date range found in document, link label or file name")
    return week


def validate(ex: Extraction, rule: CommonRule, ctx: Context) -> Extraction:
    v = rule.validate_
    if not ex.valid_from:
        raise ExtractionError("no date range")
    distance = abs((ex.valid_from - ctx.fetch_date).days)
    if distance > v.max_days_from_fetch:
        raise StaleDocument(
            f"menu week starts {ex.valid_from.isoformat()}, {distance} days away from fetch date "
            f"(limit {v.max_days_from_fetch}) – stale or mislabeled document?"
        )
    good = [d for d in ex.days.values() if d.closed or len(d.items) >= v.min_items_per_day]
    if len(good) < v.min_days:
        raise ExtractionError(
            f"only {len(good)} day(s) with ≥{v.min_items_per_day} item(s) found, {v.min_days} required"
        )
    return ex


def apply_category_prices(ex: Extraction, text: str, rule: CommonRule) -> None:
    if not rule.category_prices:
        return
    prices = {}
    for m in re.finditer(rule.category_prices, text):
        price = parse_price("€ " + m.group("price"))
        if price is not None:
            prices.setdefault(re.sub(r"\s+", " ", m.group("category")).strip().lower(), price)
    for item in [i for d in ex.days.values() for i in d.items] + ex.weekly_items:
        if item.price_cents is None and item.category and item.category.lower() in prices:
            item.price_cents = prices[item.category.lower()]
