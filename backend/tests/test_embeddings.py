"""Embedding providers, with a stub in place of the Gemini client (no network)."""

import itertools
import logging
import math
import random
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors as genai_errors
from pydantic import SecretStr

from app.core.config import Settings
from app.models import EMBEDDING_DIM
from app.services import embeddings as emb
from app.services.embeddings import (
    EmbeddingConfigError,
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingQuotaExhausted,
    FakeEmbeddingProvider,
    GeminiEmbeddingProvider,
    create_embedding_provider,
)
from app.services.tokens import count_tokens


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _api_error(code: int, status: str) -> genai_errors.APIError:
    body = {"error": {"code": code, "message": "simulated", "status": status}}
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, body)


def _quota_error(message: str) -> genai_errors.APIError:
    body = {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED"}}
    return genai_errors.ClientError(429, body)


class StubModels:
    """Stands in for ``client.aio.models``; ``script`` lists errors to raise first."""

    def __init__(
        self, dim: int = EMBEDDING_DIM, script: Sequence[BaseException] | None = None
    ) -> None:
        self.dim = dim
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []

    async def embed_content(self, *, model: str, contents: list[str], config: Any) -> Any:
        self.requests.append({"model": model, "contents": list(contents), "config": config})
        if self.script:
            raise self.script.pop(0)
        # Deliberately not normalised: the provider must normalise.
        vectors = [[float(len(text) + i) for i in range(self.dim)] for text in contents]
        return SimpleNamespace(embeddings=[SimpleNamespace(values=v) for v in vectors])


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _gemini(models: StubModels, clock: FakeClock, interval: float = 0.0) -> GeminiEmbeddingProvider:
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return GeminiEmbeddingProvider(
        client=client, min_request_interval_s=interval, sleep=clock.sleep
    )


class TestFakeProvider:
    async def test_deterministic_normalised_768(self) -> None:
        provider = FakeEmbeddingProvider()
        first = await provider.embed(["EBITDA margin guidance", "capex plan"], "document")
        again = await provider.embed(["EBITDA margin guidance"], "query")

        assert provider.dimension == 768
        assert [len(v) for v in first] == [768, 768]
        assert all(math.isclose(_norm(v), 1.0) for v in first)
        assert first[0] == again[0]
        assert first[0] != first[1]
        assert provider.calls == [
            ("document", ["EBITDA margin guidance", "capex plan"]),
            ("query", ["EBITDA margin guidance"]),
        ]

    async def test_shared_words_mean_similar_vectors(self) -> None:
        provider = FakeEmbeddingProvider()
        query, near, far = await provider.embed(
            ["EBITDA margin", "our EBITDA margin improved", "dividend payout policy"], "document"
        )
        assert _cosine(query, near) > _cosine(query, far)


class TestGeminiProvider:
    async def test_purpose_maps_to_task_type(self) -> None:
        models, clock = StubModels(), FakeClock()
        provider = _gemini(models, clock)

        await provider.embed(["a chunk"], "document")
        await provider.embed(["a query"], "query")

        configs = [r["config"] for r in models.requests]
        assert [c.task_type for c in configs] == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
        assert {c.output_dimensionality for c in configs} == {768}
        assert {r["model"] for r in models.requests} == {"gemini-embedding-001"}

    async def test_vectors_are_normalised(self) -> None:
        provider = _gemini(StubModels(), FakeClock())
        vectors = await provider.embed(["one", "three"], "document")
        assert [len(v) for v in vectors] == [768, 768]
        assert all(math.isclose(_norm(v), 1.0) for v in vectors)

    async def test_wrong_dimension_raises(self) -> None:
        provider = _gemini(StubModels(dim=3072), FakeClock())
        with pytest.raises(EmbeddingDimensionError, match="3072-dim"):
            await provider.embed(["text"], "document")

    async def test_batches_of_50_with_min_interval(self) -> None:
        models, clock = StubModels(), FakeClock()
        provider = _gemini(models, clock, interval=1.0)

        vectors = await provider.embed([f"text {i}" for i in range(120)], "document")

        assert len(vectors) == 120
        assert [len(r["contents"]) for r in models.requests] == [50, 50, 20]
        # Two waits between three requests, each close to the 1s minimum.
        assert len(clock.sleeps) == 2
        assert all(0.9 < s <= 1.0 for s in clock.sleeps)

    async def test_retries_rate_limit_and_server_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)
        script = [_api_error(429, "RESOURCE_EXHAUSTED"), _api_error(503, "UNAVAILABLE")]
        models, clock = StubModels(script=script), FakeClock()
        provider = _gemini(models, clock)

        [vector] = await provider.embed(["text"], "document")

        assert len(vector) == 768
        assert len(models.requests) == 3
        assert clock.sleeps == [2.0, 4.0]  # exponential backoff

    async def test_gives_up_after_five_attempts(self) -> None:
        script = [_api_error(429, "RESOURCE_EXHAUSTED")] * 6
        models, clock = StubModels(script=script), FakeClock()
        provider = _gemini(models, clock)

        with pytest.raises(EmbeddingError, match="after 5 attempt"):
            await provider.embed(["text"], "document")
        assert len(models.requests) == 5
        backoffs = clock.sleeps
        assert len(backoffs) == 4
        assert all(b2 > b1 for b1, b2 in itertools.pairwise(backoffs))

    async def test_per_minute_rate_limit_waits_and_retries(self) -> None:
        # Gemini's free-tier wording; the wait does not use up an attempt.
        message = (
            "You exceeded your current quota. Quota exceeded for metric: "
            "generativelanguage.googleapis.com/embed_content_free_tier_tokens, limit: 30000. "
            "Please retry in 53.77s."
        )
        script = [_quota_error(message)] * 6
        models, clock = StubModels(script=script), FakeClock()
        provider = _gemini(models, clock)

        [vector] = await provider.embed(["text"], "document")

        assert len(vector) == 768
        assert len(models.requests) == 7
        assert clock.sleeps == [pytest.approx(54.77)] * 6

    async def test_per_minute_rate_limit_without_a_hint_waits_a_minute(self) -> None:
        models, clock = StubModels(script=[_quota_error("Quota per minute exceeded")]), FakeClock()
        await _gemini(models, clock).embed(["text"], "document")
        assert clock.sleeps == [61.0]

    async def test_daily_quota_is_not_retried(self) -> None:
        script = [_quota_error("Quota exceeded for quota metric 'Requests per day'.")]
        models = StubModels(script=script)
        with pytest.raises(EmbeddingQuotaExhausted, match="daily"):
            await _gemini(models, FakeClock()).embed(["text"], "document")
        assert len(models.requests) == 1

    async def test_client_errors_are_not_retried(self) -> None:
        models = StubModels(script=[_api_error(400, "INVALID_ARGUMENT")])
        provider = _gemini(models, FakeClock())
        with pytest.raises(EmbeddingError, match="400"):
            await provider.embed(["text"], "document")
        assert len(models.requests) == 1

    async def test_long_input_truncated_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        models = StubModels()
        provider = _gemini(models, FakeClock())
        long_text = "revenue " * 3000

        with caplog.at_level(logging.WARNING, logger=emb.__name__):
            await provider.embed([long_text, "short"], "document")

        sent = models.requests[0]["contents"]
        assert len(sent[0]) < len(long_text)
        assert count_tokens(sent[0]) <= emb.GEMINI_TRUNCATE_TO_TOKENS
        assert sent[1] == "short"
        assert "exceeds the 2048-token limit" in caplog.text
        assert "revenue revenue" not in caplog.text  # text itself is never logged

    async def test_missing_key_fails_on_use_not_on_startup(self) -> None:
        provider = GeminiEmbeddingProvider(None)
        with pytest.raises(EmbeddingConfigError, match="GEMINI_API_KEY is not set"):
            await provider.embed(["text"], "query")

    async def test_rejects_unknown_purpose(self) -> None:
        provider = _gemini(StubModels(), FakeClock())
        with pytest.raises(ValueError, match="purpose"):
            await provider.embed(["text"], "classification")  # type: ignore[arg-type]


class TestFactory:
    def _settings(self, **overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "database_url": "postgresql+asyncpg://x/y",
            "test_database_url": "z",
        }
        return Settings(**(base | overrides))

    def test_fake(self) -> None:
        provider = create_embedding_provider(self._settings(embedding_provider="fake"))
        assert isinstance(provider, FakeEmbeddingProvider)

    def test_gemini(self) -> None:
        provider = create_embedding_provider(
            self._settings(
                embedding_provider="gemini",
                gemini_api_key=SecretStr("not-a-real-key"),
                embedding_min_request_interval_s=2.5,
            )
        )
        assert isinstance(provider, GeminiEmbeddingProvider)
        assert provider.model_name == "gemini-embedding-001"
        assert provider.dimension == 768
        assert provider.min_request_interval_s == 2.5
        assert "not-a-real-key" not in repr(self._settings(gemini_api_key="not-a-real-key"))

    def test_dimension_must_match_column(self) -> None:
        with pytest.raises(EmbeddingConfigError, match="vector\\(768\\)"):
            create_embedding_provider(self._settings(embedding_provider="fake", embedding_dim=1024))
