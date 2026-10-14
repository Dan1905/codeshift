"""Concrete providers: an OpenAI-compatible HTTP adapter (Groq by default) and a factory."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from codeshift.config import LLMConfig
from codeshift.exceptions import LLMError, LLMResponseError, LLMUnavailableError
from codeshift.llm.base import LLMProvider, LLMReply
from codeshift.llm.prompts import build_messages
from codeshift.llm.schemas import MigrationRequest

log = logging.getLogger("codeshift.llm")

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 20.0


class OpenAICompatibleProvider(LLMProvider):
    """Chat-completions client for Groq or any OpenAI-compatible endpoint.

    The API key is read from the environment by `LLMConfig` and is only ever
    placed in the Authorization header; it is never logged or persisted.
    """

    external = True

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str,
        *,
        timeout_seconds: float = 60,
        max_retries: int = 2,
        max_output_tokens: int = 4000,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url:
            raise LLMUnavailableError(f"provider '{name}' has no base_url configured")
        if not model:
            raise LLMUnavailableError(f"provider '{name}' has no model configured")
        if not api_key:
            raise LLMUnavailableError(f"provider '{name}' has no API key in the environment")
        self.name = name
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._max_retries = max_retries
        self._max_output_tokens = max_output_tokens
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._sleep = sleep

    def generate_migration(
        self, request: MigrationRequest, feedback: str | None = None
    ) -> LLMReply:
        payload = {
            "model": self.model,
            "messages": build_messages(request, feedback),
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_error = "no attempt made"
        for attempt in range(self._max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._client.post(self._url, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = "request timed out"
            except httpx.TransportError as exc:
                last_error = f"transport error: {type(exc).__name__}"
            else:
                elapsed = round(time.perf_counter() - started, 3)
                log.info(
                    "llm request",
                    extra={
                        "provider": self.name,
                        "model": self.model,
                        "status": response.status_code,
                        "attempt": attempt + 1,
                        "seconds": elapsed,
                        "migration_id": request.migration_id,
                    },
                )
                if response.status_code == 200:
                    return self._parse(response)
                if response.status_code in (401, 403):
                    raise LLMUnavailableError(
                        f"{self.name} rejected the credentials (HTTP {response.status_code})"
                    )
                last_error = f"HTTP {response.status_code}"
                if response.status_code == 404:
                    raise LLMUnavailableError(
                        f"{self.name} has no model '{self.model}' available to this key "
                        "(HTTP 404). Set llm.model or CODESHIFT_LLM_MODEL to a current model."
                    )
                if response.status_code not in RETRYABLE_STATUS:
                    raise LLMError(f"{self.name} request failed: {last_error}")
                retry_after = response.headers.get("retry-after", "")
                if retry_after.replace(".", "", 1).isdigit():
                    self._sleep(min(float(retry_after), MAX_BACKOFF_SECONDS))
                    continue
            if attempt < self._max_retries:
                self._sleep(min(2.0**attempt, MAX_BACKOFF_SECONDS))
        raise LLMUnavailableError(
            f"{self.name} unavailable after {self._max_retries + 1} attempt(s): {last_error}"
        )

    def _parse(self, response: httpx.Response) -> LLMReply:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(f"{self.name} returned an unexpected response shape") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError(f"{self.name} returned an empty message")
        usage = body.get("usage") or {}
        return LLMReply(
            content=content,
            model=str(body.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


def create_provider(config: LLMConfig, client: httpx.Client | None = None) -> LLMProvider | None:
    """Build the configured provider, or None when LLM assistance is disabled.

    Raises `LLMUnavailableError` when a provider is requested but not usable,
    so a missing key is reported instead of silently skipping AI changes.
    """
    if not config.enabled:
        return None
    if config.provider == "sample":
        from codeshift.llm.sample_provider import SampleProvider

        return SampleProvider()
    api_key = config.resolved_api_key()
    if not api_key:
        raise LLMUnavailableError(
            f"LLM provider '{config.provider}' is enabled but no API key was found. "
            "Set GROQ_API_KEY (or CODESHIFT_LLM_API_KEY), or use the offline 'sample' provider."
        )
    return OpenAICompatibleProvider(
        name=config.provider,
        base_url=config.resolved_base_url(),
        model=config.resolved_model(),
        api_key=api_key,
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        max_output_tokens=config.max_output_tokens,
        client=client,
    )
