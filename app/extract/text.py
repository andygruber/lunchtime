"""Text helpers: dates, prices, allergens, dish line joining and noise filtering."""

from __future__ import annotations

import datetime as dt
import re

WEEKDAYS = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
WEEKDAY_RE = re.compile(r"(?i)^(" + "|".join(WEEKDAYS) + r")\b")
WEEKDAYS_SHORT = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6,
                  "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
WEEKDAY_SHORT_RE = re.compile(r"(?i)^(" + "|".join(WEEKDAYS_SHORT) + r")\.?(?=[\s,:]|$)")


def match_weekday(text: str, mode: str = "full") -> tuple[int, int] | None:
    """(weekday index, end of match) if text starts with a weekday name. mode: full | short | both."""
    text = text.lstrip()
    if mode in ("full", "both") and (m := WEEKDAY_RE.match(text)):
        return WEEKDAYS[m.group(1).lower()], m.end()
    if mode in ("short", "both") and (m := WEEKDAY_SHORT_RE.match(text)):
        return WEEKDAYS_SHORT[m.group(1).lower()], m.end()
    return None

MONTHS = {
    "januar": 1, "jänner": 1, "jaenner": 1, "februar": 2, "feber": 2, "märz": 3, "maerz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7, "october": 10, "december": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
# 02.11.2026 | 2.11. | 19.10..2026 | 2. November 2026 | 2. November
DATE_RE = re.compile(
    r"(?<!\d)(?P<d>\d{1,2})(?P<sep>[./])\s?(?:(?P<m>\d{1,2})(?P=sep){1,2}|\s*(?P<mn>" + _MONTH_ALT + r")\b\.?)"
    r"(?:\s?(?P<y>\d{4})(?!\d)|(?P<y2>\d{2})(?![\d.:,/]))?",
    re.IGNORECASE,
)
KW_RE = re.compile(r"(?i)(?<![A-Za-z])KW[\s._]*(\d{1,2})(?!\d)")

ALLERGEN_LETTERS = "ABCDEFGHLMNOPR"
_ALLERGEN_TOKEN = re.compile(rf"^\(?[{ALLERGEN_LETTERS}]+(?:[.,/][{ALLERGEN_LETTERS}]+)*[.,]?\)?$")
# "(A, C, G)" and lowercase "(a,c,g,l)"; lowercase only with separators so "(vegan)" is never read as codes
_ALLERGEN_PAREN = re.compile(
    rf"\(\s*([{ALLERGEN_LETTERS}](?:\s*[,./]?\s*[{ALLERGEN_LETTERS}])*"
    rf"|[{ALLERGEN_LETTERS.lower()}](?:\s*[,./]\s*[{ALLERGEN_LETTERS.lower()}])*)\s*\)"
)

DEFAULT_PRICE_RE = re.compile(r"€\s*(\d{1,3}(?:\s?\d)?\s?[,.]\s?\d{2})")

CONNECTOR_END = {
    "mit", "und", "an", "auf", "in", "im", "vom", "von", "der", "dem", "den", "des", "oder", "zu", "aus",
    "nach", "dazu", "al", "alla", "la", "le", "&", "-", "–", "+", "|", "with", "and", "or", "of",
}

NEW_DISH_HINT = re.compile(r"(?i)suppe\b|bouillon|consomm")


def resolve_year(day: int, month: int, ref: dt.date) -> dt.date | None:
    """Pick the year that puts day.month closest to ref."""
    best = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            d = dt.date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((d - ref).days) < abs((best - ref).days):
            best = d
    return best


def _parse_match(m: re.Match, ref: dt.date) -> tuple[int, int, int | None] | None:
    day = int(m.group("d"))
    month = int(m.group("m")) if m.group("m") else MONTHS.get(m.group("mn").lower()) if m.group("mn") else None
    if not month or not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    year = int(m.group("y")) if m.group("y") else 2000 + int(m.group("y2")) if m.group("y2") else None
    return day, month, year


def find_dates(text: str, ref: dt.date) -> list[dt.date]:
    """All dates in text order. Missing years are taken from a following dated date or resolved around ref."""
    raw = []
    pos = 0
    while m := DATE_RE.search(text, pos):
        if p := _parse_match(m, ref):
            raw.append(p)
            pos = m.end()
        else:
            pos = m.start() + 1  # e.g. "KW.45. 02.11." must not swallow the real date
    out: list[dt.date] = []
    for i, (d, mo, y) in enumerate(raw):
        if y is None:
            # "2. November - 6. November 2026": borrow the year of the next date if it is close
            nxt = raw[i + 1] if i + 1 < len(raw) else None
            if nxt and nxt[2] is not None:
                y = nxt[2]
        try:
            out.append(dt.date(y, mo, d) if y else resolve_year(d, mo, ref))
        except ValueError:
            continue
    return [d for d in out if d is not None]


def monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def find_week(text: str, ref: dt.date, pattern: str | None = None) -> tuple[dt.date, dt.date] | None:
    """Find the menu's date range: explicit pattern, a range of two dates ≤ 7 days apart, a single date or KW."""
    if pattern:
        m = re.search(pattern, text, re.S)
        if m:
            a = find_dates(m.group("from"), ref)
            b = find_dates(m.group("to"), ref)
            if a and b:
                return a[0], b[0]
    dates = find_dates(text, ref)
    for a, b in zip(dates, dates[1:]):
        if 0 < (b - a).days <= 7:
            if (b - a).days < 4:  # consecutive day headings: extend to the last date of that week
                b = max(d for d in dates if 0 <= (d - a).days <= 6)
            return a, b
    if dates:
        return monday(dates[0]), monday(dates[0]) + dt.timedelta(days=4)
    m = KW_RE.search(text)
    if m:
        week = int(m.group(1))
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)] or [ref.year]
        candidates = []
        for y in {*years, ref.year - 1, ref.year, ref.year + 1}:
            try:
                candidates.append(dt.date.fromisocalendar(y, week, 1))
            except ValueError:
                pass
        if candidates:
            mon = min(candidates, key=lambda d: abs((d - ref).days))
            return mon, mon + dt.timedelta(days=4)
    return None


