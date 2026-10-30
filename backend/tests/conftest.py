"""Shared fixtures.

DB-backed tests run against TEST_DATABASE_URL. The schema is rebuilt with Alembic
once per session (downgrade to base, then upgrade to head) and every table is
truncated after each test.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import make_url, text

from app.core.config import BACKEND_DIR, Settings
from app.main import create_app

TABLES = (
    "verifications",
    "actuals",
    "claims",
    "extraction_runs",
    "chunks",
    "document_pages",
    "documents",
    "companies",
)


@pytest.fixture(scope="session")
def base_settings() -> Settings:
    defaults = Settings()
    if make_url(defaults.test_database_url) == make_url(defaults.database_url):
        pytest.exit("TEST_DATABASE_URL must differ from DATABASE_URL; refusing to wipe it.")
    return Settings(
        environment="test",
        database_url=defaults.test_database_url,
        log_level="WARNING",
        # Tests never call the real embedding or LLM APIs (see the *_live.py tests).
        embedding_provider="fake",
        llm_provider="fake",
        auto_extract=False,
    )


async def _ensure_database_exists(database_url: str) -> None:
    url = make_url(database_url)
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database="postgres",
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_db(base_settings: Settings) -> Iterator[str]:
    asyncio.run(_ensure_database_exists(base_settings.database_url))

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = base_settings.database_url
    config.attributes["configure_logger"] = False
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield base_settings.database_url


@pytest.fixture
def settings(base_settings: Settings, migrated_db: str, tmp_path: Path) -> Settings:
    return base_settings.model_copy(update={"upload_dir": tmp_path / "uploads"})


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    try:
        yield application
    finally:
        async with application.state.engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
        await application.state.engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
