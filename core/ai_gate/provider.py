"""LLM transport: OpenAI-compatible ``/chat/completions`` with a hard deadline.

Secrets policy: the API key lives only in the ``Authorization`` header of the
outgoing request.  Every error message raised from here is passed through
:func:`redact` so neither the key nor the header can reach a log, the journal,
Telegram or the dashboard.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import requests

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    """Transport/HTTP failure; ``kind`` is ``timeout`` or ``http_error``."""

    def __init__(self, kind: str, message: str, http_status: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    http_status: int


class LLMProvider(Protocol):
    def complete(self, system: str, user: str, deadline: float) -> ProviderResponse:
        """Return the reply or raise :class:`ProviderError` before ``deadline`` (epoch s)."""


def redact(text: str, secret: str) -> str:
    out = str(text)
    if secret:
        out = out.replace(secret, "***")
    out = re.sub(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?bearer\s+)[^\s'\",}]+", r"\1***", out)
    out = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer ***", out)
    return out[:500]


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_output_tokens: int = 600,
        response_format_json: bool = True,
        max_retries: int = 2,
        session: Optional[requests.Session] = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self.model = model
        self._temperature = float(temperature)
        self._max_tokens = int(max_output_tokens)
        self._json = bool(response_format_json)
        self._retries = int(max_retries)
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep

    def complete(self, system: str, user: str, deadline: float) -> ProviderResponse:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if self._json:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

        attempt = 0
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0.2:
                raise ProviderError("timeout", "deadline reached before request")
            started = self._clock()
            try:
                resp = self._session.post(self._url, json=body, headers=headers,
                                          timeout=(min(5.0, remaining), remaining))
            except requests.Timeout:
                raise ProviderError("timeout", "request timed out") from None
            except requests.RequestException as exc:
                error = ProviderError("http_error", redact(f"network error: {exc}", self._key))
                if not self._backoff(attempt, deadline, None):
                    raise error from None
                attempt += 1
                continue
            latency_ms = int((self._clock() - started) * 1000)

            if resp.status_code == 200:
                return self._parse(resp, latency_ms)

            error = ProviderError(
                "http_error",
                redact(f"HTTP {resp.status_code}: {resp.text[:200]}", self._key),
                resp.status_code,
            )
            if resp.status_code not in _RETRY_STATUS:
                raise error
            if not self._backoff(attempt, deadline, resp.headers.get("Retry-After")):
                raise error
            attempt += 1

    # ---------------------------------------------------------------- helpers
    def _backoff(self, attempt: int, deadline: float, retry_after: Optional[str]) -> bool:
        """Sleep before the next attempt; False when no retry is possible."""
        if attempt >= self._retries:
            return False
        wait = min(8.0, 0.5 * (2 ** attempt)) * (0.8 + 0.4 * random.random())
        if retry_after:
            try:
                wait = max(wait, float(retry_after))
            except ValueError:
                pass
        if self._clock() + wait >= deadline - 0.5:
            return False
        self._sleep(wait)
        return True

    def _parse(self, resp: requests.Response, latency_ms: int) -> ProviderResponse:
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("http_error", "malformed provider response", resp.status_code) from None
        usage = data.get("usage") or {}
        return ProviderResponse(
            text=str(text or ""),
            model=str(data.get("model") or self.model),
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
            http_status=resp.status_code,
        )


class FakeProvider:
    """Deterministic provider for tests (no network)."""

    def __init__(self, text: str = "", error: Optional[ProviderError] = None,
                 delay: float = 0.0, tokens=(1000, 100), model: str = "fake-model") -> None:
        self.text, self.error, self.delay, self.tokens, self.model = text, error, delay, tokens, model
        self.calls = []

    def complete(self, system: str, user: str, deadline: float) -> ProviderResponse:
        self.calls.append((system, user))
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return ProviderResponse(self.text, self.model, self.tokens[0], self.tokens[1],
                                int(self.delay * 1000), 200)
