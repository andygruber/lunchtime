"""Public pages: day view, restaurant selection, legal pages, preference endpoints."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse

from ..i18n import languages
from .common import get_prefs, redirect, render, safe_next, save_prefs
from .menus import dates_with_menus, day_view, neighbour_with_menu

router = APIRouter()


def _today(request: Request) -> dt.date:
    return request.app.state.importer.today()


def _selected(request: Request, prefs) -> tuple[list, bool]:
    """The user's restaurants in their order, or all restaurants if none are selected."""
    store = request.app.state.store
    refs = [ref for key in prefs.restaurants if (ref := store.location(key))]
    if refs:
        return refs, False
    all_refs = sorted(store.locations(), key=lambda r: ((r.location.city or "~").lower(), r.location.name.lower()))
    return all_refs, True


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
    refs, showing_all = _selected(request, prefs)
    keys = [r.key for r in refs]
    monday = date - dt.timedelta(days=date.weekday())
    week = [monday + dt.timedelta(days=i) for i in range(7)]
    available = dates_with_menus(keys, week[0], week[-1])
    # weekend days are only shown in the strip if someone serves food then
    strip = [d for d in week if d.weekday() < 5 or d in available or d == date]
    locations = day_view(refs, date, today)
    any_menu = any(ld.has_menu for ld in locations)
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
        locations=locations,
        showing_all=showing_all,
        any_menu=any_menu,
        next_menu=None if any_menu else neighbour_with_menu(keys, date, forward=True),
        prev_menu=None if any_menu else neighbour_with_menu(keys, date, forward=False),
    )


@router.get("/restaurants")
def restaurants(request: Request, q: str = ""):
    prefs = get_prefs(request)
    store = request.app.state.store
    selected = [ref for key in prefs.restaurants if (ref := store.location(key))]
    all_refs = sorted(store.locations(), key=lambda r: ((r.location.city or "~").lower(), r.location.name.lower()))
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
    response = redirect(safe_next(next, "/restaurants"))
    save_prefs(request, response, prefs)
    return response


@router.post("/prefs/move")
def move(request: Request, key: str = Form(...), direction: str = Form(...), next: str = Form("/restaurants")):
    prefs = get_prefs(request)
    if key in prefs.restaurants:
        i = prefs.restaurants.index(key)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(prefs.restaurants):
            prefs.restaurants[i], prefs.restaurants[j] = prefs.restaurants[j], prefs.restaurants[i]
    response = redirect(safe_next(next, "/restaurants"))
    save_prefs(request, response, prefs)
    return response


@router.post("/prefs/clear")
def clear(request: Request, next: str = Form("/restaurants")):
    prefs = get_prefs(request)
    prefs.restaurants = []
    response = redirect(safe_next(next, "/restaurants"))
    save_prefs(request, response, prefs)
    return response


@router.post("/prefs/lang")
def set_lang(request: Request, lang: str = Form(...), next: str = Form("/")):
    prefs = get_prefs(request)
    if lang in {code for code, _ in languages()}:
        prefs.lang = lang
    response = redirect(safe_next(next))
    save_prefs(request, response, prefs)
    return response


@router.post("/prefs/consent")
def consent(request: Request, next: str = Form("/")):
    prefs = get_prefs(request)
    prefs.ok = True
    response = redirect(safe_next(next))
    save_prefs(request, response, prefs)
    return response


@router.get("/impressum")
def impressum(request: Request):
    return render(request, "impressum.html")


@router.get("/datenschutz")
def privacy(request: Request):
    return render(request, "privacy.html")


@router.get("/healthz", include_in_schema=False)
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")
