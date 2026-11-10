"""Declarative extraction rules.

Rules are written in YAML (see rules/*.yml and profiles/**/*.yml) and validated by these models.
The same models provide the JSON schema handed to the LLM when it proposes a new rule.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_IGNORE = [
    r"(?i)änderungen vorbehalten",
    r"(?i)guten appetit",
    r"(?i)^allergen",
    r"(?i)codex",
]
DEFAULT_END_MARKERS = r"(?i)änderungen vorbehalten|allergeninformation|guten appetit|die ganze woche"
DEFAULT_CLOSED = r"(?i)\b(ruhetag|schulfrei|geschlossen|feiertag|betriebsurlaub|closed)\b"
DEFAULT_SPECIAL = r"(?i)(?<!vom )\bbuffet\b"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Category(_Base):
    """A labelled row (or column) of a grid, or a line pattern of a section layout."""

    match: str = Field(description="Regex matched against the label text (words joined by single spaces)")
    name: str = Field(description="Category name shown to users, e.g. 'Menü 1' or 'Suppe'")


class WeeklyPattern(_Base):
    """Regex applied to the whole document text (DOTALL); each match becomes an item valid all week."""

    pattern: str = Field(
        description="Regex with named groups: name (required), category, description, price, allergens"
    )
    category: str | None = None


class LinePattern(_Base):
    """Regex applied to each item's first line in a section layout to split off category/name."""

    match: str = Field(description="Regex with optional named groups: category, name, price, allergens")
    category: str | None = Field(None, description="Fixed category if the regex has no 'category' group")


class TagMarker(_Base):
    """A marker in the dish text that sets a diet tag, e.g. ' V+' → vegan."""

    match: str = Field(description="Regex matched against the dish name")
    tag: Literal["vegan", "vegetarian"]
    strip: bool = Field(True, description="Remove the marker from the name")


class Validation(_Base):
    min_days: int = Field(3, description="Minimum number of days with items (closed days count)")
    min_items_per_day: int = Field(1, description="Minimum items on each non-closed day that has items")
    max_days_from_fetch: int = Field(60, description="Reject documents whose week is further away from the fetch date")
    min_weekly_items: int = Field(
        0,
        description="All-week dishes expected (e.g. 'Spezialität der Woche'); fewer only give a warning, "
        "the menu is still used",
    )


class CommonRule(_Base):
    id: str | None = None
    description: str | None = None
    date_range: str | None = Field(
        None,
        description="Regex with named groups 'from' and 'to' (dd.mm[.yyyy]); default: automatic date detection",
    )
    ignore: list[str] = Field(default_factory=list, description="Regexes; matching lines are dropped")
    use_default_ignore: bool = True
    closed: str = Field(DEFAULT_CLOSED, description="Regex marking a cell/day as closed (holiday etc.)")
    special: str = Field(DEFAULT_SPECIAL, description="Regex marking an item as special (e.g. buffet)")
    weekly: list[WeeklyPattern] = Field(default_factory=list)
    skip_categories: str | None = Field(None, description="Regex; items whose category matches are dropped (e.g. dinner)")
    price: str | None = Field(None, description="Regex with one group capturing a price like 9,50; default: '€ 9,50'")
    category_prices: str | None = Field(
        None,
        description="Regex over the whole text with named groups 'category' and 'price' (e.g. 'Menü 1: € 9,50'); "
        "sets the price of items in that category that have none",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Regexes over the whole text; matches (or their 'note' group) are shown on the card, "
        "e.g. price lines like 'Tagesteller € 10,00'",
    )
    tags: list[TagMarker] = Field(default_factory=list)
    week_split: str | None = Field(
        None, description="Regex for a line starting a new week, for pages listing several weeks"
    )
    min_letters: int = Field(0, description="Drop lines with fewer letters (OCR debris); allergen codes are kept")
    weekend: bool = Field(True, description="Keep Saturday/Sunday entries")
    weekdays: Literal["full", "short", "both"] = Field(
        "full", description="Weekday names: full ('Montag'), short ('MO', 'Di.') or both"
    )
    validate_: Validation = Field(default_factory=Validation, alias="validate")

    def ignore_patterns(self) -> list[str]:
        return (DEFAULT_IGNORE if self.use_default_ignore else []) + self.ignore


class GridRule(CommonRule):
    """A table: weekdays along one axis, categories along the other."""

    type: Literal["grid"]
    orientation: Literal["days_in_columns", "days_in_rows"] = "days_in_columns"
    categories: list[Category] = Field(
        default_factory=list,
        description="Ordered category labels (row labels for days_in_columns, column headers for days_in_rows). "
        "Empty: detect row labels automatically from the left margin.",
    )
    column_align: Literal["auto", "left", "center"] = "auto"
    row_split: Literal["auto", "top", "separators"] = Field(
        "auto",
        description="auto: split rows at drawn lines or the largest vertical gap between labels; "
        "top: each row starts at the top of its label; separators: rows are the bands between drawn "
        "lines (e.g. table rules in images), named by `categories` in order (labels are not read)",
    )
    end_marker: str = Field(DEFAULT_END_MARKERS, description="Regex; the grid ends above the first matching line")
    item_separator: str | None = Field(
        None, description="Regex for a line inside a cell that separates two dishes (e.g. '^oder$')"
    )


class SectionsRule(CommonRule):
    """Running text: weekday headings followed by dishes, optionally in several text columns."""

    type: Literal["sections"]
    text_columns: int = Field(1, ge=1, le=4)
    column_preamble: Literal["drop", "continue"] = Field(
        "drop", description="Text in a column before its first day heading: drop it or append it to the previous day"
    )
    day_heading: str | None = Field(None, description="Regex for a day heading line; default: line starting with a weekday")
    item_split: Literal["gap", "line", "end", "join"] = Field(
        "gap",
        description="gap: items are separated by larger vertical gaps; line: every line is an item; "
        "end: an item ends after a line matching item_end (e.g. its price line); "
        "join: like line, but a wrapped line (lowercase start, after a hyphen or 'mit') continues the item",
    )
    item_end: str | None = Field(None, description="Regex for the last line of an item (item_split: end)")
    gap_factor: float = Field(1.5, description="A gap larger than gap_factor × typical line spacing starts a new item")
    day_heading_inline: bool = Field(
        False, description="Text after the weekday/date on a heading line is the day's first dish"
    )
    category_heading: str | None = Field(
        None, description="Regex for a line naming the category of the following dishes (e.g. 'Teller 1:')"
    )
    weekly_heading: str | None = Field(
        None, description="Regex for a line after which dishes are valid all week (e.g. 'Wochenkarte')"
    )
    lines: list[LinePattern] = Field(default_factory=list)
    default_category: str | None = None
    end_marker: str | None = Field(None, description="Regex; text after the first matching line is ignored")


class SingleRule(CommonRule):
    """A flyer for one date (e.g. a buffet) that becomes a single special item."""

    type: Literal["single"]
    title: str = Field("Buffet", description="Item name if title_pattern does not match")
    title_pattern: str | None = Field(None, description="Regex with a 'title' group to take the name from the text")
    category: str | None = "Special"
    special: str = ".*"  # everything from a single-date flyer is special
    validate_: Validation = Field(default_factory=lambda: Validation(min_days=1), alias="validate")


Rule = Annotated[Union[GridRule, SectionsRule, SingleRule], Field(discriminator="type")]


class RuleDoc(BaseModel):
    """Wrapper used to validate a rule loaded from YAML."""

    rule: Rule
