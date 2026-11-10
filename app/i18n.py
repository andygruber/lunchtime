"""Translations from app/translations/<lang>.yml. Adding a language = adding a file."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

DIR = Path(__file__).parent / "translations"
DEFAULT = "de"


@lru_cache
def catalogs() -> dict[str, dict[str, Any]]:
    y = YAML(typ="safe")
    return {p.stem: y.load(p.read_text(encoding="utf-8")) for p in sorted(DIR.glob("*.yml"))}


def languages() -> list[tuple[str, str]]:
    return [(code, cat.get("_name", code)) for code, cat in catalogs().items()]


def negotiate(accept_language: str | None) -> str:
    available = catalogs()
    ranked = []
    for part in (accept_language or "").split(","):
        lang, _, q = part.strip().partition(";q=")
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        if lang:
            ranked.append((weight, lang.split("-")[0].lower()))
    for _, lang in sorted(ranked, key=lambda r: -r[0]):
        if lang in available:
            return lang
    return DEFAULT


class Translator:
    def __init__(self, lang: str):
        cats = catalogs()
        self.lang = lang if lang in cats else DEFAULT
        self.cat = cats.get(self.lang, {})
        self.fallback = cats.get(DEFAULT, {})

    def _lookup(self, cat: dict[str, Any], key: str) -> Any:
        node: Any = cat
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def __call__(self, key: str, **kwargs: Any) -> Any:
        value = self._lookup(self.cat, key)
        if value is None:
            value = self._lookup(self.fallback, key)
        if value is None:
            return key
        if isinstance(value, str) and kwargs:
            return value.format(**kwargs)
        return value

    def weekday(self, d: dt.date, short: bool = False) -> str:
        return self("dates.weekdays_short" if short else "dates.weekdays")[d.weekday()]

    def date_long(self, d: dt.date) -> str:
        return self("dates.long", weekday=self.weekday(d), day=d.day, month=self("dates.months")[d.month - 1],
                    year=d.year)

    def date_short(self, d: dt.date) -> str:
        return self("dates.short", day=d.day, month=d.month)

    def price(self, cents: int | None) -> str:
        if cents is None:
            return ""
        euros, rest = divmod(cents, 100)
        sep = self("number.decimal")
        return f"€ {euros}{sep}{rest:02d}"
