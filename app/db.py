"""SQLite storage: archived documents, extracted menu items, import runs and rule suggestions."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


# Renamed location keys ("source/location"), old -> new. Applied to the database at startup and to the
# favourites in visitors' cookies, so a rename never loses history or favourites.
# 2026-11-04: single-location sources moved to the short profile form (location id "main").
LOCATION_KEY_RENAMES: dict[str, str] = {
    f"{source}/{old}": f"{source}/main"
    for source, old in [
        ("altesbackhaus", "eisenstadt"), ("bsh-eisenstadt", "fred-sinowatz-haus"), ("diealm", "eisenstadt"),
        ("gasthaus-kiss", "eisenstadt"), ("haydnbraeu", "eisenstadt"), ("henis-imbiss", "eisenstadt"),
        ("karlwirt", "eisenstadt"), ("oscars", "eze"), ("rabina-hofpassage", "eisenstadt"),
        ("ruckendorfer", "eisenstadt"), ("schlosscafe", "eisenstadt"), ("theresianum", "eisenstadt"),
        ("timeout", "technologiezentrum"),
    ]
}


def rename_location_key(key: str) -> str:
    return LOCATION_KEY_RENAMES.get(key, key)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Document(Base):
    """One fetched file for one (location, document spec). The same URL+content is stored once per spec."""

    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    location_key: Mapped[str] = mapped_column(String(200), index=True)  # "source/location"
    doc_id: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))  # weekly | special | static
    url: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    media_type: Mapped[str | None] = mapped_column(String(50), default="application/pdf")
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    # pending | ok | failed | stale | superseded | static
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    origin: Mapped[str | None] = mapped_column(String(20))  # rule | plugin | llm
    error: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[dt.date | None] = mapped_column(Date, index=True)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)
    extraction_json: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)  # e.g. price lines, shown on the card

    items: Mapped[list["MenuItem"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    days: Mapped[list["DayStatus"]] = relationship(back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_document_spec", "location_key", "doc_id", "sha256"),)


class MenuItem(Base):
    __tablename__ = "menu_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    location_key: Mapped[str] = mapped_column(String(200))
    date: Mapped[dt.date] = mapped_column(Date)
    position: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price_cents: Mapped[int | None] = mapped_column(Integer)
    allergens: Mapped[str] = mapped_column(String(100), default="")  # "A,C,G"
    tags: Mapped[str] = mapped_column(String(100), default="")  # "vegan,vegetarian"
    special: Mapped[bool] = mapped_column(Boolean, default=False)
    weekly: Mapped[bool] = mapped_column(Boolean, default=False)

    document: Mapped[Document] = relationship(back_populates="items")

    __table_args__ = (Index("ix_menu_item_loc_date", "location_key", "date"),)


class DayStatus(Base):
    """A day marked closed in a document (holiday, 'Ruhetag', ...)."""

    __tablename__ = "day_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    location_key: Mapped[str] = mapped_column(String(200))
    date: Mapped[dt.date] = mapped_column(Date)
    closed: Mapped[str] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="days")

    __table_args__ = (Index("ix_day_status_loc_date", "location_key", "date"),)


class ImportRun(Base):
    __tablename__ = "import_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")  # schedule | manual | startup
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | warning | failed
    log: Mapped[str] = mapped_column(Text, default="")


class RuleSuggestion(Base):
    __tablename__ = "rule_suggestion"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    rule_yaml: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(default=0.0)  # agreement with the LLM extraction, 0..1
    report: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | accepted | dismissed
    model: Mapped[str | None] = mapped_column(String(100))

    document: Mapped[Document] = relationship()


_engine = None
_Session: sessionmaker[Session] | None = None


def init_db(url: str) -> None:
    global _engine, _Session
    _engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(_engine, "connect")
    def _pragmas(conn, _record):  # noqa: ANN001
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _rename_location_keys(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)


def _add_missing_columns(engine) -> None:
    """Additive schema changes for existing databases (new nullable columns only)."""
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
            for column in table.columns:
                if column.name not in existing:
                    ddl = column.type.compile(engine.dialect)
                    conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl}")


@contextmanager
def session() -> Iterator[Session]:
    assert _Session is not None, "init_db() not called"
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _rename_location_keys(engine) -> None:
    """Apply LOCATION_KEY_RENAMES to stored rows (idempotent: renamed keys no longer match)."""
    tables = [t.name for t in Base.metadata.sorted_tables if "location_key" in t.columns]
    with engine.begin() as conn:
        for old, new in LOCATION_KEY_RENAMES.items():
            for table in tables:
                conn.exec_driver_sql(f"UPDATE {table} SET location_key = ? WHERE location_key = ?", (new, old))
