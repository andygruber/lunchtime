"""Render an HTML page into positioned words, so the PDF rules (grid/sections) work on web pages too.

Block elements become lines (paragraphs get extra vertical space, headings a bigger "font"), <b>/<strong>
and headings are bold, and tables are laid out as real grids with row separators.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

from .pdf import Page, PdfDoc, Word

BASE_SIZE = 12.0
HEADING_SIZES = {"h1": 26.0, "h2": 22.0, "h3": 18.0, "h4": 16.0, "h5": 14.0, "h6": 13.0}
BLOCK = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "ul",
}
DROP = {"script", "style", "noscript", "svg", "template", "iframe", "button", "select", "input", "textarea", "head"}
BOLD = {"b", "strong", "th"}
CHAR_WIDTH = 0.55  # average glyph width relative to the font size
MAX_CELL_CHARS = 60

Token = tuple[str, float, bool]  # text, size, bold


class _Flow:
    """Collects inline tokens into lines; `gaps` holds the extra space before each line."""

    def __init__(self) -> None:
        self.lines: list[list[Token]] = []
        self.gaps: list[float] = []
        self.current: list[Token] = []
        self.pending_gap = 0.0
        self.blocks: list[tuple[int, object]] = []  # (line index, table layout) for tables

    def word(self, text: str, size: float, bold: bool) -> None:
        self.current.append((text, size, bold))

    def newline(self, gap: float = 0.0) -> None:
        if self.current:
            self.lines.append(self.current)
            self.gaps.append(self.pending_gap)
            self.current = []
            self.pending_gap = 0.0
        self.pending_gap = max(self.pending_gap, gap)


def _walk(node, flow: _Flow, size: float, bold: bool) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if child.__class__.__name__ in ("Comment", "Doctype", "Declaration", "ProcessingInstruction"):
                continue
            for w in str(child).split():
                flow.word(w, size, bold)
            continue
        if not isinstance(child, Tag) or child.name in DROP:
            continue
        name = child.name
        if name == "br":
            flow.newline()
        elif name == "table":
            flow.newline(size * 0.6)
            flow.blocks.append((len(flow.lines), _table(child)))
            flow.newline(size * 0.6)
        elif name in HEADING_SIZES:
            flow.newline(size * 0.8)
            _walk(child, flow, HEADING_SIZES[name], True)
            flow.newline(size * 0.4)
        elif name in BLOCK:
            flow.newline(size * 0.5 if name in ("p", "li", "dd", "dt") else 0.0)
            _walk(child, flow, size, bold or name in BOLD)
            flow.newline(size * 0.5 if name in ("p", "li", "dd", "dt") else 0.0)
        else:
            _walk(child, flow, size, bold or name in BOLD)


def _cell_lines(cell: Tag) -> list[list[Token]]:
    flow = _Flow()
    _walk(cell, flow, BASE_SIZE, cell.name == "th")
    flow.newline()
    lines = flow.lines
    out = []
    for line in lines:  # wrap very long cell lines so columns stay readable
        chunk: list[Token] = []
        length = 0
        for tok in line:
            if chunk and length + len(tok[0]) > MAX_CELL_CHARS:
                out.append(chunk)
                chunk, length = [], 0
            chunk.append(tok)
            length += len(tok[0]) + 1
        if chunk:
            out.append(chunk)
    return out


def _table(table: Tag) -> list[list[tuple[int, int, list[list[Token]]]]]:
    """Rows of (column index, colspan, lines). Nested tables are flattened into their cell."""
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        col, row = 0, []
        for cell in tr.find_all(["td", "th"], recursive=False):
            span = int(cell.get("colspan", 1)) if str(cell.get("colspan", "1")).isdigit() else 1
            row.append((col, span, _cell_lines(cell)))
            col += span
        if row:
            rows.append(row)
    return rows


def render_html(html: str | bytes, selector: str | None = None) -> PdfDoc:
    soup = BeautifulSoup(html, "html.parser")
    roots = soup.select(selector) if selector else [soup.body or soup]
    flow = _Flow()
    for root in roots:
        _walk(root, flow, BASE_SIZE, False)
        flow.newline(BASE_SIZE)
    flow.newline()

    words: list[Word] = []
    separators: list[float] = []
    y = 0.0
    width = 0.0

    def place_line(tokens: list[Token], x: float, top: float) -> tuple[float, float]:
        height = max(t[1] for t in tokens)
        for text, size, bold in tokens:
            w = len(text) * size * CHAR_WIDTH
            words.append(Word(text, x, x + w, top + (height - size), top + height, size, bold))
            x += w + size * 0.3
        return x, height

    tables = dict(flow.blocks)
    for i, (tokens, gap) in enumerate(zip(flow.lines + [[]], flow.gaps + [0.0])):
        if i in tables:
            y = _place_table(tables.pop(i), y, words, separators, place_line)
        if not tokens:
            continue
        y += gap
        x_end, height = place_line(tokens, 0.0, y)
        width = max(width, x_end)
        y += height * 1.25
    for rows in tables.values():  # tables at the very end
        y = _place_table(rows, y, words, separators, place_line)
    width = max([width] + [w.x1 for w in words])
    page = Page(width + 20, y + 20, words, separators)
    page.source = "html"
    return PdfDoc([page])


def _place_table(rows, y: float, words, separators, place_line) -> float:
    ncols = max(c + span for row in rows for c, span, _ in row)
    widths = [0.0] * ncols
    for row in rows:
        for col, span, lines in row:
            if span == 1:
                need = max((sum(len(t[0]) + 1 for t in line) for line in lines), default=0) * BASE_SIZE * CHAR_WIDTH
                widths[col] = max(widths[col], need)
    xs = [0.0]
    for w in widths:
        xs.append(xs[-1] + max(w, BASE_SIZE) + 3 * BASE_SIZE)  # generous gutters keep columns apart
    for row in rows:
        separators.append(y)
        top = y + 0.4 * BASE_SIZE
        bottom = top
        for col, span, lines in row:
            ly = top
            for line in lines:
                _, h = place_line(line, xs[col], ly)
                ly += h * 1.25
            bottom = max(bottom, ly)
        y = bottom + 0.4 * BASE_SIZE
    separators.append(y)
    return y + BASE_SIZE


def page_text(html: str | bytes, selector: str | None = None) -> str:
    """Normalized visible text of the (selected) page — used to detect content changes."""
    doc = render_html(html, selector)
    return re.sub(r"\s+", " ", doc.text).strip()
