"""Read endpoints never call Groq or Gemini: every GET route runs with providers that
raise if they are touched. (POST /companies/{id}/search is the one endpoint that may
embed a query; it is not a GET.)"""

import re
import uuid

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import select

from app.models import Claim, ClaimThread, Document
from app.services.embeddings import EmbeddingProvider, EmbeddingPurpose
from app.services.llm import LLMProvider, LLMUsage, Message
from scripts.seed_demo import seed_demo


class ExplodingLLM(LLMProvider):
    default_model = "never"

    async def _complete(
        self, messages: list[Message], schema: type[BaseModel], model: str
    ) -> tuple[str, LLMUsage]:
        raise AssertionError("A read endpoint called the LLM")


class ExplodingEmbedder(EmbeddingProvider):
    model_name = "never"
    dimension = 768
    max_batch_size = 1

    async def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> list[list[float]]:
        raise AssertionError("A read endpoint called the embedding API")


async def test_every_get_endpoint_runs_without_providers(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    company_id = await seed_demo(app.state.sessionmaker, app.state.settings)
    app.state.llm = ExplodingLLM()
    app.state.embedder = ExplodingEmbedder()

    async with app.state.sessionmaker() as session:
        ids: dict[str, uuid.UUID] = {
            "company_id": company_id,
            "document_id": (await session.scalars(select(Document.id).limit(1))).one(),
            "claim_id": (await session.scalars(select(Claim.id).limit(1))).one(),
            "thread_id": (await session.scalars(select(ClaimThread.id).limit(1))).one(),
        }

    # Every documented GET endpoint (FastAPI nests included routers, so use the schema).
    get_routes = [path for path, ops in app.openapi()["paths"].items() if "get" in ops]
    assert "/companies/{company_id}/scorecard" in get_routes
    assert "/threads/{thread_id}" in get_routes
    checked = []
    for path in get_routes:
        url = re.sub(r"\{(\w+)\}", lambda m: str(ids[m.group(1)]), path)
        response = await client.get(url)
        assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
        checked.append(path)
    assert len(checked) == len(get_routes) >= 14


async def test_exploding_providers_do_explode() -> None:
    with pytest.raises(AssertionError):
        await ExplodingLLM().complete_json("s", "u", BaseModel)
    with pytest.raises(AssertionError):
        await ExplodingEmbedder().embed(["x"], "query")
