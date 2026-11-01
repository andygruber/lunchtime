"""Shared web helpers: the single preferences cookie, rendering with translations, safe redirects."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..i18n import Translator, languages, negotiate

COOKIE = "lunchtime"
COOKIE_MAX_AGE = 400 * 24 * 3600  # browsers cap cookie lifetime at 400 days

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def map_url(location) -> str:
    """Link to OpenStreetMap (opened only when the visitor clicks it; nothing is embedded)."""
    if location.geo:
        return f"https://www.openstreetmap.org/?mlat={location.geo.lat}&mlon={location.geo.lon}#map=18/{location.geo.lat}/{location.geo.lon}"
    return "https://www.openstreetmap.org/search?query=" + quote_plus(location.address or location.city or location.name)


templates.env.globals["map_url"] = map_url


@dataclass
class Prefs:
    restaurants: list[str] = field(default_factory=list)
    lang: str | None = None
    ok: bool = False  # cookie notice acknowledged

    def encode(self) -> str:
        data = {"r": self.restaurants, "l": self.lang, "ok": int(self.ok)}
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, raw: str | None) -> "Prefs":
        if not raw:
            return cls()
        try:
            data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            restaurants = [r for r in data.get("r", []) if isinstance(r, str) and len(r) < 200][:100]
            lang = data.get("l") if isinstance(data.get("l"), str) else None
            return cls(restaurants=restaurants, lang=lang, ok=bool(data.get("ok")))
        except (ValueError, TypeError, AttributeError):
            return cls()


def get_prefs(request: Request) -> Prefs:
    return Prefs.decode(request.cookies.get(COOKIE))


def get_lang(request: Request, prefs: Prefs) -> str:
    codes = {code for code, _ in languages()}
    if prefs.lang in codes:
        return prefs.lang
    return negotiate(request.headers.get("accept-language"))


def save_prefs(request: Request, response, prefs: Prefs) -> None:
    response.set_cookie(
        COOKIE,
        prefs.encode(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow local redirects."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    return target


def redirect(target: str) -> RedirectResponse:
    return RedirectResponse(target, status_code=303)


def render(request: Request, template: str, status_code: int = 200, **ctx: Any):
    prefs = get_prefs(request)
    lang = get_lang(request, prefs)
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        template,
        {
            "t": Translator(lang),
            "lang": lang,
            "languages": languages(),
            "prefs": prefs,
            "site": settings.site,
            "path": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
            **ctx,
        },
        status_code=status_code,
    )
