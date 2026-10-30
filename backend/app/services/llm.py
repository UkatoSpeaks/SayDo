"""LLM providers that return Pydantic-validated JSON.

``LLMProvider.complete_json`` sends a system + user prompt, validates the reply
against a Pydantic model and, if the reply is invalid, makes one repair attempt
that shows the model its validation error. A second failure raises
``LLMOutputError``.

``GroqProvider`` uses the official groq SDK:
- strict JSON-schema structured outputs on models that support them (gpt-oss),
  JSON mode plus Pydantic validation otherwise;
- a client-side limiter on tokens and requests per rolling 60 s window (Groq's
  free-tier limits are per organization, so share one provider per process);
- on 429: honour ``retry-after``, exponential backoff with jitter, at most
  ``max_attempts`` attempts; a *daily* quota error raises ``LLMQuotaExhausted``
  straight away so the caller can pause.

``FakeLLMProvider`` replays scripted responses for tests.
"""

import abc
import asyncio
import json
import logging
import random
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import groq
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.services.claim_schema import llm_json_schema
from app.services.tokens import count_tokens

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

WINDOW_S = 60.0
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 60.0
# Tokens added per chat message by the API's own formatting (approximate).
MESSAGE_OVERHEAD_TOKENS = 4
# Longest a single 429 retry-after is honoured for before treating it as a quota stop.
MAX_RETRY_AFTER_S = 120.0
MAX_ERROR_CHARS = 500

Message = dict[str, str]


# ----------------------------------------------------------------- errors


class LLMError(Exception):
    """An LLM call failed; the message never contains credentials."""

    def __init__(self, message: str, *, usage: "LLMUsage | None" = None) -> None:
        super().__init__(message)
        self.usage = usage or LLMUsage()


class LLMConfigError(LLMError):
    pass


class LLMOutputError(LLMError):
    """The model's reply was not valid JSON for the schema, even after the repair retry."""


class LLMRateLimitError(LLMError):
    """Still rate limited (or the API kept failing) after every attempt."""


class LLMQuotaExhausted(LLMError):
    """The daily quota is used up; retrying today is pointless."""


# ------------------------------------------------------------------ usage


@dataclass
class LLMUsage:
    tokens_in: int = 0
    tokens_out: int = 0

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(self.tokens_in + other.tokens_in, self.tokens_out + other.tokens_out)

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out


# ------------------------------------------------------------------ clock


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Time only moves when someone sleeps. ``sleeps`` records every wait."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)


# ---------------------------------------------------------- rate limiting


@dataclass
class _Entry:
    at: float
    tokens: int


class RateLimiter:
    """Tokens-per-minute and requests-per-minute budget over a rolling window.

    ``acquire`` reserves an estimate and waits until it fits; ``settle`` replaces
    the estimate with the real usage once the response is in. A single request
    larger than the whole TPM budget is let through once the window is empty.
    """

    def __init__(
        self, tpm_limit: int, rpm_limit: int, clock: Clock, *, window_s: float = WINDOW_S
    ) -> None:
        self.tpm_limit = tpm_limit
        self.rpm_limit = rpm_limit
        self.window_s = window_s
        self._clock = clock
        self._entries: deque[_Entry] = deque()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    def _prune(self, now: float) -> None:
        while self._entries and self._entries[0].at <= now - self.window_s:
            self._entries.popleft()

    def tokens_in_window(self) -> int:
        self._prune(self._clock.monotonic())
        return sum(entry.tokens for entry in self._entries)

    def _wait_needed(self, now: float, tokens: int) -> float:
        """Seconds until a request of ``tokens`` fits (0 when it fits now)."""
        if now < self._blocked_until:
            return self._blocked_until - now
        used = sum(entry.tokens for entry in self._entries)
        fits_tokens = used + tokens <= self.tpm_limit or not self._entries
        fits_requests = len(self._entries) < self.rpm_limit
        if fits_tokens and fits_requests:
            return 0.0
        # Walk the window oldest-first until enough has expired.
        freed_tokens = 0
        for freed_requests, entry in enumerate(self._entries, start=1):
            freed_tokens += entry.tokens
            remaining = len(self._entries) - freed_requests
            tokens_ok = used - freed_tokens + tokens <= self.tpm_limit or remaining == 0
            if tokens_ok and remaining < self.rpm_limit:
                return max(0.0, entry.at + self.window_s - now)
        raise AssertionError("unreachable")  # pragma: no cover

    async def acquire(self, tokens: int) -> _Entry:
        async with self._lock:
            while True:
                now = self._clock.monotonic()
                self._prune(now)
                wait = self._wait_needed(now, tokens)
                if wait <= 0:
                    entry = _Entry(now, tokens)
                    self._entries.append(entry)
                    return entry
                logger.info(
                    "LLM rate limiter: waiting %.1fs (%d tokens / %d requests in window)",
                    wait,
                    sum(e.tokens for e in self._entries),
                    len(self._entries),
                )
                await self._clock.sleep(wait)

    def settle(self, entry: _Entry, actual_tokens: int) -> None:
        entry.tokens = max(0, actual_tokens)

    def block_for(self, seconds: float) -> None:
        """Hold every request back for ``seconds`` (after a 429 with retry-after)."""
        self._blocked_until = max(self._blocked_until, self._clock.monotonic() + seconds)


