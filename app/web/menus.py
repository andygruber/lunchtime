"""Read model for the public day view."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select

from .. import db
from ..profiles import LocationRef


@dataclass
class Link:
    url: str
    title: str


@dataclass
class LocationDay:
    ref: LocationRef
    items: list[db.MenuItem] = field(default_factory=list)
    weekly: list[db.MenuItem] = field(default_factory=list)
    specials: list[db.MenuItem] = field(default_factory=list)
    closed: str | None = None
    pdfs: list[Link] = field(default_factory=list)
    fallback_pdfs: list[Link] = field(default_factory=list)  # documents we could not parse
    static_links: list[Link] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. prices, from the documents covering the day
    llm: bool = False
    ocr: bool = False  # read from an image
    hidden: int = 0  # dishes left out by the diet filter

    @property
    def has_menu(self) -> bool:
        return bool(self.items or self.weekly or self.specials)

    @property
    def has_any_menu(self) -> bool:
        """A menu before the diet filter was applied."""
        return self.has_menu or self.hidden > 0


_VEGAN_CATEGORY = re.compile(r"(?i)\bvegan")
_VEGETARIAN_CATEGORY = re.compile(r"(?i)vegetari|veggie")


def diet_tags(item: db.MenuItem) -> set[str]:
    """Diet tags found in the dish text at import, plus the category (menus list "Vegetarisch" as a row)."""
    tags = {t for t in (item.tags or "").split(",") if t}
    if item.category and _VEGAN_CATEGORY.search(item.category):
        tags |= {"vegan", "vegetarian"}
    elif item.category and _VEGETARIAN_CATEGORY.search(item.category):
        tags.add("vegetarian")
    return tags


def _key(item: db.MenuItem) -> tuple[str, str]:
    return ((item.category or "").lower(), re.sub(r"\W+", " ", item.name.lower()).strip())


_TIME_RANGE = re.compile(r"\d{1,2}[:.]\d{2}\s*(?:–|-|bis)\s*\d{1,2}[:.]\d{2}(?:\s*Uhr)?")
_DATE = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.(?:\d{2,4})?")


def special_parts(item: db.MenuItem) -> tuple[list[str], list[str]]:
    """A special's description as (times, dishes): flyers list their dishes line by line, joined with ' · '.

    The line with the date is reduced to its time range (the card already shows the day).
    """
    times, dishes = [], []
    for part in (item.description or "").split(" · "):
        part = part.strip()
        if not part:
            continue
        if m := _TIME_RANGE.search(part):
            times.append(m.group(0))
        elif not (_DATE.search(part) and len(_DATE.sub("", part).strip(" ,.-–")) < 12):
            dishes.append(part)  # a date-only line ("Mi., 04.11.2026") is dropped
    return times, dishes


def _replaces_menu(ref: LocationRef, doc: db.Document) -> bool:
    spec = next((d for d in ref.location.documents if d.id == doc.doc_id), None)
    return bool(spec and spec.replaces_menu)


def day_view(refs: list[LocationRef], date: dt.date, today: dt.date, diet: str | None = None) -> list[LocationDay]:
    """The menus of `refs` on `date`; with `diet` ("vegetarian"/"vegan") only the dishes marked as such."""
    keys = [r.key for r in refs]
    out = {r.key: LocationDay(ref=r) for r in refs}
    if not keys:
        return []
    with db.session() as s:
        items = s.scalars(
            select(db.MenuItem)
            .where(db.MenuItem.location_key.in_(keys), db.MenuItem.date == date)
            .order_by(db.MenuItem.location_key, db.MenuItem.weekly, db.MenuItem.document_id, db.MenuItem.position)
        ).all()
        seen: dict[str, set] = {k: set() for k in keys}
        # locations whose special replaces the regular dishes today (e.g. a buffet day)
        replaced = {i.location_key for i in items
                    if i.document.kind == "special" and _replaces_menu(out[i.location_key].ref, i.document)}
        for item in items:
            if item.location_key in replaced and item.document.kind != "special" and not item.weekly:
                continue
            k = _key(item)
            if k in seen[item.location_key]:
                continue  # same dish from two documents (e.g. two diet variants sharing soup and dessert)
            seen[item.location_key].add(k)
            ld = out[item.location_key]
            if diet and diet not in diet_tags(item):
                ld.hidden += 1
                continue
            if item.document.kind == "special":
                ld.specials.append(item)
            elif item.weekly:
                ld.weekly.append(item)
            else:
                ld.items.append(item)
            ld.llm |= item.document.origin == "llm"
            ld.ocr |= (item.document.media_type or "").startswith("image/") and item.document.origin != "llm"

        for st in s.scalars(
            select(db.DayStatus).where(db.DayStatus.location_key.in_(keys), db.DayStatus.date == date)
        ):
            out[st.location_key].closed = st.closed

        docs = s.scalars(select(db.Document).where(db.Document.location_key.in_(keys))
                         .order_by(db.Document.last_seen.desc())).all()
        recent = dt.datetime.combine(min(date, today) - dt.timedelta(days=10), dt.time())
        for doc in docs:
            ld = out[doc.location_key]
            title = doc.label or doc.url.rsplit("/", 1)[-1]
            if doc.kind == "static":
                spec = next((d for d in ld.ref.location.documents if d.id == doc.doc_id), None)
                ld.static_links.append(Link(doc.url, spec.title if spec and spec.title else title))
            elif doc.status == "ok" and doc.valid_from and doc.valid_to and doc.valid_from <= date <= doc.valid_to:
                if doc.url not in [l.url for l in ld.pdfs]:
                    ld.pdfs.append(Link(doc.url, title))
                for note in (doc.notes or "").splitlines():
                    if note not in ld.notes:
                        ld.notes.append(note)
            elif doc.status == "failed" and doc.last_seen >= recent and abs((date - today).days) <= 7:
                ld.fallback_pdfs.append(Link(doc.url, title))
    return [out[k] for k in keys]


def dates_with_menus(keys: list[str], start: dt.date, end: dt.date) -> set[dt.date]:
    if not keys:
        return set()
    with db.session() as s:
        return set(
            s.scalars(
                select(db.MenuItem.date)
                .where(db.MenuItem.location_key.in_(keys), db.MenuItem.date >= start, db.MenuItem.date <= end)
                .distinct()
            )
        )


def neighbour_with_menu(keys: list[str], date: dt.date, forward: bool) -> dt.date | None:
    if not keys:
        return None
    with db.session() as s:
        q = select(func.min(db.MenuItem.date) if forward else func.max(db.MenuItem.date)).where(
            db.MenuItem.location_key.in_(keys), (db.MenuItem.date > date) if forward else (db.MenuItem.date < date)
        )
        return s.scalar(q)
