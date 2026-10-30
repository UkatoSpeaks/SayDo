"""LLM providers, the repair retry and the rate limiter (no network)."""

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest

from app.services.claim_schema import ExtractionResult
from app.services.llm import (
    FakeClock,
    FakeLLMProvider,
    GroqProvider,
    LLMOutputError,
    LLMQuotaExhausted,
    LLMRateLimitError,
    RateLimiter,
    retry_after_seconds,
)

VALID = {
    "claims": [
        {
            "quote": "We expect margins of 19% to 20% in FY27.",
            "metric_raw": "EBITDA margin",
            "metric_normalized": "ebitda_margin",
            "claim_type": "numeric",
            "target_low": 19,
            "target_high": 20,
            "unit": "percent",
            "direction": "increase",
            "timeframe_raw": "in FY27",
            "target_fiscal_year": 2027,
            "target_fiscal_quarter": None,
            "confidence": 0.9,
        }
    ]
}
API_KEY = "gsk_test_secret_key_value"
_REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


# ------------------------------------------------------------ repair retry


async def test_valid_reply_needs_one_call() -> None:
    fake = FakeLLMProvider([VALID])
    result, usage = await fake.complete_json("sys", "user", ExtractionResult)
    assert result.claims[0].target_low == 19
    assert len(fake.calls) == 1 and usage.tokens_in > 0 and usage.tokens_out > 0


