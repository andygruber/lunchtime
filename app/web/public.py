"""Public pages: day view, restaurant selection, legal pages, preference endpoints."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse

from ..i18n import languages
from .common import DIETS, SORTS, THEMES, VIEWS, get_prefs, prefs_response, redirect, render
from .menus import dates_with_menus, day_view, neighbour_with_menu

router = APIRouter()


def _today(request: Request) -> dt.date:
    return request.app.state.importer.today()


def _all_locations(request: Request) -> list:
    return sorted(request.app.state.store.locations(),
                  key=lambda r: ((r.location.city or "~").lower(), r.location.name.lower()))


def _favourites(request: Request, prefs) -> list:
    store = request.app.state.store
    return [ref for key in prefs.restaurants if (ref := store.location(key))]


def _selected(request: Request, prefs) -> tuple[list, str]:
    """The favourites in the visitor's order, or all restaurants (by town, optionally favourites first).

    Without favourites all restaurants are shown, whatever the setting.
    """
    favourites = _favourites(request, prefs)
    if favourites and (prefs.view or "fav") == "fav":
        return favourites, "fav"
    refs = _all_locations(request)
    if prefs.fav_first:
        keys = set(prefs.restaurants)
        refs.sort(key=lambda r: r.key not in keys)  # stable: by town within both groups
    return refs, "all"


@router.get("/")
def home(request: Request, date: str | None = None):
    if date:
        return redirect(f"/d/{date}")
    return _day(request, _today(request))


@router.get("/d/{day}")
def day(request: Request, day: str):
    try:
        date = dt.date.fromisoformat(day)
    except ValueError:
        return redirect("/")
    if date == _today(request):
        return redirect("/")
    return _day(request, date)


def _day(request: Request, date: dt.date):
    prefs = get_prefs(request)
    today = _today(request)
    refs, view = _selected(request, prefs)
    keys = [r.key for r in refs]
    monday = date - dt.timedelta(days=date.weekday())
    week = [monday + dt.timedelta(days=i) for i in range(7)]
    available = dates_with_menus(keys, week[0], week[-1])
    # weekend days are only shown in the strip if someone serves food then
    strip = [d for d in week if d.weekday() < 5 or d in available or d == date]
    locations = day_view(refs, date, today, prefs.diet)
    any_menu = any(ld.has_any_menu for ld in locations)
    if view == "all":  # only restaurants with something to show for the day
        shown = [ld for ld in locations if ld.has_menu or ld.closed or (ld.fallback_pdfs and not prefs.diet)]
    else:
        shown = locations
    # prev/next skip weekend days unless someone serves food then
    nearby = dates_with_menus(keys, date - dt.timedelta(days=3), date + dt.timedelta(days=3))

    def step(delta: int) -> dt.date:
        d = date + dt.timedelta(days=delta)
        while d.weekday() >= 5 and d not in nearby:
            d += dt.timedelta(days=delta)
        return d

    return render(
        request,
        "day.html",
        date=date,
        today=today,
        prev_day=step(-1),
        next_day=step(1),
        strip=strip,
        available=available,
        locations=shown,
        view=view,
        has_favourites=bool(_favourites(request, prefs)),
        favourite_keys=set(prefs.restaurants),
        diet_hidden=sum(1 for ld in locations if ld.hidden and not ld.has_menu and ld not in shown),
        any_menu=any_menu,
        next_menu=None if any_menu else neighbour_with_menu(keys, date, forward=True),
        prev_menu=None if any_menu else neighbour_with_menu(keys, date, forward=False),
    )


@router.get("/restaurants")
def restaurants(request: Request, q: str = ""):
    prefs = get_prefs(request)
    selected = _favourites(request, prefs)
    all_refs = _all_locations(request)
    query = q.strip().lower()
    if query:
        all_refs = [
            r for r in all_refs
            if query in " ".join(filter(None, [r.location.name, r.location.city, r.location.address,
                                               r.source.name])).lower()
        ]
    return render(request, "restaurants.html", selected=selected, results=all_refs, q=q,
                  selected_keys=set(prefs.restaurants))


@router.post("/prefs/toggle")
def toggle(request: Request, key: str = Form(...), next: str = Form("/restaurants")):
    prefs = get_prefs(request)
    if key in prefs.restaurants:
        prefs.restaurants.remove(key)
    elif request.app.state.store.location(key):
        prefs.restaurants.append(key)
    prefs.ok = True
    return prefs_response(request, prefs, next, "/restaurants")


@router.post("/prefs/move")
def move(request: Request, key: str = Form(...), direction: str = Form(...), next: str = Form("/restaurants")):
    prefs = get_prefs(request)
    if key in prefs.restaurants:
        i = prefs.restaurants.index(key)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(prefs.restaurants):
            prefs.restaurants[i], prefs.restaurants[j] = prefs.restaurants[j], prefs.restaurants[i]
    return prefs_response(request, prefs, next, "/restaurants")


@router.post("/prefs/clear")
def clear(request: Request, next: str = Form("/restaurants")):
    prefs = get_prefs(request)
    prefs.restaurants = []
    return prefs_response(request, prefs, next, "/restaurants")


@router.post("/prefs/lang")
def set_lang(request: Request, lang: str = Form(...), next: str = Form("/")):
    prefs = get_prefs(request)
    if lang in {code for code, _ in languages()}:
        prefs.lang = lang
    return prefs_response(request, prefs, next)


@router.post("/prefs/theme")
def set_theme(request: Request, theme: str = Form(...), next: str = Form("/")):
    prefs = get_prefs(request)
    prefs.theme = theme if theme in THEMES else None  # "auto": follow the system
    return prefs_response(request, prefs, next)


@router.post("/prefs/view")
def set_view(request: Request, view: str | None = Form(None), sort: str | None = Form(None),
             fav_first: str | None = Form(None), diet: str | None = Form(None), next: str = Form("/")):
    """Display settings of the day view; every field is optional so each control can be its own form."""
    prefs = get_prefs(request)
    if view in VIEWS:
        prefs.view = view
    if sort in SORTS:
        prefs.sort = sort
    if fav_first is not None:
        prefs.fav_first = fav_first == "1"
    if diet is not None:
        prefs.diet = diet if diet in DIETS else None
    return prefs_response(request, prefs, next)


@router.post("/prefs/consent")
def consent(request: Request, next: str = Form("/")):
    prefs = get_prefs(request)
    prefs.ok = True
    return prefs_response(request, prefs, next)


@router.get("/allergene")
def allergens(request: Request):
    return render(request, "allergens.html")


@router.get("/impressum")
def impressum(request: Request):
    return render(request, "impressum.html")


@router.get("/datenschutz")
def privacy(request: Request):
    return render(request, "privacy.html")


@router.get("/healthz", include_in_schema=False)
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")
