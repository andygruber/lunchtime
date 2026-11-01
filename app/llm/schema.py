"""Structured output schemas for the LLM and conversion to/from the extraction model."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from ..extract.model import Extraction, Item
from ..extract.text import dedupe, detect_tags


class LLMItem(BaseModel):
    category: str | None = Field(description="Row/column label as printed, e.g. 'Menü 1', 'Suppe', 'Dessert'")
    name: str = Field(description="Dish text as printed; several dishes of one menu separated by a newline")
    description: str | None = Field(description="Side dishes/description printed in smaller type, if separate")
    price_cents: int | None = Field(description="Price in euro cents if printed for this item, else null")
    allergens: list[str] = Field(description="Allergen letter codes (A–R) printed for this item")
    vegan: bool = Field(description="Explicitly marked vegan")
    vegetarian: bool = Field(description="Explicitly marked vegetarian")
    special: bool = Field(description="Buffet, theme day or other one-off special")


class LLMDay(BaseModel):
    date: str = Field(description="ISO date YYYY-MM-DD")
    closed: str | None = Field(description="Reason if the kitchen is closed that day (holiday, Ruhetag), else null")
    items: list[LLMItem]


class LLMMenu(BaseModel):
    valid_from: str = Field(description="ISO date of the first day the menu covers")
    valid_to: str = Field(description="ISO date of the last day the menu covers")
    days: list[LLMDay]
    weekly_items: list[LLMItem] = Field(description="Dishes offered on every day of the week (e.g. 'Wochenteller')")
    notes: str | None = Field(description="Problems or uncertainties, else null")


class LLMRule(BaseModel):
    rule_yaml: str = Field(description="The complete rule as YAML (one mapping, no code fences)")
    explanation: str = Field(description="One or two sentences on how the rule maps the layout")


def to_extraction(menu: LLMMenu) -> Extraction:
    def item(i: LLMItem) -> Item:
        tags = detect_tags(i.name, i.description)
        if i.vegan:
            tags = dedupe([*tags, "vegan", "vegetarian"])
        elif i.vegetarian:
            tags = dedupe([*tags, "vegetarian"])
        return Item(
            name=i.name.strip(),
            category=(i.category or "").strip() or None,
            description=(i.description or "").strip() or None,
            price_cents=i.price_cents,
            allergens=sorted(dedupe([a.strip().upper() for a in i.allergens if a.strip()])),
            tags=tags,
            special=i.special,
        )

    ex = Extraction(
        valid_from=dt.date.fromisoformat(menu.valid_from),
        valid_to=dt.date.fromisoformat(menu.valid_to),
        weekly_items=[item(i) for i in menu.weekly_items],
        warnings=[menu.notes] if menu.notes else [],
    )
    for d in menu.days:
        day = ex.day(dt.date.fromisoformat(d.date))
        day.closed = d.closed
        day.items = [item(i) for i in d.items if i.name.strip()]
    ex.days = {k: v for k, v in ex.days.items() if v.items or v.closed}
    return ex
