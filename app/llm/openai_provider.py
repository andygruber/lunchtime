"""Any OpenAI-compatible chat endpoint (OpenAI, Ollama, OpenRouter, LM Studio, ...).

PDF upload is not portable across these servers, so the positioned text of the document is sent instead
(it is already part of the prompt); images are attached for vision-capable models.
"""

from __future__ import annotations

import base64
import json

import openai
from pydantic import ValidationError

from .base import LLMError, T


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(self, model: str, api_key: str | None, base_url: str | None = None):
        self.model = model
        self.client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url, max_retries=3, timeout=300.0)

    def complete(self, system: str, prompt: str, schema: type[T], attachment: tuple[bytes, str] | None = None,
                 effort: str = "medium") -> T:
        user: str | list[dict] = prompt
        if attachment is not None and attachment[1].startswith("image/"):
            # images go along for vision models; PDFs are represented by their positioned text in the prompt
            data_uri = f"data:{attachment[1]};base64,{base64.b64encode(attachment[0]).decode()}"
            user = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_uri}}]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        formats = [
            {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}},
            {"type": "json_object"},  # servers without json_schema support
        ]
        last: Exception | None = None
        for fmt in formats:
            if fmt["type"] == "json_object":
                messages[0]["content"] = (
                    system + "\n\nAnswer with a single JSON object matching this JSON schema:\n"
                    + json.dumps(schema.model_json_schema())
                )
            try:
                r = self.client.chat.completions.create(model=self.model, messages=messages, response_format=fmt)
            except openai.BadRequestError as e:
                last = e
                continue
            except openai.APIError as e:
                raise LLMError(f"API error: {e}") from e
            text = r.choices[0].message.content or ""
            try:
                return schema.model_validate_json(text)
            except ValidationError as e:
                last = e
        raise LLMError(f"no valid structured output: {last}")