# --------------------------------------------------------------- providers


def _format_validation_error(exc: ValidationError | ValueError) -> str:
    if isinstance(exc, ValidationError):
        lines = [
            f"- {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()[:20]
        ]
        return "\n".join(lines)
    return str(exc)


def _repair_message(error: str) -> Message:
    return {
        "role": "user",
        "content": (
            "Your previous reply was not valid for the required JSON schema:\n"
            f"{error}\n\n"
            "Reply again with only the corrected JSON object. Keep the same claims; do not "
            "add new ones or invent values. Use null where a value is unknown."
        ),
    }


class LLMProvider(abc.ABC):
    default_model: str

    async def complete_json(
        self, system: str, user: str, schema: type[T], model: str | None = None
    ) -> tuple[T, LLMUsage]:
        """Return the reply parsed as ``schema`` and the tokens used (all attempts)."""
        model = model or self.default_model
        messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        usage = LLMUsage()
        for attempt in (1, 2):
            try:
                content, call_usage = await self._complete(messages, schema, model)
            except LLMError as exc:
                exc.usage = usage + exc.usage
                raise
            usage += call_usage
            try:
                return schema.model_validate_json(content), usage
            except ValidationError as exc:
                error = _format_validation_error(exc)
            if attempt == 2:
                raise LLMOutputError(
                    f"Model output failed validation after a repair retry: {error}"[
                        :MAX_ERROR_CHARS
                    ],
                    usage=usage,
                )
            logger.warning("LLM output failed validation; sending one repair request")
            messages = [
                *messages,
                {"role": "assistant", "content": content or "(empty reply)"},
                _repair_message(error),
            ]
        raise AssertionError("unreachable")  # pragma: no cover

    @abc.abstractmethod
    async def _complete(
        self, messages: list[Message], schema: type[BaseModel], model: str
    ) -> tuple[str, LLMUsage]:
        """Send ``messages`` and return the raw reply text and its usage."""


# ------------------------------------------------------------------ fake

Scripted = str | dict[str, Any] | BaseModel | Exception
Responder = Callable[[list[Message]], Scripted]


@dataclass
class FakeCall:
    model: str
    messages: list[Message]

    @property
    def system(self) -> str:
        return self.messages[0]["content"]

    @property
    def user(self) -> str:
        return self.messages[1]["content"]


class FakeLLMProvider(LLMProvider):
    """Returns scripted replies in order; when the script runs out, ``responder``
    (if given) answers, else ``{"claims": []}``. Exceptions in the script are raised.
    Usage is the tiktoken count of the messages and the reply.
    """

    def __init__(
        self,
        script: Sequence[Scripted] = (),
        *,
        responder: Responder | None = None,
        default_model: str = "fake-llm",
    ) -> None:
        self.script: deque[Scripted] = deque(script)
        self.responder = responder
        self.default_model = default_model
        self.calls: list[FakeCall] = []

    async def _complete(
        self, messages: list[Message], schema: type[BaseModel], model: str
    ) -> tuple[str, LLMUsage]:
        self.calls.append(FakeCall(model, list(messages)))
        if self.script:
            item = self.script.popleft()
        elif self.responder is not None:
            item = self.responder(messages)
        else:
            item = {"claims": []}
        if isinstance(item, Exception):
            raise item
        if isinstance(item, BaseModel):
            content = item.model_dump_json()
        elif isinstance(item, dict):
            content = json.dumps(item)
        else:
            content = item
        tokens_in = sum(count_tokens(m["content"]) for m in messages)
        return content, LLMUsage(tokens_in, count_tokens(content))


# ------------------------------------------------------------------ groq

