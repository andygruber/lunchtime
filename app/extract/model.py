"""Result types shared by rule-based extraction, plugins and the LLM fallback."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class Item:
    name: str  # dish parts separated by "\n" (e.g. soup and main course in one grid cell)
    category: str | None = None
    description: str | None = None
    price_cents: int | None = None
    allergens: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # "vegetarian", "vegan"
    special: bool = False
    weekly: bool = False  # valid all week (expanded into the days of a multi-week document)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "price_cents": self.price_cents,
            "allergens": self.allergens,
            "tags": self.tags,
            "special": self.special,
            "weekly": self.weekly,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        return cls(
            name=d["name"],
            category=d.get("category"),
            description=d.get("description"),
            price_cents=d.get("price_cents"),
            allergens=list(d.get("allergens") or []),
            tags=list(d.get("tags") or []),
            special=bool(d.get("special")),
            weekly=bool(d.get("weekly")),
        )


@dataclass
class DayMenu:
    date: dt.date
    items: list[Item] = field(default_factory=list)
    closed: str | None = None  # e.g. "Allerseelen Schulfrei"


@dataclass
class Extraction:
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    days: dict[dt.date, DayMenu] = field(default_factory=dict)
    weekly_items: list[Item] = field(default_factory=list)  # valid on every day of the week
    notes: list[str] = field(default_factory=list)  # shown on the card, e.g. prices
    warnings: list[str] = field(default_factory=list)

    def day(self, date: dt.date) -> DayMenu:
        if date not in self.days:
            self.days[date] = DayMenu(date)
        return self.days[date]

    def to_dict(self) -> dict:
        return {
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "days": [
                {
                    "date": d.date.isoformat(),
                    "closed": d.closed,
                    "items": [i.to_dict() for i in d.items],
                }
                for d in sorted(self.days.values(), key=lambda d: d.date)
            ],
            "weekly_items": [i.to_dict() for i in self.weekly_items],
            "notes": self.notes,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Extraction":
        ex = cls(
            valid_from=dt.date.fromisoformat(d["valid_from"]) if d.get("valid_from") else None,
            valid_to=dt.date.fromisoformat(d["valid_to"]) if d.get("valid_to") else None,
            weekly_items=[Item.from_dict(i) for i in d.get("weekly_items") or []],
            notes=list(d.get("notes") or []),
            warnings=list(d.get("warnings") or []),
        )
        for day in d.get("days") or []:
            date = dt.date.fromisoformat(day["date"])
            ex.days[date] = DayMenu(date, [Item.from_dict(i) for i in day.get("items") or []], day.get("closed"))
        return ex


class ExtractionError(Exception):
    """Raised when a rule cannot be applied or its result fails validation."""


class StaleDocument(ExtractionError):
    """The document's dates are far from the fetch date (old or mislabeled file); the LLM won't do better."""
