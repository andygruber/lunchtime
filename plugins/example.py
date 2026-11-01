"""Example Python parser plugin.

Reference it from a profile document with   rule: python:plugins.example:parse
A plugin receives the parsed PDF (words with positions, see app/extract/pdf.py) and the fetch context and
returns an Extraction. Use it only for layouts the YAML rules (grid/sections/single) cannot express.
"""

from __future__ import annotations

import datetime as dt

from app.extract.common import Context, build_item, clean_lines, resolve_week
from app.extract.model import Extraction, ExtractionError
from app.extract.pdf import PdfDoc
from app.extract.rules import CommonRule


def parse(doc: PdfDoc, ctx: Context) -> Extraction:
    rule = CommonRule()  # only used for the default ignore/price/allergen settings
    week = resolve_week(doc.text, rule, ctx)
    ex = Extraction(valid_from=week[0], valid_to=week[1])
    lines = clean_lines(doc.text.splitlines(), rule)
    if not lines:
        raise ExtractionError("empty document")
    # Example: put every line on Monday. A real plugin would locate days and dishes.
    monday = week[0] - dt.timedelta(days=week[0].weekday())
    for line in lines:
        if item := build_item([line], rule):
            ex.day(monday).items.append(item)
    return ex
