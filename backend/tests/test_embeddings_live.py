"""Calls the real Gemini API. Run with: uv run pytest -m live -s tests/test_embeddings_live.py"""

import math
import time

import pytest

from app.core.config import Settings
from app.services.embeddings import GeminiEmbeddingProvider

pytestmark = pytest.mark.live


async def test_gemini_embeds_two_texts() -> None:
    settings = Settings()
    if settings.gemini_api_key is None:
        pytest.skip("GEMINI_API_KEY is not set")
    provider = GeminiEmbeddingProvider(
        settings.gemini_api_key.get_secret_value(),
        dimension=settings.embedding_dim,
        min_request_interval_s=settings.embedding_min_request_interval_s,
    )
    texts = [
        "[Acme | Q2FY26 | Rajesh Kumar (management) | Q&A]\nWe expect revenue growth of 15-18%.",
        "[Acme | Q2FY26 | Anita Desai (management) | Prepared remarks]\n"
        "Net debt fell to Rs. 310 crore.",
    ]

    started = time.perf_counter()
    vectors = await provider.embed(texts, "document")
    doc_latency = time.perf_counter() - started
    started = time.perf_counter()
    [query] = await provider.embed(["revenue growth guidance"], "query")
    query_latency = time.perf_counter() - started

    norms = [math.sqrt(sum(x * x for x in v)) for v in [*vectors, query]]
    similarity = [sum(a * b for a, b in zip(query, v, strict=True)) for v in vectors]
    print(
        f"\ndims={[len(v) for v in vectors]} norms={[round(n, 6) for n in norms]} "
        f"doc_latency={doc_latency:.2f}s query_latency={query_latency:.2f}s "
        f"query_similarity={[round(s, 3) for s in similarity]}"
    )
    assert [len(v) for v in vectors] == [768, 768]
    assert all(math.isclose(n, 1.0, rel_tol=1e-6) for n in norms)
    assert similarity[0] > similarity[1]  # the growth chunk is closer to a growth query
