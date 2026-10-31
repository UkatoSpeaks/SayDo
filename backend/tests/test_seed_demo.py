"""scripts/seed_demo.py: realistic demo data, idempotent, --reset."""

from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import (
    Actual,
    Chunk,
    Claim,
    ClaimThread,
    Company,
    Document,
    Verification,
)
from app.services.threads import list_threads, scorecard
from scripts.seed_demo import seed_demo

TABLES = (Company, Document, Chunk, Claim, Actual, ClaimThread)


async def _counts(app: FastAPI) -> dict[str, int]:
    async with app.state.sessionmaker() as session:
        counts = {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model)) or 0
            for model in TABLES
        }
        counts["latest_verifications"] = (
            await session.scalar(select(func.count()).where(Verification.is_latest)) or 0
        )
        counts["verifications"] = (
            await session.scalar(select(func.count()).select_from(Verification)) or 0
        )
    return counts


async def test_seed_demo_runs_twice_without_duplicates(app: FastAPI) -> None:
    first_id = await seed_demo(app.state.sessionmaker, app.state.settings)
    first = await _counts(app)
    assert first == {
        "companies": 1,
        "documents": 4,
        "chunks": 19,
        "claims": 21,
        "actuals": 17,
        "claim_threads": 10,
        "latest_verifications": 10,
        "verifications": 10,
    }

    second_id = await seed_demo(app.state.sessionmaker, app.state.settings)
    second = await _counts(app)
    assert second_id == first_id
    assert second == first | {"verifications": 20}  # history kept, one latest per thread

    reset_id = await seed_demo(app.state.sessionmaker, app.state.settings, reset=True)
    assert reset_id != first_id
    assert await _counts(app) == first


async def test_seed_demo_covers_every_scenario(app: FastAPI) -> None:
    company_id = await seed_demo(app.state.sessionmaker, app.state.settings)
    async with app.state.sessionmaker() as session:
        threads = {t.title: t for t in await list_threads(session, company_id)}
        card = await scorecard(session, company_id)
    status = {title: t.status.value if t.status else None for title, t in threads.items()}
    assert status == {
        "EBITDA margin 18–19% in FY26": "delivered",
        "Revenue ₹5,000 crore in FY26": "missed",
        "Line 4 commissioning by Q1FY27": "in_progress",
        "Revenue growth 12–14% in FY26": "delivered",
        "PAT ₹400 crore in FY26": "delivered",
        "Net debt ₹200 crore by FY27": "dropped",
        "Capacity utilization to increase in FY26": "delivered",
        "Aura premium range launch by Q4FY26": "delivered",
        "Capex ₹300–350 crore in FY26": "delivered",
        "EBITDA margin 19–20% in FY27": "in_progress",
    }
    assert threads["Line 4 commissioning by Q1FY27"].mention_count == 4
    assert card.say_do.ratio == 0.75 and card.say_do.denominator == 8
    assert card.guidance_behavior.counts["delayed"] == 2
    assert card.guidance_behavior.counts["raised"] == 1
    assert card.guidance_behavior.counts["lowered"] == 1
    assert [t.title for t in card.top_misses] == ["Revenue ₹5,000 crore in FY26"]
    assert str(card.top_misses[0].gap_pct) == "-7.60"