# Models that accept response_format={"type": "json_schema", "strict": true} on Groq.
_JSON_SCHEMA_MODEL_PREFIXES = ("openai/gpt-oss-",)
_DAILY_QUOTA_RE = re.compile(
    r"per day|\(TPD\)|\(RPD\)|\(ASD\)|\(ASH\)|daily (?:token |request )?(?:limit|quota)",
    re.IGNORECASE,
)
_TRY_AGAIN_RE = re.compile(
    r"try again in\s+(?:(?P<h>\d+)h)?\s*(?:(?P<m>\d+)m(?!s))?\s*(?:(?P<s>[\d.]+)s)?"
    r"\s*(?:(?P<ms>[\d.]+)ms)?",
    re.IGNORECASE,
)


def supports_json_schema(model: str) -> bool:
    return model.startswith(_JSON_SCHEMA_MODEL_PREFIXES)


def _error_text(exc: groq.APIStatusError) -> str:
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return str(exc.message or exc)


def _error_code(exc: groq.APIStatusError) -> str | None:
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
    return None


def is_daily_quota_error(exc: groq.APIStatusError) -> bool:
    """Groq's 429 text names the limit, e.g. "... on tokens per day (TPD): Limit 200000"."""
    return bool(_DAILY_QUOTA_RE.search(_error_text(exc)))


def retry_after_seconds(exc: groq.APIStatusError) -> float | None:
    """``retry-after`` header (seconds), else the "try again in 1m2.5s" hint in the message."""
    header = exc.response.headers.get("retry-after") if exc.response is not None else None
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    match = _TRY_AGAIN_RE.search(_error_text(exc))
    if match and any(match.groupdict().values()):
        return (
            float(match["h"] or 0) * 3600
            + float(match["m"] or 0) * 60
            + float(match["s"] or 0)
            + float(match["ms"] or 0) / 1000
        )
    return None


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, groq.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, groq.APIConnectionError | groq.APITimeoutError)


