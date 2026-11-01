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

    @property
    def has_menu(self) -> bool:
        return bool(self.items or self.weekly or self.specials)


def _key(item: db.MenuItem) -> tuple[str, str]:
    return ((item.category or "").lower(), re.sub(r"\W+", " ", item.name.lower()).strip())


def day_view(refs: list[LocationRef], date: dt.date, today: dt.date) -> list[LocationDay]:
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
        for item in items:
            k = _key(item)
            if k in seen[item.location_key]:
                continue  # same dish from two documents (e.g. two diet variants sharing soup and dessert)
            seen[item.location_key].add(k)
            ld = out[item.location_key]
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
