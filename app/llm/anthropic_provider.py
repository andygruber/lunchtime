"""Claude via the Anthropic SDK: the PDF is attached as a document block, output via structured outputs."""

from __future__ import annotations

import base64

import anthropic

from .base import LLMError, T

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# models that accept server-side refusal fallbacks ("default" routing)
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None, base_url: str | None = None):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, max_retries=3, timeout=300.0)

    def complete(self, system: str, prompt: str, schema: type[T], attachment: tuple[bytes, str] | None = None,
                 effort: str = "medium") -> T:
        content: list[dict] = []
        if attachment is not None:
            data, media = attachment
            encoded = base64.standard_b64encode(data).decode()
            if media == "application/pdf":
                content.append({"type": "document",
                                "source": {"type": "base64", "media_type": media, "data": encoded}})
            elif media in IMAGE_TYPES:
                content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": encoded}})
        content.append({"type": "text", "text": prompt})
        extra = {}
        if self.model in _FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=32000,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": effort},
                output_format=schema,
                **extra,
            )
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e
        if response.stop_reason == "refusal":
            raise LLMError("model refused the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("response truncated (max_tokens)")
        if response.parsed_output is None:
            raise LLMError("no structured output returned")
        return response.parsed_output