def parse_price(text: str, pattern: str | None = None) -> int | None:
    m = re.search(pattern, text) if pattern else DEFAULT_PRICE_RE.search(text)
    if not m:
        return None
    raw = re.sub(r"\s", "", m.group(1)).replace(".", ",")
    try:
        euros, cents = raw.split(",") if "," in raw else (raw, "0")
        return int(euros) * 100 + int(cents.ljust(2, "0")[:2])
    except ValueError:
        return None


def strip_price(text: str, pattern: str | None = None) -> str:
    rx = re.compile(pattern) if pattern else DEFAULT_PRICE_RE
    return rx.sub("", text).strip()


def is_allergen_token(token: str) -> bool:
    return bool(_ALLERGEN_TOKEN.match(token)) and token.strip("(),.") != ""


def split_allergens(line: str) -> tuple[str, list[str]]:
    """Split trailing allergen codes ('A,C,G', 'ACGL', '(A, C, G)', 'L') off a line."""
    found: list[str] = []
    for m in reversed(list(_ALLERGEN_PAREN.finditer(line))):  # "(a,c,g)" anywhere in the line
        found = re.findall(rf"[{ALLERGEN_LETTERS}]", m.group(1).upper()) + found
        line = (line[: m.start()].rstrip() + " " + line[m.end():].lstrip()).strip()
    tokens = line.split()
    while tokens and is_allergen_token(tokens[-1]):
        found = re.findall(rf"[{ALLERGEN_LETTERS}]", tokens.pop()) + found
    return " ".join(tokens), found


def dedupe(seq: list[str]) -> list[str]:
    out: list[str] = []
    for s in seq:
        if s not in out:
            out.append(s)
    return out


def is_noise(line: str, price_pattern: str | None = None) -> bool:
    """Lines made of scattered single letters (rotated/curved text) or stray numbers."""
    tokens = line.split()
    if not tokens:
        return True
    if all(is_allergen_token(t) or t in {",", "."} for t in tokens):
        return False  # "G L" — allergen codes set as single letters
    if not re.search(r"[A-Za-zÄÖÜäöüß]", line):
        return not (len(tokens) <= 2 and parse_price(line, price_pattern) is not None)
    letters = [t for t in tokens if re.search(r"[A-Za-zÄÖÜäöüß]", t)]
    if len(tokens) >= 2 and sum(len(t) for t in tokens) / len(tokens) <= 1.7:
        return True
    return len(letters) == 1 and len(tokens) == 1 and len(tokens[0]) == 1


