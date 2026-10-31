"""Cerebras adapter for the OpenAI-compatible ``/chat/completions`` endpoint.

Responsibilities kept at this boundary:

* HTTP transport, timeout and retry (429 / 5xx / timeouts / transport errors)
  with exponential backoff, jitter and ``Retry-After`` support;
* a **shared cooldown**: one provider instance is used by every generation
  thread, so a rate-limit response in one thread must also pause the others.
  After a 429/5xx the cooldown is armed until ``now + delay`` and every thread
  waits for it before its next request, and only one request is in flight at a
  time (otherwise a burst of threads would immediately re-trigger the limit);
* optional JSON mode (``response_format={"type": "json_object"}``) when the
  request asks for ``response_format == "json"``;
* per-request parameters (model, temperature, top_p, max_tokens, seed);
* usage, finish reason, latency and the raw response body;
* response scrubbing: the API key never leaves this module's HTTP headers, and
  every error message, ``details`` payload and stored ``raw`` body is scrubbed
  by :mod:`contractforge.sanitize` so a provider that echoes credentials back
  cannot get them into a log, an exception envelope or a rejected-contract file.

Lifecycle: the adapter owns an ``httpx.Client``. Call :meth:`close` (or use it
as a context manager) when the provider is no longer needed — ``pipeline``
closes it when a dataset run finishes.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..errors import ConfigError, ProviderError
from ..models import LLMRequest, LLMResponse, Usage
from ..sanitize import scrub_text, scrub_value

RETRY_AFTER_CAP_S = 300.0
_JITTER = 0.1  # Retry-After is scaled by uniform(1 - _JITTER, 1 + _JITTER)
_ERROR_SNIPPET_CHARS = 500


class _Cooldown:
    """Thread-safe rate-limit gate shared by every thread using one provider.

    ``arm(delay)`` records that no thread may send a request until ``delay``
    seconds from now; ``wait()`` blocks the calling thread until that has
    passed. ``monotonic()`` measures the sleep so a wall-clock change cannot
    shorten a server-imposed pause.
    """

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._deadline = 0.0

    def arm(self, delay_s: float) -> float:
        """Block requests for at least ``delay_s`` more seconds; return the wait.

        Returns the part of the cooldown that has *not* already been slept by an
        earlier ``wait()``/``arm()`` in any thread, so the caller that triggered
        the retry can sleep it once instead of twice.
        """

        with self._lock:
            now = self._clock()
            self._deadline = max(self._deadline, now + max(delay_s, 0.0))
            return max(0.0, self._deadline - now)

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._deadline - self._clock())

    def wait(self) -> None:
        """Block until the cooldown armed by *any* thread has elapsed."""

        while (remaining := self.remaining()) > 0:
            self._sleep(remaining)


class CerebrasProvider:
    """Synchronous Cerebras client implementing :class:`Provider`."""

    name = "cerebras"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float,
        max_retries: int,
        backoff_s: float,
        temperature: float,
        top_p: float | None,
        max_tokens: int,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not api_key:
            raise ConfigError(
                "CEREBRAS_API_KEY is required for the cerebras provider; export the key "
                "or select the explicit offline provider (provider='mock')"
            )
        if not base_url:
            raise ConfigError("a Cerebras base URL is required")
        if not model:
            raise ConfigError("a Cerebras model id is required")
        if timeout_s <= 0:
            raise ConfigError("timeout_s must be greater than 0")
        if max_retries < 0:
            raise ConfigError("max_retries must be 0 or greater")
        if backoff_s < 0:
            raise ConfigError("backoff_s must be 0 or greater")

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self._sleep = sleep or time.sleep
        self._cooldown = _Cooldown(sleep=self._sleep, clock=clock or time.monotonic)
        self._request_lock = threading.Lock()
        self._closed = False
        self._client = httpx.Client(
            base_url=f"{self.base_url}/",
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def __repr__(self) -> str:  # never expose the API key in logs
        return f"CerebrasProvider(model={self.model!r}, base_url={self.base_url!r})"

    def __enter__(self) -> CerebrasProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        self._client.close()

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._closed:
            raise ProviderError(
                "Cerebras provider is closed; it cannot be used after close()",
                details={"provider": self.name},
            )
        payload = self._payload(request)
        started = time.perf_counter()
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            # One request in flight per provider: threads sharing a provider must
            # not burst into the rate limit. The cooldown is re-checked while the
            # lock is held, because a cooldown armed by the request that just
            # finished must be waited out by the next one.
            with self._request_lock:
                self._cooldown.wait()
                try:
                    response = self._client.post("chat/completions", json=payload)
                except httpx.TimeoutException as exc:
                    if attempt < attempts:
                        self._wait_before_retry(attempt, retry_response=None)
                        continue
                    raise ProviderError(
                        f"Cerebras request timed out after {attempt} attempt(s) "
                        f"(timeout {self.timeout_s:g}s)",
                        details={"attempts": attempt, "timeout_s": self.timeout_s},
                        attempts=attempt,
                    ) from exc
                except httpx.TransportError as exc:
                    if attempt < attempts:
                        self._wait_before_retry(attempt, retry_response=None)
                        continue
                    raise ProviderError(
                        f"Cerebras transport error after {attempt} attempt(s): "
                        f"{type(exc).__name__}: {exc}",
                        details={"attempts": attempt},
                        attempts=attempt,
                    ) from exc

            if response.status_code == 429 or response.status_code >= 500:
                delay = self._retry_delay(response, attempt)
                if attempt < attempts:
                    self._wait_before_retry(attempt, retry_response=response)
                    continue
                raise ProviderError(
                    f"Cerebras returned HTTP {response.status_code} after {attempt} "
                    f"attempt(s): {_snippet(response.text)}",
                    details={
                        "status_code": response.status_code,
                        "attempts": attempt,
                        "retryable": True,
                        "retry_after_s": round(delay, 3),
                        "error_excerpt": _snippet(response.text),
                    },
                    attempts=attempt,
                )
            if response.status_code >= 400:
                raise ProviderError(
                    f"Cerebras returned HTTP {response.status_code}: {_snippet(response.text)}",
                    details={
                        "status_code": response.status_code,
                        "attempts": attempt,
                        "retryable": False,
                        # The provider's own error body: scrubbed before it can
                        # reach a log line, a rejected file or a CLI envelope.
                        "error_excerpt": _snippet(response.text),
                    },
                    attempts=attempt,
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise ProviderError(
                    "Cerebras response body was not valid JSON",
                    details={"status_code": response.status_code, "attempts": attempt},
                    attempts=attempt,
                ) from exc
            if not isinstance(body, dict):
                raise ProviderError(
                    "Cerebras response body was not a JSON object",
                    details={"status_code": response.status_code, "attempts": attempt},
                    attempts=attempt,
                )
            latency_ms = (time.perf_counter() - started) * 1000.0
            return self._to_response(request, body, latency_ms=latency_ms)

        # The loop either returns or raises; this keeps type checkers happy.
        raise ProviderError("Cerebras request failed without a response")  # pragma: no cover

    # -- request assembly -------------------------------------------------

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        model = request.model or self.model
        temperature = self.temperature if request.temperature is None else request.temperature
        top_p = self.top_p if request.top_p is None else request.top_p
        max_tokens = self.max_tokens if request.max_tokens is None else request.max_tokens

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        return payload

    # -- response parsing -------------------------------------------------

    def _to_response(
        self, request: LLMRequest, body: dict[str, Any], *, latency_ms: float
    ) -> LLMResponse:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError(
                "Cerebras response contained no choices",
                details={"finish_reason": body.get("finish_reason")},
            )
        choice = choices[0]
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ProviderError("Cerebras response contained no message content")

        usage = _parse_usage(body.get("usage"))
        body_model = body.get("model")
        model = (
            body_model
            if isinstance(body_model, str) and body_model
            else (request.model or self.model)
        )
        finish_reason = choice.get("finish_reason")
        return LLMResponse(
            text=content,
            provider=self.name,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            # A raw body is stored on disk and in call records: it must never
            # contain a credential the provider echoed back.
            raw=scrub_value(body),
        )

    # -- retry helpers ----------------------------------------------------

    def _wait_before_retry(self, attempt: int, *, retry_response: httpx.Response | None) -> None:
        """Wait once before the next attempt and share that wait with all threads.

        The cooldown is armed with the total delay; the retrying thread then
        sleeps the part of it the other threads have not already used up.
        """

        delay = (
            self._backoff_delay(attempt)
            if retry_response is None
            else self._retry_delay(retry_response, attempt)
        )
        remaining = self._cooldown.arm(delay)
        self._sleep(remaining)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff for the attempt that just failed (1-based)."""

        delay = self.backoff_s * float(2 ** (attempt - 1))
        return delay + random.uniform(0.0, delay * 0.25)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        """``Retry-After`` (jittered, capped) or exponential backoff without it."""

        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        if retry_after is None:
            return self._backoff_delay(attempt)
        jittered = retry_after * random.uniform(1.0 - _JITTER, 1.0 + _JITTER)
        return min(max(jittered, 0.0), RETRY_AFTER_CAP_S)


def _parse_retry_after(header: str | None) -> float | None:
    """Parse ``Retry-After`` as delta-seconds or an HTTP date."""

    if not header:
        return None
    header = header.strip()
    try:
        return float(header)
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - datetime.now(timezone.utc)).total_seconds()


def _parse_usage(payload: Any) -> Usage:
    if not isinstance(payload, dict):
        return Usage()
    input_tokens = _token_value(payload, "prompt_tokens", "input_tokens")
    output_tokens = _token_value(payload, "completion_tokens", "output_tokens")
    total_tokens = _token_value(payload, "total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _token_value(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _snippet(text: str) -> str:
    """Collapsed, length-capped HTTP body excerpt with credentials redacted."""

    collapsed = " ".join(text.split())
    return scrub_text(collapsed[:_ERROR_SNIPPET_CHARS]) or "(empty body)"
