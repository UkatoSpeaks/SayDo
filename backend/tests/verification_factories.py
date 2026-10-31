"""Builders for linking / verification tests."""

import itertools
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ClaimType, Company, Direction
from app.services.actuals_import import COLUMNS, import_actuals
from app.services.derived_metrics import ActualPoint, ActualsIndex
from app.services.deterministic_check import ThreadView
from app.services.embeddings import FakeEmbeddingProvider
from app.services.linking import Mention, change_types
from app.services.periods import end_ordinal
from scripts.seed_demo import DemoChunk, DemoClaim, _seed_call

_pages = itertools.count(1)


def mention(
    call: str = "Q1FY26",
    *,
    metric: str | None = "ebitda_margin",
    low: str | None = None,
    high: str | None = None,
    unit: str | None = None,
    direction: Direction = Direction.NONE,
    claim_type: ClaimType | None = None,
    target: tuple[int, int | None] | None = (2027, None),
    quote: str = "We expect this.",
    metric_raw: str | None = None,
    page: int | None = None,
) -> Mention:
    """A claim as linking sees it. ``call`` is a label like "Q2FY26"."""
    fy, q = 2000 + int(call[-2:]), int(call[1])
    if claim_type is None:
        claim_type = ClaimType.NUMERIC if low is not None else ClaimType.DIRECTIONAL
    return Mention(
        claim_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        call_ordinal=end_ordinal(fy, q),
        call_label=call,
        page_number=page if page is not None else next(_pages),
        metric=metric,
        metric_raw=metric_raw or (metric or "claim").replace("_", " "),
        claim_type=claim_type,
        target_low=Decimal(low) if low is not None else None,
        target_high=Decimal(high if high is not None else low) if low is not None else None,
        unit=unit,
        direction=direction,
        target_fiscal_year=target[0] if target else None,
        target_fiscal_quarter=target[1] if target else None,
        quote=quote,
    )


def thread(*mentions: Mention, dropped: bool = False) -> ThreadView:
    ordered = sorted(mentions, key=lambda m: m.sort_key)
    return ThreadView(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        title="test thread",
        metric=ordered[-1].metric,
        is_dropped=dropped,
        last_seen_period=ordered[-1].call_label,
        mentions=ordered,
        change_types=list(change_types(ordered)),
    )


def actuals(*rows: tuple[str, int, int | None, str, str]) -> ActualsIndex:
    """rows: (metric, fiscal_year, quarter, value, unit)."""
    return ActualsIndex(
        ActualPoint(metric, fy, q, Decimal(value), unit, id=uuid.uuid4())
        for metric, fy, q, value, unit in rows
    )


def ordinal(label: str) -> int:
    """ "Q4FY26" -> quarter ordinal; "FY26" -> its Q4."""
    if label.startswith("FY"):
        return end_ordinal(2000 + int(label[2:]), None)
    return end_ordinal(2000 + int(label[-2:]), int(label[1]))


Calls = dict[str, tuple[list[DemoChunk], list[DemoClaim]]]


async def seed_company(
    sessionmaker: async_sessionmaker[AsyncSession],
    calls: Calls,
    *,
    symbol: str = "TESTCO",
    actuals_csv: str | None = None,
) -> uuid.UUID:
    """Insert a company with transcripts, chunks (fake embeddings), claims and actuals."""
    embedder = FakeEmbeddingProvider()
    async with sessionmaker() as session:
        company = await session.scalar(select(Company).where(Company.nse_symbol == symbol))
        if company is None:
            company = Company(name=f"{symbol} Ltd", nse_symbol=symbol)
            session.add(company)
            await session.commit()
            await session.refresh(company)
        for label, (chunks, claims) in calls.items():
            await _seed_call(session, company, label, chunks, claims, embedder)
        if actuals_csv:
            report = await import_actuals(session, company, actuals_csv.encode())
            assert report.ok, report.errors
        return company.id


def csv_rows(symbol: str, *rows: str) -> str:
    header = ",".join(COLUMNS)
    return "\n".join([header, *(f"{symbol},{row}" for row in rows)]) + "\n"


def chunk_and_claim(text: str, **claim: Any) -> tuple[DemoChunk, DemoClaim]:
    """One management chunk whose whole text is the claim's quote."""
    from app.models import ChunkSection, SpeakerRole

    chunk = DemoChunk("CFO Name", SpeakerRole.MANAGEMENT, ChunkSection.PREPARED_REMARKS, 2, text)
    return chunk, DemoClaim(chunk=0, quote=text, **claim)
