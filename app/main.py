"""FastAPI application: wiring of settings, storage, profiles, importer, scheduler and routes."""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import get_settings
from .importer.pipeline import Importer
from .importer.scheduler import ImportScheduler
from .llm.base import get_provider
from .profiles import ProfileStore
from .web import admin, public
from .web.common import templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lunchtime")


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(settings.db_url)
        store = ProfileStore(settings.profiles_dir, settings.rules_dir)
        for name, err in store.errors.items():
            log.error("profile error in %s: %s", name, err)
        provider = get_provider(settings)
        importer = Importer(settings, store, provider)
        scheduler = ImportScheduler(importer, settings.schedule, settings.timezone) if settings.scheduler_enabled else None
        app.state.settings, app.state.store, app.state.importer, app.state.scheduler = settings, store, importer, scheduler
        log.info("loaded %d sources, LLM fallback: %s", len(store.sources),
                 f"{provider.name}/{provider.model}" if provider else "off")
        if scheduler:
            scheduler.start()
        yield
        if scheduler:
            scheduler.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(public.router)
    app.include_router(admin.router)

    tz = ZoneInfo(settings.timezone)

    def localtime(value: dt.datetime | None) -> str:
        if value is None:
            return "–"
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(tz).strftime("%d.%m.%Y %H:%M")

    templates.env.filters["localtime"] = localtime

    @app.exception_handler(admin.NotAuthenticated)
    async def _login_redirect(request: Request, exc: admin.NotAuthenticated):
        return RedirectResponse(f"/admin/login?next={request.url.path}", status_code=303)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        return response

    return app


app = create_app()
