"""Embedding providers.

``GeminiEmbeddingProvider`` calls ``gemini-embedding-001`` through the official
google-genai SDK. ``FakeEmbeddingProvider`` is deterministic and offline, for tests.
Pick one with ``EMBEDDING_PROVIDER``.
"""

import abc
import asyncio
import hashlib
import logging
import math
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.core.config import Settings
from app.models import EMBEDDING_DIM
from app.services.tokens import count_tokens, truncate_to_tokens

logger = logging.getLogger(__name__)

EmbeddingPurpose = Literal["document", "query"]

GEMINI_MODEL = "gemini-embedding-001"
GEMINI_MAX_BATCH = 50
# Gemini's input limit is 2048 tokens. tiktoken only approximates Gemini's
# tokenizer, so inputs are cut a little below it.
GEMINI_MAX_INPUT_TOKENS = 2048
GEMINI_TRUNCATE_TO_TOKENS = 1900
GEMINI_TIMEOUT_MS = 60_000
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 30.0

_TASK_TYPES: dict[EmbeddingPurpose, str] = {
    "document": "RETRIEVAL_DOCUMENT",
    "query": "RETRIEVAL_QUERY",
}


class EmbeddingError(Exception):
    """Embedding failed; the message is safe to show (never contains credentials)."""


class EmbeddingConfigError(EmbeddingError):
    pass


class EmbeddingDimensionError(EmbeddingError):
    pass


def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise EmbeddingError("Embedding model returned an all-zero vector.")
    return [x / norm for x in vector]


class EmbeddingProvider(abc.ABC):
    """Turns texts into L2-normalised vectors of length ``dimension``."""

    model_name: str
    dimension: int
    max_batch_size: int

    @abc.abstractmethod
    async def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> list[list[float]]:
        """Embed ``texts`` (any number; batched internally), preserving order."""


