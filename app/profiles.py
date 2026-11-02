"""Restaurant profiles (profiles/*.yml) and reusable rules (rules/*.yml).

YAML files are the source of truth. The admin UI edits them in place; ruamel's round-trip mode keeps
comments and formatting intact.
"""

from __future__ import annotations

import io
import re
import threading
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML

from .extract.engine import PLUGIN_PREFIX, parse_rule
from .extract.model import ExtractionError
from .extract.rules import Rule

ID_RE = r"^[a-z0-9][a-z0-9-]*$"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinkMatch(_Base):
    label: str | None = Field(None, description="Regex matched against the link text (or image alt text)")
    url: str | None = Field(None, description="Regex matched against the absolute link/image URL")
    pdf_only: bool = Field(True, description="Only consider links whose URL path ends with .pdf")

    def matches(self, label: str, url: str) -> bool:
        if not self.label and not self.url:
            return False
        if self.pdf_only and not urlparse(url).path.lower().endswith(".pdf"):
            return False
        if self.label and not re.search(self.label, label):
            return False
        if self.url and not re.search(self.url, url):
            return False
        return True


class UrlRewrite(_Base):
    pattern: str
    replace: str


class OcrOptions(_Base):
    psm: int = Field(6, description="Tesseract page segmentation mode (6: one text block, 4: columns of text)")
    threshold: int | None = Field(110, description="Binarization threshold 0–255 (higher keeps thin fonts)")


class DocumentSpec(_Base):
    id: str = Field(pattern=ID_RE)
    kind: Literal["weekly", "special", "static"] = "weekly"
    title: str | None = Field(None, description="Shown for static links, e.g. 'À la carte'")
    target: Literal["link", "page", "image"] = Field(
        "link",
        description="link: a linked file (PDF) matched by `match`; page: the HTML page itself; "
        "image: images on the page (menus published as pictures, read via OCR)",
    )
    page: str | None = Field(None, description="Page to look on instead of the source's index_url")
    match: LinkMatch | None = None
    selector: str | None = Field(None, description="CSS selector limiting the page region (target page/image)")
    index: int | None = Field(None, description="Only the n-th matching image/link (0-based)")
    url_rewrite: UrlRewrite | None = Field(None, description="Regex rewrite of the document URL, e.g. bigger images")
    ocr: OcrOptions = Field(default_factory=OcrOptions, description="OCR settings for images")
    llm: bool = Field(True, description="Allow the LLM fallback for this document")
    replaces_menu: bool = Field(
        False,
        description="kind special: on its date the location's regular dishes are not shown "
        "(e.g. a weekly plan that only says 'Buffet' that day)",
    )
    rule: str | dict[str, Any] | None = Field(
        None,
        description="Rule id from rules/, inline rule (may 'extends' a rule id) or 'python:module:function'. "
        "Empty: LLM only.",
    )

    @model_validator(mode="after")
    def _check(self) -> "DocumentSpec":
        if self.target == "link" and self.match is None:
            raise ValueError(f"document {self.id}: target 'link' needs 'match'")
        return self


class Geo(_Base):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class StaticLink(_Base):
    title: str
    url: str


class Location(_Base):
    id: str = Field(pattern=ID_RE)
    name: str
    city: str | None = None
    address: str | None = Field(None, description="Street, postcode and town, e.g. 'Hauptstraße 30, 7000 Eisenstadt'")
    geo: Geo | None = Field(None, description="Coordinates (fill with: python -m app.cli geocode)")
    phone: str | None = None
    website: str | None = None
    hours: list[str] = Field(default_factory=list, description="Opening hours as display lines")
    info: str | None = Field(None, description="Short note shown on the card, e.g. menu prices")
    links: list[StaticLink] = Field(default_factory=list, description="Fixed links, e.g. à la carte page")
    documents: list[DocumentSpec] = Field(default_factory=list)