@dataclass
class GroqProvider(LLMProvider):
    api_key: str | None
    default_model: str
    limiter: RateLimiter
    clock: Clock = field(default_factory=SystemClock)
    max_attempts: int = 5
    max_output_tokens: int = 4096
    output_tokens_estimate: int = 1000
    reasoning_effort: str | None = "low"
    timeout_s: float = 90.0
    client: Any = None
    jitter: Callable[[float, float], float] = random.uniform
    _json_mode_models: set[str] = field(default_factory=set)

    def _get_client(self) -> Any:
        if self.client is None:
            if not self.api_key:
                raise LLMConfigError(
                    "GROQ_API_KEY is not set; add it to backend/.env or set LLM_PROVIDER=fake."
                )
            # Retries are handled here so they respect the shared rate limiter.
            self.client = groq.AsyncGroq(
                api_key=self.api_key, max_retries=0, timeout=self.timeout_s
            )
        return self.client

    async def list_models(self) -> list[str]:
        response = await self._get_client().models.list()
        return sorted(model.id for model in response.data)

    def _request_kwargs(
        self, messages: list[Message], schema: type[BaseModel], model: str
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": self.max_output_tokens,
        }
        if model.startswith("openai/gpt-oss-") and self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if supports_json_schema(model) and model not in self._json_mode_models:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": llm_json_schema(schema),
                    "strict": True,
                },
            }
        else:
            # JSON mode: the schema has to be in the prompt, and the word "JSON" must appear.
            schema_text = json.dumps(llm_json_schema(schema), separators=(",", ":"))
            first, *rest = messages
            kwargs["messages"] = [
                {
                    "role": "system",
                    "content": f"{first['content']}\n\nRespond with a single JSON object that "
                    f"matches this JSON schema:\n{schema_text}",
                },
                *rest,
            ]
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def _estimate_tokens(self, kwargs: dict[str, Any]) -> int:
        prompt = sum(
            count_tokens(m["content"]) + MESSAGE_OVERHEAD_TOKENS for m in kwargs["messages"]
        )
        response_format = kwargs.get("response_format", {})
        if response_format.get("type") == "json_schema":
            prompt += count_tokens(json.dumps(response_format["json_schema"]["schema"]))
        return prompt + min(self.output_tokens_estimate, self.max_output_tokens)

    async def _complete(
        self, messages: list[Message], schema: type[BaseModel], model: str
    ) -> tuple[str, LLMUsage]:
        client = self._get_client()
        for attempt in range(1, self.max_attempts + 1):
            kwargs = self._request_kwargs(messages, schema, model)
            estimate = self._estimate_tokens(kwargs)
            entry = await self.limiter.acquire(estimate)
            try:
                response = await client.chat.completions.create(**kwargs)
            except groq.BadRequestError as exc:
                if _error_code(exc) == "json_validate_failed":
                    # Strict mode rejected the model's output: hand it to the repair step.
                    self.limiter.settle(entry, estimate)
                    failed = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
                    content = str(failed.get("failed_generation") or "")
                    return content, LLMUsage(estimate - self.output_tokens_estimate, 0)
                if (
                    "response_format" in kwargs
                    and kwargs["response_format"]["type"] == "json_schema"
                    and re.search(r"response_format|json_schema", _error_text(exc), re.I)
                ):
                    logger.warning(
                        "%s rejected json_schema output; falling back to JSON mode", model
                    )
                    self.limiter.settle(entry, 0)
                    self._json_mode_models.add(model)
                    continue
                self.limiter.settle(entry, 0)
                raise LLMError(
                    f"Groq rejected the request: {_error_text(exc)}"[:MAX_ERROR_CHARS]
                ) from exc
            except Exception as exc:
                self.limiter.settle(entry, 0)
                if isinstance(exc, groq.RateLimitError) and is_daily_quota_error(exc):
                    raise LLMQuotaExhausted(
                        f"Groq daily quota exhausted: {_error_text(exc)}"[:MAX_ERROR_CHARS]
                    ) from exc
                if isinstance(exc, groq.AuthenticationError | groq.PermissionDeniedError):
                    raise LLMConfigError(
                        f"Groq rejected the API key: {_error_text(exc)}"[:MAX_ERROR_CHARS]
                    ) from exc
                if not _is_retryable(exc):
                    raise LLMError(f"Groq request failed: {self._describe(exc)}") from exc
                if attempt == self.max_attempts:
                    raise LLMRateLimitError(
                        f"Groq request failed after {attempt} attempts: {self._describe(exc)}"
                    ) from exc
                delay = self._backoff(attempt, exc)
                logger.warning(
                    "Groq request failed (%s); retry %d/%d in %.1fs",
                    self._describe(exc),
                    attempt,
                    self.max_attempts - 1,
                    delay,
                )
                await self.clock.sleep(delay)
                continue

            usage = LLMUsage(
                getattr(response.usage, "prompt_tokens", 0) or 0,
                getattr(response.usage, "completion_tokens", 0) or 0,
            )
            self.limiter.settle(entry, usage.total)
            choice = response.choices[0]
            if choice.finish_reason == "length":
                logger.warning("Groq reply hit max_completion_tokens=%d", self.max_output_tokens)
            return choice.message.content or "", usage
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int, exc: Exception) -> float:
        delay: float = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2.0 ** (attempt - 1))
        if isinstance(exc, groq.RateLimitError):
            retry_after = retry_after_seconds(exc)
            if retry_after is not None:
                if retry_after > MAX_RETRY_AFTER_S:
                    # A long wait means a long-window quota; stop instead of hanging the run.
                    raise LLMQuotaExhausted(
                        f"Groq asked to wait {retry_after:.0f}s: {_error_text(exc)}"[
                            :MAX_ERROR_CHARS
                        ]
                    ) from exc
                delay = max(delay, retry_after)
                self.limiter.block_for(retry_after)
        return delay + self.jitter(0, delay / 4)

    def _describe(self, exc: Exception) -> str:
        if isinstance(exc, groq.APIStatusError):
            message = f"HTTP {exc.status_code}: {_error_text(exc)}"
        else:
            message = f"{type(exc).__name__}: {exc}"
        if self.api_key:
            message = message.replace(self.api_key, "[redacted]")
        return message[:MAX_ERROR_CHARS]


def create_llm_provider(settings: Settings, *, clock: Clock | None = None) -> LLMProvider:
    if settings.llm_provider == "fake":
        return FakeLLMProvider(default_model=settings.extraction_model)
    clock = clock or SystemClock()
    key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else None
    return GroqProvider(
        api_key=key,
        default_model=settings.extraction_model,
        limiter=RateLimiter(settings.llm_tpm_limit, settings.llm_rpm_limit, clock),
        clock=clock,
        max_attempts=settings.llm_max_attempts,
        max_output_tokens=settings.llm_max_output_tokens,
        output_tokens_estimate=settings.llm_output_tokens_estimate,
        reasoning_effort=settings.llm_reasoning_effort,
        timeout_s=settings.llm_timeout_s,
    )
