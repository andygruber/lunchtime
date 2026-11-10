"""PDF access: words with font info, line grouping and horizontal separators (pdfplumber)."""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

_BOLD = re.compile(r"(?i)bold|black|heavy|semibold|demi")


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class Line:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w.bottom for w in self.words)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def size(self) -> float:
        return max(w.size for w in self.words)

    @property
    def bold(self) -> bool:
        chars = sum(len(w.text) for w in self.words)
        return sum(len(w.text) for w in self.words if w.bold) * 2 > chars


@dataclass
class Page:
    width: float
    height: float
    words: list[Word]
    separators: list[float] = field(default_factory=list)  # y positions of wide horizontal lines
    source: str = "pdf"  # pdf | html | image

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in group_lines(self.words))


@dataclass
class PdfDoc:
    pages: list[Page]

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)


def load_pdf(source: bytes | str | Path) -> PdfDoc:
    fp = io.BytesIO(source) if isinstance(source, bytes) else source
    pages = []
    with pdfplumber.open(fp) as pdf:
        for p in pdf.pages:
            # rotated text (labels, stamps) only produces letter soup
            upright = p.filter(lambda o: o.get("object_type") != "char" or o.get("upright", True))
            raw = upright.extract_words(extra_attrs=["fontname", "size"], keep_blank_chars=False, x_tolerance_ratio=0.15)
            words = [
                Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], float(w["size"]), bool(_BOLD.search(w["fontname"])))
                for w in raw
                if w["text"].strip()
            ]
            pages.append(Page(float(p.width), float(p.height), words, _separators(p)))
    return PdfDoc(pages)


def _separators(page) -> list[float]:
    """Merge collinear horizontal edges (table borders, cell rects) and keep those spanning ≥35% of the page."""
    rows: dict[int, list[tuple[float, float]]] = {}
    for e in page.edges:
        if e.get("orientation") != "h":
            continue
        rows.setdefault(round(float(e["top"]) / 1.5), []).append((float(e["x0"]), float(e["x1"])))
    out = []
    for key, spans in rows.items():
        spans.sort()
        covered, cur_a, cur_b = 0.0, None, None
        for a, b in spans:
            if cur_b is None or a > cur_b:
                if cur_b is not None:
                    covered += cur_b - cur_a
                cur_a, cur_b = a, b
            else:
                cur_b = max(cur_b, b)
        if cur_b is not None:
            covered += cur_b - cur_a
        if covered >= 0.35 * float(page.width):
            out.append(key * 1.5)
    return sorted(out)


def group_lines(words: list[Word]) -> list[Line]:
    """Cluster words into lines by vertical center; tolerant to mixed font sizes on one baseline."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.cy, w.x0)):
        if lines:
            cur = lines[-1]
            cy = sum(x.cy for x in cur) / len(cur)
            tol = 0.5 * min(w.size, min(x.size for x in cur))
            if abs(w.cy - cy) <= tol:
                cur.append(w)
                continue
        lines.append([w])
    return [Line(sorted(ws, key=lambda w: w.x0)) for ws in lines]


def joined_with_spans(words: list[Word]) -> tuple[str, list[tuple[int, int]]]:
    """Join words by spaces and return the char span of every word, to map regex matches back to words."""
    text, spans, pos = [], [], 0
    for w in words:
        spans.append((pos, pos + len(w.text)))
        text.append(w.text)
        pos += len(w.text) + 1
    return " ".join(text), spans


def words_in_span(words: list[Word], spans: list[tuple[int, int]], start: int, end: int) -> list[Word]:
    return [w for w, (a, b) in zip(words, spans) if a < end and b > start]


def layout_text(doc: PdfDoc, max_chars: int = 60000) -> str:
    """Positioned text for LLM prompts: one line per text line, chunks prefixed with their x position.

    Example: "y=205 s=9 | x=100 27.10.2026 | x=189 Gemüsecremesuppe | x=334 Kaiserschmarren mit"
    """
    out = []
    for n, page in enumerate(doc.pages, 1):
        out.append(f"=== page {n} (width {page.width:.0f}, height {page.height:.0f}) ===")
        for line in group_lines(page.words):
            chunks: list[list[Word]] = []
            for w in line.words:
                if chunks and w.x0 - chunks[-1][-1].x1 <= 0.8 * w.size:
                    chunks[-1].append(w)
                else:
                    chunks.append([w])
            parts = [f"x={c[0].x0:.0f}{' b' if all(w.bold for w in c) else ''} {' '.join(w.text for w in c)}" for c in chunks]
            out.append(f"y={line.top:.0f} s={line.size:.0f} | " + " | ".join(parts))
    text = "\n".join(out)
    return text[:max_chars]