# --------------------------------------------------------------------- fake


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic, offline embeddings for tests.

    Each word is hashed to a few dimensions (feature hashing), so texts that share
    words get similar vectors and search results are meaningful in tests. Every
    call is recorded in ``calls`` as ``(purpose, texts)``.
    """

    model_name = "fake-hash-embedding"
    max_batch_size = GEMINI_MAX_BATCH

    def __init__(self, dimension: int = EMBEDDING_DIM) -> None:
        self.dimension = dimension
        self.calls: list[tuple[EmbeddingPurpose, list[str]]] = []

    def vector(self, text: str) -> list[float]:
        values = [0.0] * self.dimension
        words = re.findall(r"\w+", text.lower()) or [text]
        for word in words:
            digest = hashlib.sha256(word.encode()).digest()
            for k in range(3):
                index = int.from_bytes(digest[4 * k : 4 * k + 4], "big") % self.dimension
                values[index] += 1.0 if digest[12 + k] % 2 else -1.0
        if not any(values):
            values[0] = 1.0
        return l2_normalize(values)

    async def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> list[list[float]]:
        self.calls.append((purpose, list(texts)))
        return [self.vector(text) for text in texts]


# ------------------------------------------------------------------- gemini


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError):
        status = (exc.status or "").upper()
        return exc.code == 429 or exc.code >= 500 or status == "RESOURCE_EXHAUSTED"
    return isinstance(exc, httpx.TransportError | asyncio.TimeoutError)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, genai_errors.APIError):
        return f"Gemini API error {exc.code} {exc.status or ''}: {exc.message or ''}".strip()
    return f"{type(exc).__name__}: {exc}"


class GeminiEmbeddingProvider(EmbeddingProvider):
    model_name = GEMINI_MODEL
    max_batch_size = GEMINI_MAX_BATCH

    def __init__(
        self,
        api_key: str | None = None,
        *,
        dimension: int = EMBEDDING_DIM,
        min_request_interval_s: float = 1.0,
        client: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # The client is created on first use, so the app starts without a key.
        self._api_key = api_key
        self._client = client
        self.dimension = dimension
        self.min_request_interval_s = min_request_interval_s
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_request_at: float | None = None
        self._dimension_checked = False

    def _get_client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise EmbeddingConfigError(
                    "GEMINI_API_KEY is not set; add it to backend/.env or set "
                    "EMBEDDING_PROVIDER=fake."
                )
            self._client = genai.Client(
                api_key=self._api_key,
                http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
            )
        return self._client

    async def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> list[list[float]]:
        if purpose not in _TASK_TYPES:
            raise ValueError(f"Unknown embedding purpose: {purpose!r}")
        prepared = [self._prepare(text) for text in texts]
        vectors: list[list[float]] = []
        for i in range(0, len(prepared), self.max_batch_size):
            batch = prepared[i : i + self.max_batch_size]
            vectors.extend(await self._embed_batch(batch, purpose))
        return vectors

    def _prepare(self, text: str) -> str:
        if not text.strip():
            raise EmbeddingError("Cannot embed an empty text.")
        tokens = count_tokens(text)
        if tokens > GEMINI_MAX_INPUT_TOKENS:
            logger.warning(
                "Embedding input of ~%d tokens exceeds the %d-token limit; truncating to %d.",
                tokens,
                GEMINI_MAX_INPUT_TOKENS,
                GEMINI_TRUNCATE_TO_TOKENS,
            )
            return truncate_to_tokens(text, GEMINI_TRUNCATE_TO_TOKENS)
        return text

    async def _wait_for_slot(self) -> None:
        """Keep at least ``min_request_interval_s`` between request starts."""
        if self._last_request_at is not None:
            wait = self.min_request_interval_s - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)
        self._last_request_at = time.monotonic()

    async def _embed_batch(self, batch: list[str], purpose: EmbeddingPurpose) -> list[list[float]]:
        client = self._get_client()
        config = genai_types.EmbedContentConfig(
            task_type=_TASK_TYPES[purpose], output_dimensionality=self.dimension
        )
        for attempt in range(1, MAX_ATTEMPTS + 1):
            async with self._lock:
                await self._wait_for_slot()
                try:
                    response = await client.aio.models.embed_content(
                        model=self.model_name, contents=batch, config=config
                    )
                except Exception as exc:
                    if not _is_retryable(exc) or attempt == MAX_ATTEMPTS:
                        raise EmbeddingError(
                            f"Embedding request failed after {attempt} attempt(s): {_describe(exc)}"
                        ) from exc
                    delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (attempt - 1))
                    delay += random.uniform(0, delay / 2)
                    logger.warning(
                        "Embedding request failed (%s); retry %d/%d in %.1fs",
                        _describe(exc),
                        attempt,
                        MAX_ATTEMPTS - 1,
                        delay,
                    )
                else:
                    return self._parse_response(response, len(batch))
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _parse_response(self, response: Any, expected: int) -> list[list[float]]:
        embeddings = response.embeddings or []
        if len(embeddings) != expected:
            raise EmbeddingError(
                f"Embedding API returned {len(embeddings)} vectors for {expected} inputs."
            )
        vectors = []
        for embedding in embeddings:
            values = list(embedding.values or [])
            if len(values) != self.dimension:
                raise EmbeddingDimensionError(
                    f"{self.model_name} returned {len(values)}-dim vectors but "
                    f"EMBEDDING_DIM is {self.dimension}; the chunks.embedding column is "
                    f"vector({EMBEDDING_DIM})."
                )
            vectors.append(l2_normalize(values))
        if not self._dimension_checked:
            self._dimension_checked = True
            logger.info("%s returns %d-dim vectors", self.model_name, self.dimension)
        return vectors


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_dim != EMBEDDING_DIM:
        raise EmbeddingConfigError(
            f"EMBEDDING_DIM={settings.embedding_dim} does not match the database column "
            f"vector({EMBEDDING_DIM}); a new migration is needed to change it."
        )
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider(settings.embedding_dim)
    key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None
    return GeminiEmbeddingProvider(
        key,
        dimension=settings.embedding_dim,
        min_request_interval_s=settings.embedding_min_request_interval_s,
    )