async def test_invalid_reply_gets_one_repair_retry() -> None:
    broken = json.loads(json.dumps(VALID))
    broken["claims"][0]["metric_normalized"] = "headcount"
    fake = FakeLLMProvider(["not json at all", VALID])
    result, usage = await fake.complete_json("sys", "user", ExtractionResult)
    assert len(result.claims) == 1
    assert len(fake.calls) == 2
    repair = fake.calls[1].messages
    assert [m["role"] for m in repair] == ["system", "user", "assistant", "user"]
    assert repair[2]["content"] == "not json at all"
    assert "not valid for the required JSON schema" in repair[3]["content"]
    assert usage.tokens_in > sum(len(m["content"]) // 10 for m in repair[:2])

    fake = FakeLLMProvider([broken, VALID])
    await fake.complete_json("sys", "user", ExtractionResult)
    assert "claims.0.metric_normalized" in fake.calls[1].messages[3]["content"]


async def test_second_invalid_reply_fails() -> None:
    fake = FakeLLMProvider(['{"claims": "nope"}', '{"wrong": []}', VALID])
    with pytest.raises(LLMOutputError) as info:
        await fake.complete_json("sys", "user", ExtractionResult)
    assert len(fake.calls) == 2
    assert info.value.usage.tokens_in > 0  # tokens of both attempts are still reported


async def test_fake_responder_and_default() -> None:
    fake = FakeLLMProvider(
        responder=lambda messages: VALID if "margin" in messages[1]["content"] else {"claims": []}
    )
    assert len((await fake.complete_json("s", "margin talk", ExtractionResult))[0].claims) == 1
    assert (await fake.complete_json("s", "small talk", ExtractionResult))[0].claims == []
    assert (await FakeLLMProvider().complete_json("s", "u", ExtractionResult))[0].claims == []


# ------------------------------------------------------------ rate limiter


async def test_limiter_waits_for_token_budget() -> None:
    clock = FakeClock()
    limiter = RateLimiter(tpm_limit=1000, rpm_limit=30, clock=clock)
    await limiter.acquire(600)
    clock.now = 10
    await limiter.acquire(300)
    assert clock.sleeps == []
    await limiter.acquire(400)  # 900 + 400 > 1000: wait until the first entry expires at t=60
    assert clock.sleeps == [pytest.approx(50)]
    assert limiter.tokens_in_window() == 700


async def test_limiter_settle_frees_budget() -> None:
    clock = FakeClock()
    limiter = RateLimiter(tpm_limit=1000, rpm_limit=30, clock=clock)
    entry = await limiter.acquire(900)  # estimate
    limiter.settle(entry, 200)  # actual usage was far lower
    await limiter.acquire(700)
    assert clock.sleeps == []


async def test_limiter_waits_for_request_budget() -> None:
    clock = FakeClock()
    limiter = RateLimiter(tpm_limit=100_000, rpm_limit=3, clock=clock)
    for t in (0, 5, 20):
        clock.now = t
        await limiter.acquire(10)
    clock.now = 30
    await limiter.acquire(10)
    assert clock.sleeps == [pytest.approx(30)]  # oldest request (t=0) leaves the window at t=60


async def test_limiter_lets_oversized_request_through_on_empty_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(tpm_limit=1000, rpm_limit=30, clock=clock)
    await limiter.acquire(5000)
    assert clock.sleeps == []
    await limiter.acquire(10)
    assert clock.sleeps == [pytest.approx(60)]


async def test_limiter_block_for() -> None:
    clock = FakeClock()
    limiter = RateLimiter(tpm_limit=1000, rpm_limit=30, clock=clock)
    limiter.block_for(12)
    await limiter.acquire(1)
    assert clock.sleeps == [pytest.approx(12)]


# -------------------------------------------------------------------- groq


def _response(content: str, prompt_tokens: int = 500, completion_tokens: int = 120) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def _rate_limited(message: str, headers: dict[str, str] | None = None) -> groq.RateLimitError:
    response = httpx.Response(429, headers=headers or {}, request=_REQUEST)
    body = {"error": {"message": message, "type": "tokens", "code": "rate_limit_exceeded"}}
    return groq.RateLimitError(message, response=response, body=body)


def _status_error(status: int, body: dict[str, Any]) -> groq.APIStatusError:
    response = httpx.Response(status, request=_REQUEST)
    cls: Callable[..., groq.APIStatusError] = {
        400: groq.BadRequestError,
        401: groq.AuthenticationError,
        500: groq.InternalServerError,
        503: groq.InternalServerError,
    }[status]
    return cls(body["error"]["message"], response=response, body=body)


class FakeGroqClient:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _provider(
    script: list[Any], *, model: str = "openai/gpt-oss-120b", **kw: Any
) -> tuple[GroqProvider, FakeGroqClient, FakeClock]:
    clock = FakeClock()
    client = FakeGroqClient(script)
    provider = GroqProvider(
        api_key=API_KEY,
        default_model=model,
        limiter=RateLimiter(8000, 30, clock),
        clock=clock,
        client=client,
        jitter=lambda low, high: 0.0,
        **kw,
    )
    return provider, client, clock


async def test_groq_uses_strict_json_schema_on_gpt_oss() -> None:
    provider, client, _ = _provider([_response(json.dumps(VALID))])
    result, usage = await provider.complete_json("system", "user", ExtractionResult)
    assert result.claims[0].unit is not None
    assert (usage.tokens_in, usage.tokens_out) == (500, 120)
    [request] = client.requests
    assert request["model"] == "openai/gpt-oss-120b"
    assert request["temperature"] == 0 and request["reasoning_effort"] == "low"
    fmt = request["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["required"] == ["claims"]
    assert provider.limiter.tokens_in_window() == 620  # estimate replaced by real usage


async def test_groq_json_mode_for_other_models() -> None:
    provider, client, _ = _provider([_response(json.dumps(VALID))], model="qwen/qwen3.8-27b")
    await provider.complete_json("system", "user", ExtractionResult)
    [request] = client.requests
    assert request["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in request
    assert "JSON schema" in request["messages"][0]["content"]
    assert '"claims"' in request["messages"][0]["content"]


async def test_groq_falls_back_to_json_mode_when_schema_rejected() -> None:
    rejected = _status_error(
        400,
        {
            "error": {
                "message": "response_format json_schema is not supported with this model",
                "code": "invalid_request_error",
            }
        },
    )
    provider, client, _ = _provider([rejected, _response(json.dumps(VALID))])
    await provider.complete_json("system", "user", ExtractionResult)
    assert [r["response_format"]["type"] for r in client.requests] == ["json_schema", "json_object"]


async def test_groq_strict_validation_failure_goes_to_repair() -> None:
    failed = _status_error(
        400,
        {
            "error": {
                "message": "Generated JSON does not match the expected schema.",
                "code": "json_validate_failed",
                "failed_generation": '{"claims": [{"quote": 1}]}',
            }
        },
    )
    provider, client, _ = _provider([failed, _response(json.dumps(VALID))])
    result, _ = await provider.complete_json("system", "user", ExtractionResult)
    assert len(result.claims) == 1
    assert len(client.requests) == 2
    repair_messages = client.requests[1]["messages"]
    assert repair_messages[2] == {"role": "assistant", "content": '{"claims": [{"quote": 1}]}'}
    assert "claims.0" in repair_messages[3]["content"]


async def test_groq_429_respects_retry_after_then_succeeds() -> None:
    limited = _rate_limited(
        "Rate limit reached for model `openai/gpt-oss-120b` on tokens per minute (TPM): "
        "Limit 8000, Used 7900, Requested 900. Please try again in 6.75s.",
        headers={"retry-after": "7"},
    )
    provider, client, clock = _provider([limited, limited, _response(json.dumps(VALID))])
    await provider.complete_json("system", "user", ExtractionResult)
    assert len(client.requests) == 3
    # Backoff is max(exponential, retry-after): 2s -> 7s and 4s -> 7s; the limiter is
    # blocked for the same 7s, so no extra wait is needed after each sleep.
    assert [s for s in clock.sleeps if s > 0] == [pytest.approx(7), pytest.approx(7)]


async def test_groq_429_uses_message_hint_without_header() -> None:
    error = _rate_limited("Rate limit reached ... Please try again in 1m2.5s.")
    assert retry_after_seconds(error) == pytest.approx(62.5)
    assert retry_after_seconds(_rate_limited("Please try again in 850ms.")) == pytest.approx(0.85)
    assert retry_after_seconds(_rate_limited("slow down")) is None


async def test_groq_429_gives_up_after_max_attempts() -> None:
    limited = _rate_limited("Rate limit reached on requests per minute (RPM).")
    provider, client, clock = _provider([limited] * 5)
    with pytest.raises(LLMRateLimitError, match="after 5 attempts"):
        await provider.complete_json("system", "user", ExtractionResult)
    assert len(client.requests) == 5
    assert clock.sleeps == [2, 4, 8, 16]  # exponential backoff (jitter zeroed)


async def test_groq_daily_quota_stops_immediately() -> None:
    quota = _rate_limited(
        "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service "
        "tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199800, Requested 1500. "
        "Please try again in 7m12s.",
        headers={"retry-after": "432"},
    )
    provider, client, clock = _provider([quota, _response(json.dumps(VALID))])
    with pytest.raises(LLMQuotaExhausted, match="daily quota"):
        await provider.complete_json("system", "user", ExtractionResult)
    assert len(client.requests) == 1 and clock.sleeps == []


async def test_groq_very_long_retry_after_is_treated_as_quota() -> None:
    limited = _rate_limited("Rate limit reached.", headers={"retry-after": "3600"})
    provider, client, _ = _provider([limited])
    with pytest.raises(LLMQuotaExhausted):
        await provider.complete_json("system", "user", ExtractionResult)
    assert len(client.requests) == 1


async def test_groq_server_errors_are_retried() -> None:
    boom = _status_error(503, {"error": {"message": "Service Unavailable"}})
    provider, client, clock = _provider([boom, _response(json.dumps(VALID))])
    await provider.complete_json("system", "user", ExtractionResult)
    assert len(client.requests) == 2 and clock.sleeps == [2]


async def test_groq_errors_never_contain_the_key() -> None:
    leak = _status_error(500, {"error": {"message": f"bad key {API_KEY}"}})
    provider, _, _ = _provider([leak] * 2, max_attempts=2)
    with pytest.raises(LLMRateLimitError) as info:
        await provider.complete_json("system", "user", ExtractionResult)
    assert API_KEY not in str(info.value)
    assert "[redacted]" in str(info.value)


async def test_groq_without_key_is_a_config_error() -> None:
    provider = GroqProvider(
        api_key=None,
        default_model="openai/gpt-oss-120b",
        limiter=RateLimiter(8000, 30, FakeClock()),
    )
    with pytest.raises(Exception, match="GROQ_API_KEY is not set"):
        await provider.complete_json("system", "user", ExtractionResult)
