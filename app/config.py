"""Settings from environment variables and config/site.yml.

Site and Impressum values can come from either; environment variables (e.g. from .env) win, so a deployment
keeps its operator data out of the git checkout.
"""

from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from ruamel.yaml import YAML

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


class Impressum(BaseModel):
    owner: str = "[Name / Firma]"
    address: str = "[Straße Hausnummer, PLZ Ort, Österreich]"
    email: str = "[kontakt@example.org]"
    phone: str | None = None
    purpose: str = "[Unternehmensgegenstand / Zweck der Website]"
    authority: str | None = None  # Aufsichtsbehörde, Kammer, UID … if applicable
    media_line: str = "[Blattlinie: Information über Mittagsmenüs regionaler Restaurants]"


class SiteConfig(BaseModel):
    site_name: str = "Lunchtime"
    base_url: str | None = None
    impressum: Impressum = Impressum()
    hosting: str = "[Hosting-Anbieter, z. B. Oracle Cloud Infrastructure, Region]"
    source_url: str = "https://github.com/andygruber/lunchtime"  # "Source code" link in the footer

    def placeholders(self) -> list[str]:
        """Fields still showing a '[placeholder]' (must be replaced before going live)."""
        values = {"hosting": self.hosting, **{f"impressum.{k}": v for k, v in self.impressum.model_dump().items()}}
        return [k for k, v in values.items() if isinstance(v, str) and v.startswith("[")]


# environment variable -> SiteConfig field ("section.field" for the Impressum)
SITE_ENV = {
    "SITE_NAME": "site_name",
    "SITE_BASE_URL": "base_url",
    "SITE_HOSTING": "hosting",
    "SITE_SOURCE_URL": "source_url",
    **{f"IMPRESSUM_{name.upper()}": f"impressum.{name}" for name in Impressum.model_fields},
}


class Settings(BaseModel):
    data_dir: Path
    profiles_dir: Path
    rules_dir: Path
    config_dir: Path
    secret_key: str
    admin_password_hash: str | None
    timezone: str
    schedule: str
    scheduler_enabled: bool
    llm_provider: str | None
    llm_model: str | None
    llm_api_key: str | None
    llm_base_url: str | None
    site: SiteConfig

    @property
    def files_dir(self) -> Path:
        """Archived documents (PDFs, HTML pages, images), named by content hash."""
        return self.data_dir / "files"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'lunchtime.db'}"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_provider and self.llm_model and (self.llm_api_key or self.llm_base_url))


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or default).resolve()


def load_site(config_dir: Path, env: dict[str, str] | None = None) -> SiteConfig:
    """config/site.yml (optional), overridden by SITE_* / IMPRESSUM_* environment variables (empty = unset)."""
    env = os.environ if env is None else env
    path = config_dir / "site.yml"
    data: dict[str, Any] = {}
    if path.exists():
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
    for var, target in SITE_ENV.items():
        value = (env.get(var) or "").strip()
        if not value:
            continue
        section, _, field = target.rpartition(".")
        if section:
            data[section] = {**(data.get(section) or {}), field: value}
        else:
            data[field] = value
    return SiteConfig.model_validate(data)


@lru_cache
def get_settings() -> Settings:
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        secret = secrets.token_urlsafe(32)
        log.warning("SECRET_KEY not set – using a random key, admin sessions end on restart")
    provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower() or None
    api_key = os.environ.get("LLM_API_KEY") or (
        os.environ.get("ANTHROPIC_API_KEY") if provider == "anthropic" else os.environ.get("OPENAI_API_KEY")
    )
    config_dir = _env_path("CONFIG_DIR", ROOT / "config")
    s = Settings(
        data_dir=_env_path("DATA_DIR", ROOT / "data"),
        profiles_dir=_env_path("PROFILES_DIR", ROOT / "profiles"),
        rules_dir=_env_path("RULES_DIR", ROOT / "rules"),
        config_dir=config_dir,
        secret_key=secret,
        admin_password_hash=os.environ.get("ADMIN_PASSWORD_HASH") or None,
        timezone=os.environ.get("TZ") or "Europe/Vienna",
        # several cron expressions separated by ";" — default: hourly 6–12 on weekdays, Sunday evening
        schedule=os.environ.get("IMPORT_SCHEDULE") or "0 6-12 * * mon-fri; 0 18 * * sun",
        scheduler_enabled=(os.environ.get("SCHEDULER_ENABLED") or "true").lower() in ("1", "true", "yes"),
        llm_provider=provider,
        llm_model=os.environ.get("LLM_MODEL") or None,
        llm_api_key=api_key or None,
        llm_base_url=os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or None,
        site=load_site(config_dir),
    )
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.files_dir.mkdir(parents=True, exist_ok=True)
    return s
