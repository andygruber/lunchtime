"""Menus published as images: OCR with Tesseract into positioned words (same model as PDFs)."""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

from .model import ExtractionError
from .pdf import Page, PdfDoc, Word

MIN_WIDTH = 2000  # small web images are upscaled; Tesseract likes ~300 dpi
LANGUAGES = "deu+eng"


def available() -> bool:
    return shutil.which("tesseract") is not None


def _prepare(img: Image.Image, threshold: int | None) -> Image.Image:
    """Flatten transparency, upscale, keep the darkest colour channel (coloured backgrounds and red
    spell-check squiggles in screenshots become white/black consistently) and binarize."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")
    if img.width < MIN_WIDTH:
        img = img.resize((MIN_WIDTH, round(img.height * MIN_WIDTH / img.width)), Image.LANCZOS)
    r, g, b = img.split()
    gray = ImageChops.darker(ImageChops.darker(r, g), b)
    if threshold:
        gray = gray.point(lambda v: 255 if v > threshold else 0)
    return gray


def ocr_image(data: bytes, languages: str = LANGUAGES, psm: int = 6, threshold: int | None = 110) -> PdfDoc:
    if not available():
        raise ExtractionError("OCR not available: tesseract is not installed")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001 — any decoder error
        raise ExtractionError(f"cannot read image: {e}") from e
    img = _prepare(img, threshold)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "menu.png"
        img.save(path)
        try:
            out = subprocess.run(
                ["tesseract", str(path), "stdout", "-l", languages, "--psm", str(psm), "--dpi", "300", "tsv"],
                capture_output=True, text=True, timeout=120, check=True,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise ExtractionError(f"tesseract failed: {e}") from e

    rows = list(csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE))
    line_height: dict[tuple[str, str, str, str], float] = {}
    for r in rows:
        if r["level"] == "4":
            line_height[(r["page_num"], r["block_num"], r["par_num"], r["line_num"])] = float(r["height"])
    words = []
    for r in rows:
        text = (r.get("text") or "").strip()
        if r["level"] != "5" or not text or float(r["conf"]) < 0:
            continue
        x0, top, w, h = (float(r[k]) for k in ("left", "top", "width", "height"))
        size = line_height.get((r["page_num"], r["block_num"], r["par_num"], r["line_num"]), h) * 0.8
        words.append(Word(text, x0, x0 + w, top, top + h, size, False))
    if not words:
        raise ExtractionError("OCR found no text")
    page = Page(float(img.width), float(img.height), words, _rules(img), "image")
    return PdfDoc([page])


def _rules(img: Image.Image) -> list[float]:
    """y positions of horizontal table rules (solid or dashed lines across ≥35% of the width)."""
    w, h = img.size
    px = img.load()
    hits = [y for y in range(h) if sum(1 for x in range(0, w, 2) if px[x, y] < 150) * 2 > 0.35 * w]
    out: list[float] = []
    run: list[int] = []
    for y in hits + [-10]:
        if run and y - run[-1] > 2:
            out.append(sum(run) / len(run))
            run = []
        if y >= 0:
            run.append(y)
    return out
