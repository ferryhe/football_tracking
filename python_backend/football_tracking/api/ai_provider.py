from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class OpenAIProviderSettings:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    chat_model: str = "gpt-4o-mini"
    improvement_model: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())


def load_provider_settings(repo_root: Path) -> OpenAIProviderSettings:
    env_values = _read_dotenv(repo_root / ".env")
    api_key = os.environ.get("PROVIDER_OPENAI_API_KEY", env_values.get("PROVIDER_OPENAI_API_KEY", "")).strip()
    base_url = os.environ.get(
        "PROVIDER_OPENAI_BASE_URL",
        env_values.get("PROVIDER_OPENAI_BASE_URL", "https://api.openai.com/v1"),
    ).strip()
    chat_model = os.environ.get(
        "PROVIDER_OPENAI_CHAT_MODEL",
        env_values.get("PROVIDER_OPENAI_CHAT_MODEL", "gpt-4o-mini"),
    ).strip()
    improvement_model = os.environ.get(
        "PROVIDER_OPENAI_IMPROVEMENT_MODEL",
        env_values.get("PROVIDER_OPENAI_IMPROVEMENT_MODEL", ""),
    ).strip()
    return OpenAIProviderSettings(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        chat_model=chat_model or "gpt-4o-mini",
        improvement_model=improvement_model or None,
    )


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class OpenAIResponsesClient:
    def __init__(self, settings: OpenAIProviderSettings) -> None:
        self.settings = settings

    def is_enabled(self) -> bool:
        return self.settings.enabled

    def create_json_response(
        self,
        *,
        instructions: str,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            raise RuntimeError("OpenAI provider is not configured.")

        payload = {
            "model": model or self.settings.chat_model,
            "instructions": instructions,
            "input": prompt,
            "temperature": temperature,
            "text": {
                "format": {
                    "type": "json_object",
                }
            },
        }
        return self._post_json_payload(payload)

    def create_json_vision_response(
        self,
        *,
        instructions: str,
        prompt: str,
        images: list[dict[str, str]],
        model: str | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            raise RuntimeError("OpenAI provider is not configured.")

        content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
        for image in images:
            label = image.get("label")
            data_url = image.get("data_url")
            if not data_url:
                continue
            if label:
                content.append({"type": "input_text", "text": f"Image: {label}"})
            content.append({"type": "input_image", "image_url": data_url})

        text_format: dict[str, Any]
        if json_schema is not None:
            text_format = {
                "type": "json_schema",
                "name": "ai_visual_review",
                "schema": json_schema,
                "strict": True,
            }
        else:
            text_format = {"type": "json_object"}

        payload = {
            "model": model or self.settings.chat_model,
            "instructions": instructions,
            "input": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "temperature": temperature,
            "text": {
                "format": text_format,
            },
        }
        return self._post_json_payload(payload)

    def _post_json_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url=f"{self.settings.base_url}/responses",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.api_key}",
            },
            data=json.dumps(payload).encode("utf-8"),
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"OpenAI API HTTP {exc.code}: {_redact_provider_error(detail, self.settings.api_key)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"OpenAI API request failed: {_redact_provider_error(str(exc), self.settings.api_key)}"
            ) from exc

        parsed = json.loads(raw)
        output_text = parsed.get("output_text") or self._extract_output_text(parsed)
        if not output_text:
            raise RuntimeError("OpenAI API returned no output_text.")
        try:
            return json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenAI API did not return valid JSON: {output_text[:400]}") from exc

    def _extract_output_text(self, response: dict[str, Any]) -> str:
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return str(content.get("text", ""))
        return ""


def _redact_provider_error(message: str, api_key: str = "") -> str:
    redacted = re.sub(r"data:[^\s\"']*;base64,[^\s\"']+", "data:<redacted-base64>", message)
    redacted = re.sub(r"Bearer\s+[^\s\"']+", "Bearer <redacted>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-<redacted>", redacted)
    if api_key:
        redacted = redacted.replace(api_key, "<redacted-api-key>")
    return redacted