# a lone participle/adjective ("Geschwenkte", "Überbackener", "Cremige") belongs to the next line
_LONE_ADJECTIVE = re.compile(r"^(?:(?:Ge|Über|Ver)\w+(?:t|en)(?:e|er|es|en)|\w+(?:ige|iger|iges|ische|ischer|isches))$")


CONNECTOR_START = {"mit", "dazu", "und", "an", "auf", "in", "im", "vom", "von", "oder", "with", "and"}


def strip_letter_runs(line: str) -> str:
    """Remove letter soup from curved/decorative text merged into a real line ("Brotsuppe e i")."""
    tokens = line.split()
    tiny = [len(t) <= 2 and (t.isalpha() and t.islower() or t == ":") for t in tokens]
    keep = [True] * len(tokens)
    i = 0
    while i < len(tokens):
        if tiny[i]:
            j = i
            while j < len(tokens) and tiny[j]:
                j += 1
            at_end = j == len(tokens)
            if j - i >= 3 or (at_end and j - i >= 2 and all(len(t) == 1 for t in tokens[i:j])):
                keep[i:j] = [False] * (j - i)
            i = j
        else:
            i += 1
    return " ".join(t for t, k in zip(tokens, keep) if k)


def _continues(prev: str, line: str) -> bool:
    if line[:1] in "„“\"‚‘'»«" and line[1:2].isupper():
        return False  # „Laci Pecsenye“ — a quoted dish name starts a new dish
    first = line[:1]
    if not first.isalpha() or first.islower():
        return True
    if line.split()[0].lower() in CONNECTOR_START:
        return True  # "Mit Schnittlauch", "Dazu Apfelmus"
    if _LONE_ADJECTIVE.match(prev.split("\n")[-1].strip()) and " " not in prev.strip():
        return True
    last_word = prev.split()[-1].lower() if prev.split() else ""
    if last_word in CONNECTOR_END or prev.endswith(("-", ",", "–", "&", "|")):
        return True
    return False


def join_lines(lines: list[str]) -> list[str]:
    """Join wrapped lines of a cell into dish parts.

    A line continues the previous dish if it starts lowercase / with punctuation or the previous line ends
    with a connector ('mit', 'und', '-', ...). Otherwise it starts a new dish part.
    """
    parts: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if parts and _continues(parts[-1], line):
            prev = parts[-1]
            if prev.endswith("-") and line[:1].islower():
                parts[-1] = prev[:-1] + line  # hyphenated word: "Knoblauchcreme-" + "suppe"
            elif prev.endswith("-"):
                parts[-1] = prev + line  # "Spinat-" + "Fetastrudel"
            else:
                parts[-1] = prev + " " + line
        else:
            parts.append(line)
    return dedupe([re.sub(r"\s+", " ", p).strip(" ,") for p in parts])


def detect_tags(*texts: str | None) -> list[str]:
    """Only explicit markers ("vegan", "(Vegan/Allergie)", "vegetarisch", "Veggie") — not "mit veganer Sauce"."""
    t = " ".join(x for x in texts if x)
    tags = []
    if re.search(r"(?i)\bvegan\b(?!e)", t):
        tags.append("vegan")
    if "vegan" in tags or re.search(r"(?i)\b(vegetarisch|vegetarian|veggie|veggi)\b", t):
        tags.append("vegetarian")
    return tags


def parse_heading(text: str, mode: str, ref: dt.date) -> tuple[int | None, dt.date | None, str] | None:
    """A day heading: weekday and/or date at the start of the line.

    Returns (weekday, date, rest of line) or None. "Montag, 26. Oktober" → (0, 2026-10-26, "").
    """
    text = text.strip()
    weekday, rest = None, text
    if wd := match_weekday(text, mode):
        weekday, rest = wd[0], text[wd[1]:]
    rest = rest.lstrip(" ,:-–")
    date = None
    m = DATE_RE.match(rest)
    if m and (p := _parse_match(m, ref)):
        d, mo, y = p
        try:
            date = dt.date(y, mo, d) if y else resolve_year(d, mo, ref)
        except ValueError:
            date = None
        rest = rest[m.end():].lstrip(" ,:-–")
    if weekday is None and date is None:
        return None
    return weekday, date, rest


def is_date_only(line: str, ref: dt.date) -> bool:
    return bool(find_dates(line, ref)) and not re.sub(r"[\d.\s/–-]", "", DATE_RE.sub("", line))