class Source(_Base):
    id: str = Field(pattern=ID_RE)
    name: str
    index_url: str
    enabled: bool = True
    schedule: str | None = Field(None, description="Cron expression (minute hour day month weekday)")
    user_agent: str | None = Field(
        None,
        description="Empty: identify as LunchtimeBot. 'browser': send ordinary browser headers (for hosts that "
        "block every non-browser client). Anything else: a custom User-Agent string.",
    )
    locations: list[Location]

    @field_validator("locations")
    @classmethod
    def unique_ids(cls, v: list[Location]) -> list[Location]:
        ids = [loc.id for loc in v]
        if len(ids) != len(set(ids)):
            raise ValueError("location ids must be unique within a source")
        return v


class LocationRef(BaseModel):
    """A location with its source, as used by the web UI and importer."""

    source: Source
    location: Location

    @property
    def key(self) -> str:
        return f"{self.source.id}/{self.location.id}"


def _yaml_rt() -> YAML:
    y = YAML()  # round-trip: keeps comments and order
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 120
    return y


class ProfileStore:
    """Loads and validates profiles and rules; reloadable at runtime."""

    def __init__(self, profiles_dir: Path, rules_dir: Path):
        self.profiles_dir = profiles_dir
        self.rules_dir = rules_dir
        self._lock = threading.Lock()
        self.sources: dict[str, Source] = {}
        self.source_files: dict[str, Path] = {}
        self.rules: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, str] = {}  # file name -> error
        self.reload()

    # --- loading -----------------------------------------------------------------------------------------

    def reload(self) -> None:
        sources, files, rules, errors = {}, {}, {}, {}
        safe = YAML(typ="safe")
        for path in sorted(self.rules_dir.glob("*.yml")):
            try:
                data = safe.load(path.read_text(encoding="utf-8")) or {}
                rule_id = data.get("id") or path.stem
                parse_rule(data)
                rules[rule_id] = data
            except Exception as e:  # noqa: BLE001 — report any broken file in the admin UI
                errors[f"rules/{path.name}"] = str(e)
        for path in sorted(self.profiles_dir.glob("*.yml")):
            try:
                data = safe.load(path.read_text(encoding="utf-8")) or {}
                src = Source.model_validate(data)
                if src.id in sources:
                    raise ValueError(f"duplicate source id {src.id}")
                for loc in src.locations:
                    for doc in loc.documents:
                        if doc.kind != "static" and doc.rule is not None:
                            self._resolve(doc.rule, rules)
                sources[src.id] = src
                files[src.id] = path
            except (ValidationError, ValueError, ExtractionError) as e:
                errors[f"profiles/{path.name}"] = str(e)
            except Exception as e:  # noqa: BLE001
                errors[f"profiles/{path.name}"] = f"{type(e).__name__}: {e}"
        with self._lock:
            self.sources, self.source_files, self.rules, self.errors = sources, files, rules, errors

    # --- lookups -----------------------------------------------------------------------------------------

    def locations(self) -> list[LocationRef]:
        return [
            LocationRef(source=s, location=loc)
            for s in self.sources.values()
            if s.enabled
            for loc in s.locations
        ]

    def location(self, key: str) -> LocationRef | None:
        source_id, _, loc_id = key.partition("/")
        src = self.sources.get(source_id)
        if not src:
            return None
        loc = next((loc for loc in src.locations if loc.id == loc_id), None)
        return LocationRef(source=src, location=loc) if loc else None

    def document(self, location_key: str, doc_id: str) -> DocumentSpec | None:
        ref = self.location(location_key)
        if not ref:
            return None
        return next((d for d in ref.location.documents if d.id == doc_id), None)

    def resolve_rule(self, spec: str | dict[str, Any] | None) -> Rule | str | None:
        return None if spec is None else self._resolve(spec, self.rules)

    @staticmethod
    def _resolve(spec: str | dict[str, Any], rules: dict[str, dict[str, Any]]) -> Rule | str:
        if isinstance(spec, str):
            if spec.startswith(PLUGIN_PREFIX):
                return spec
            if spec not in rules:
                raise ExtractionError(f"unknown rule '{spec}'")
            return parse_rule(rules[spec])
        data = dict(spec)
        base = data.pop("extends", None)
        if base:
            if base not in rules:
                raise ExtractionError(f"unknown rule '{base}' in extends")
            data = {**rules[base], **data}
        return parse_rule(data)

    # --- editing (admin UI) ------------------------------------------------------------------------------

    def files(self) -> list[str]:
        return [f"profiles/{p.name}" for p in sorted(self.profiles_dir.glob("*.yml"))] + [
            f"rules/{p.name}" for p in sorted(self.rules_dir.glob("*.yml"))
        ]

    def path_for(self, name: str) -> Path:
        folder, _, filename = name.partition("/")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*\.yml", filename):
            raise ValueError("invalid file name")
        base = {"profiles": self.profiles_dir, "rules": self.rules_dir}.get(folder)
        if base is None:
            raise ValueError("invalid folder")
        return base / filename

    def validate_text(self, name: str, text: str) -> None:
        data = YAML(typ="safe").load(text) or {}
        if name.startswith("rules/"):
            parse_rule(data)
        else:
            src = Source.model_validate(data)
            rules = dict(self.rules)
            for loc in src.locations:
                for doc in loc.documents:
                    if doc.kind != "static" and doc.rule is not None:
                        self._resolve(doc.rule, rules)

    def write_file(self, name: str, text: str) -> None:
        self.validate_text(name, text)
        self.path_for(name).write_text(text, encoding="utf-8")
        self.reload()

    def delete_file(self, name: str) -> None:
        self.path_for(name).unlink(missing_ok=True)
        self.reload()

    def save_rule(self, rule_id: str, rule: dict[str, Any], header: str | None = None) -> str:
        data = {"id": rule_id, **{k: v for k, v in rule.items() if k != "id"}}
        parse_rule(data)
        buf = io.StringIO()
        if header:
            buf.write("".join(f"# {line}\n" for line in header.splitlines()))
        _yaml_rt().dump(data, buf)
        name = f"rules/{rule_id}.yml"
        self.path_for(name).write_text(buf.getvalue(), encoding="utf-8")
        self.reload()
        return name

    def update_location(self, source_id: str, location_id: str, values: dict[str, Any],
                        comment: str | None = None) -> None:
        """Set fields of a location in its profile file (comments and formatting are kept).
        New keys are inserted after 'address' (or 'city'); dict values are written in flow style."""
        from ruamel.yaml.comments import CommentedMap

        path = self.source_files[source_id]
        y = _yaml_rt()
        data = y.load(path.read_text(encoding="utf-8"))
        loc = next(loc for loc in data["locations"] if loc["id"] == location_id)
        for key, value in values.items():
            if isinstance(value, dict):
                cm = CommentedMap(value)
                cm.fa.set_flow_style()
                value = cm
            if key in loc:
                loc[key] = value
            else:
                anchor = next((k for k in ("address", "city", "name") if k in loc), None)
                pos = list(loc.keys()).index(anchor) + 1 if anchor else len(loc)
                loc.insert(pos, key, value)
            if comment:
                loc.yaml_add_eol_comment(comment, key)
        buf = io.StringIO()
        y.dump(data, buf)
        self.write_file(f"profiles/{path.name}", buf.getvalue())

    def set_document_rule(self, source_id: str, location_id: str, doc_id: str, rule: str) -> None:
        """Point a document at a rule id, keeping the rest of the profile file (and comments) untouched."""
        path = self.source_files[source_id]
        y = _yaml_rt()
        data = y.load(path.read_text(encoding="utf-8"))
        for loc in data["locations"]:
            if loc["id"] == location_id:
                for doc in loc["documents"]:
                    if doc["id"] == doc_id:
                        doc["rule"] = rule
                        buf = io.StringIO()
                        y.dump(data, buf)
                        self.write_file(f"profiles/{path.name}", buf.getvalue())
                        return
        raise KeyError(f"{source_id}/{location_id}/{doc_id} not found")
