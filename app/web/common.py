"""Shared web helpers: the single preferences cookie, rendering with translations, safe redirects."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..db import rename_location_key
from ..i18n import Translator, languages, negotiate
from .menus import diet_tags, special_parts

COOKIE = "lunchtime"
COOKIE_MAX_AGE = 400 * 24 * 3600  # browsers cap cookie lifetime at 400 days

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_static_versions: dict[str, tuple[float, str]] = {}


def static_url(path: str) -> str:
    """/static/<path>?v=<content hash>: browsers fetch a changed file at once instead of using a stale cache."""
    file = STATIC_DIR / path
    mtime = file.stat().st_mtime
    cached = _static_versions.get(path)
    if not cached or cached[0] != mtime:
        cached = _static_versions[path] = (mtime, hashlib.sha256(file.read_bytes()).hexdigest()[:10])
    return f"/static/{path}?v={cached[1]}"


def map_url(location) -> str:
    """Link to OpenStreetMap (opened only when the visitor clicks it; nothing is embedded)."""
    if location.geo:
        return f"https://www.openstreetmap.org/?mlat={location.geo.lat}&mlon={location.geo.lon}#map=18/{location.geo.lat}/{location.geo.lon}"
    return "https://www.openstreetmap.org/search?query=" + quote_plus(location.address or location.city or location.name)


templates.env.globals["map_url"] = map_url
templates.env.globals["static_url"] = static_url
templates.env.globals["diet_tags"] = diet_tags
templates.env.globals["special_parts"] = special_parts


THEMES = ("light", "dark")  # None = follow the system
VIEWS = ("fav", "all")  # None = favourites if there are any
SORTS = ("city", "distance")  # distance is sorted in the browser, the position never reaches the server
DIETS = ("vegetarian", "vegan")  # None = all dishes


def _choice(value: Any, choices: tuple[str, ...]) -> str | None:
    return value if value in choices else None


@dataclass
class Prefs:
    restaurants: list[str] = field(default_factory=list)  # favourites, in the visitor's order
    lang: str | None = None
    ok: bool = False  # cookie notice acknowledged
    theme: str | None = None
    view: str | None = None
    sort: str = "city"
    fav_first: bool = True
    diet: str | None = None

    def encode(self) -> str:
        data = {"r": self.restaurants, "l": self.lang, "ok": int(self.ok), "t": self.theme, "v": self.view,
                "s": self.sort, "ff": int(self.fav_first), "d": self.diet}
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, raw: str | None) -> "Prefs":
        if not raw:
            return cls()
        try:
            data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            restaurants = [r for r in data.get("r", []) if isinstance(r, str) and len(r) < 200][:100]
            restaurants = list(dict.fromkeys(rename_location_key(r) for r in restaurants))
            lang = data.get("l") if isinstance(data.get("l"), str) else None
            return cls(restaurants=restaurants, lang=lang, ok=bool(data.get("ok")),
                       theme=_choice(data.get("t"), THEMES), view=_choice(data.get("v"), VIEWS),
                       sort=_choice(data.get("s"), SORTS) or "city", fav_first=bool(data.get("ff", 1)),
                       diet=_choice(data.get("d"), DIETS))
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


def is_fetch(request: Request) -> bool:
    """Forms submitted by app.js in the background (the page updates itself, no redirect needed)."""
    return request.headers.get("x-requested-with") == "fetch"


def prefs_response(request: Request, prefs: Prefs, next: str | None, default: str = "/") -> Response:
    response = Response(status_code=204) if is_fetch(request) else redirect(safe_next(next, default))
    save_prefs(request, response, prefs)
    return response


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
