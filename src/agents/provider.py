"""The LLM abstraction (section 10.2). The only module with provider-specific code.

An agent holds a Provider and calls complete(prompt). Given a JSON schema, the
reply is held to it by the provider's own structured output mode, so it is
always one JSON object of that shape. Which provider it is,
and how it is reached, is decided here from agents.yaml. The API key comes from
the provider's environment variable (main loads .env into the environment). It
is never logged or put in an error message.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Protocol

from common.paths import ConfigError
from agents.schema import ProviderConfig

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_API_VERSION = "2023-06-01"
CLAUDE_MAX_TOKENS = 16000   # the reply's ceiling; the Messages API requires one

# Worth another try: rate limited, or the service briefly failing (529: Anthropic overloaded).
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class Provider(Protocol):
    def complete(self, prompt: str, temperature: float = 0.0, schema: dict | None = None) -> str: ...


class ProviderError(RuntimeError):
    """The provider could not be reached, refused the request, or returned no text."""


class GeminiProvider:
    """Google Gemini over its REST API, with the standard library only."""

    def __init__(self, config: ProviderConfig, api_key: str):
        self.config = config
        self.api_key = api_key
        self.model = f"gemini/{config.model}"   # the envelope's "provider/model-name"

    def complete(self, prompt: str, temperature: float = 0.0, schema: dict | None = None) -> str:
        """The model's text reply, JSON of schema's shape if given. Retries timeouts, 429 and 5xx; raises ProviderError."""
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        if schema:
            body["generationConfig"] |= {"responseMimeType": "application/json", "responseJsonSchema": schema}
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        data = post("gemini", GEMINI_URL.format(model=self.config.model), body, headers, self.config)
        return gemini_text(data)


class ClaudeProvider:
    """Anthropic Claude over the Messages API, with the standard library only."""

    def __init__(self, config: ProviderConfig, api_key: str):
        self.config = config
        self.api_key = api_key
        self.model = f"claude/{config.model}"   # the envelope's "provider/model-name"

    def complete(self, prompt: str, temperature: float = 0.0, schema: dict | None = None) -> str:
        """The model's text reply, JSON of schema's shape if given. Retries timeouts, 429, 529 and 5xx; raises ProviderError."""
        body = {
            "model": self.config.model,
            "max_tokens": CLAUDE_MAX_TOKENS,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if schema:
            body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": CLAUDE_API_VERSION,
        }
        return claude_text(post("claude", CLAUDE_URL, body, headers, self.config))


def post(name: str, url: str, body: dict, headers: dict, config: ProviderConfig) -> dict:
    """POST body as JSON and return the JSON reply. Retries timeouts and RETRY_STATUSES."""
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")

    attempts = config.max_retries + 1
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in RETRY_STATUSES:
                raise ProviderError(f"{name} returned HTTP {error.code}: {error_message(error)}") from error
            last = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError) as error:
            last = str(getattr(error, "reason", error))
        except json.JSONDecodeError as error:
            raise ProviderError(f"{name} returned a reply that is not JSON") from error
        if attempt < attempts:
            time.sleep(config.retry_backoff_seconds * attempt)

    raise ProviderError(f"{name} unreachable after {attempts} attempts: {last}")


def gemini_text(data: dict) -> str:
    """The text of Gemini's first candidate. A blocked or cut-off reply is an error."""
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise ProviderError(f"gemini blocked the prompt: {feedback['blockReason']}")
    candidates = data.get("candidates") or []
    if not candidates:
        raise ProviderError("gemini returned no candidates")
    candidate = candidates[0]
    if candidate.get("finishReason") not in (None, "STOP"):
        raise ProviderError(f"gemini stopped early: {candidate['finishReason']}")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    if not text.strip():
        raise ProviderError("gemini returned an empty reply")
    return text


def claude_text(data: dict) -> str:
    """The text blocks of Claude's reply. A refused or cut-off reply is an error."""
    if data.get("stop_reason") != "end_turn":
        raise ProviderError(f"claude stopped early: {data.get('stop_reason')}")
    blocks = data.get("content") or []
    text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    if not text.strip():
        raise ProviderError("claude returned an empty reply")
    return text


def error_message(error: urllib.error.HTTPError) -> str:
    """The provider's own error message from the body, e.g. "API key not valid"."""
    try:
        return json.loads(error.read())["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return error.reason or "no message"


# agents.yaml provider.name -> the class, and the environment variable its key falls back to.
PROVIDERS = {
    "gemini": (GeminiProvider, "GEMINI_API_KEY"),
    "claude": (ClaudeProvider, "ANTHROPIC_API_KEY"),
}


def make_provider(config: ProviderConfig) -> Provider:
    """The provider agents.yaml names, with its key from the environment."""
    if config.name not in PROVIDERS:
        raise ConfigError(f"provider '{config.name}' is not supported; use one of: {', '.join(PROVIDERS)}")
    provider_class, variable = PROVIDERS[config.name]
    api_key = os.environ.get(variable, "").strip()
    if not api_key:
        raise ConfigError(f"no API key: set {variable} in .env or the environment")
    return provider_class(config, api_key)
