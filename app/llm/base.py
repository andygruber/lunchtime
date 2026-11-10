"""LLM provider interface and factory. The app works without any provider configured."""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

from ..config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class Provider(Protocol):
    name: str
    model: str

    def complete(
        self, system: str, prompt: str, schema: type[T], attachment: tuple[bytes, str] | None = None,
        effort: str = "medium",
    ) -> T:
        """Return the model's answer parsed into `schema`. `attachment` = (bytes, media type) of a PDF or image."""
        ...


def get_provider(settings: Settings) -> Provider | None:
    if not settings.llm_enabled:
        return None
    if settings.llm_provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings.llm_model, settings.llm_api_key, settings.llm_base_url)
    if settings.llm_provider in ("openai", "openai-compatible", "ollama"):
        from .openai_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(settings.llm_model, settings.llm_api_key, settings.llm_base_url)
    raise LLMError(f"unknown LLM_PROVIDER '{settings.llm_provider}' (use 'anthropic' or 'openai')")
