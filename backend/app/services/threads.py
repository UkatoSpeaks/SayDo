"""Read side of threads, verifications, actuals and the scorecard.

Database reads and pure code only: nothing here calls an LLM or an embedding API.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.errors import NotFoundError
from app.models import (
    Chunk,
    Claim,
    ClaimThread,
    Document,
    Verification,
    VerificationStatus,
)
from app.schemas.verification import (
    ActualRead,
    ChunkExcerpt,
    GuidanceBehaviorRead,
    MetricScoreRead,
    SayDoRead,
    ScorecardRead,
    ScorecardThread,
    ThreadDetail,
    ThreadMention,
    ThreadRead,
    VerificationDetail,
    VerificationSummary,
)
from app.services.derived_metrics import ResolvedActual
from app.services.fiscal import parse_period_label
from app.services.linking import company_as_of
from app.services.periods import end_ordinal, label_or_none, ordinal_label
from app.services.scorecard import ScoredThread, compute_scorecard
from app.services.verification import load_actuals


async def get_thread_or_404(session: AsyncSession, thread_id: uuid.UUID) -> ClaimThread:
    thread = await session.get(ClaimThread, thread_id)
    if thread is None:
        raise NotFoundError(f"Thread {thread_id} not found.", code="thread_not_found")
    return thread


async def _latest_verifications(
    session: AsyncSession, thread_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Verification]:
    if not thread_ids:
        return {}
    rows = await session.scalars(
        select(Verification).where(
            Verification.thread_id.in_(thread_ids), Verification.is_latest.is_(True)
        )
    )
    return {v.thread_id: v for v in rows if v.thread_id is not None}


async def _mentions(
    session: AsyncSession, thread_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[Claim, Document]]]:
    if not thread_ids:
        return {}
    rows = await session.execute(
        select(Claim, Document)
        .join(Document, Document.id == Claim.document_id)
        .where(Claim.thread_id.in_(thread_ids))
    )
    out: dict[uuid.UUID, list[tuple[Claim, Document]]] = {}
    for claim, document in rows.tuples():
        assert claim.thread_id is not None
        out.setdefault(claim.thread_id, []).append((claim, document))
    for items in out.values():
        items.sort(
            key=lambda cd: (
                end_ordinal(cd[1].fiscal_year, cd[1].fiscal_quarter),
                cd[0].page_number,
                str(cd[0].id),
            )
        )
    return out


def _thread_read(
    thread: ClaimThread,
    verification: Verification | None,
    mentions: list[tuple[Claim, Document]],
) -> dict[str, object]:
    return {
        "id": thread.id,
        "company_id": thread.company_id,
        "title": thread.title,
        "metric_normalized": thread.metric_normalized,
        "target_fiscal_year": thread.target_fiscal_year,
        "target_fiscal_quarter": thread.target_fiscal_quarter,
        "target_period": label_or_none(thread.target_fiscal_year, thread.target_fiscal_quarter),
        "first_seen_period": thread.first_seen_period,
        "last_seen_period": thread.last_seen_period,
        "mention_count": thread.mention_count,
        "is_dropped": thread.is_dropped,
        "latest_change_type": mentions[-1][0].change_type if mentions else None,
        "status": verification.status if verification else None,
        "verification": VerificationSummary.model_validate(verification) if verification else None,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
    }


async def list_threads(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: VerificationStatus | None = None,
    metric: str | None = None,
    dropped: bool | None = None,
) -> list[ThreadRead]:
    query = select(ClaimThread).where(ClaimThread.company_id == company_id)
    if metric is not None:
        query = query.where(ClaimThread.metric_normalized == metric)
    if dropped is not None:
        query = query.where(ClaimThread.is_dropped.is_(dropped))
    threads = list(await session.scalars(query))
    ids = [t.id for t in threads]
    latest = await _latest_verifications(session, ids)
    mentions = await _mentions(session, ids)
    out = []
    for thread in threads:
        verification = latest.get(thread.id)
        if status is not None and (verification is None or verification.status is not status):
            continue
        out.append(
            ThreadRead.model_validate(
                _thread_read(thread, verification, mentions.get(thread.id, []))
            )
        )
    out.sort(key=lambda t: (_label_ordinal(t.first_seen_period), t.title, str(t.id)))
    return out


def _label_ordinal(label: str) -> int:
    try:
        fiscal_year, quarter = parse_period_label(label)
    except ValueError:
        return 0
    return end_ordinal(fiscal_year, quarter)


async def _chunks(session: AsyncSession, ids: Sequence[uuid.UUID]) -> list[ChunkExcerpt]:
    if not ids:
        return []
    rows = await session.execute(
        select(Chunk, Document.period_label)
        .options(defer(Chunk.embedding))
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(ids))
    )
    by_id = {
        chunk.id: ChunkExcerpt(
            id=chunk.id,
            document_id=chunk.document_id,
            period_label=period,
            speaker=chunk.speaker,
            speaker_role=chunk.speaker_role,
            section=chunk.section,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            text=chunk.text,
        )
        for chunk, period in rows.tuples()
    }
    return [by_id[i] for i in ids if i in by_id]  # keep the stored (ranked) order


async def thread_detail(session: AsyncSession, thread_id: uuid.UUID) -> ThreadDetail:
    thread = await get_thread_or_404(session, thread_id)
    verification = (await _latest_verifications(session, [thread.id])).get(thread.id)
    mentions = (await _mentions(session, [thread.id])).get(thread.id, [])
    base = _thread_read(thread, verification, mentions)
    details = verification.details if verification else {}
    return ThreadDetail.model_validate(
        {
            **base,
            "mentions": [
                ThreadMention(
                    claim_id=claim.id,
                    document_id=claim.document_id,
                    period_label=document.period_label,
                    change_type=claim.change_type,
                    quote=claim.quote,
                    speaker=claim.speaker,
                    page_number=claim.page_number,
                    metric_raw=claim.metric_raw,
                    claim_type=claim.claim_type,
                    target_low=claim.target_low,
                    target_high=claim.target_high,
                    unit=claim.unit,
                    direction=claim.direction,
                    timeframe_raw=claim.timeframe_raw,
                    target_period=label_or_none(
                        claim.target_fiscal_year, claim.target_fiscal_quarter
                    ),
                )
                for claim, document in mentions
            ],
            "latest_verification": (
                VerificationDetail.model_validate(verification) if verification else None
            ),
            "actual_used": details.get("actual"),
            "baseline_used": details.get("baseline"),
            "evidence_chunks": await _chunks(
                session, verification.evidence_chunk_ids if verification else []
            ),
            "explanation_chunks": await _chunks(
                session, verification.explanation_chunk_ids if verification else []
            ),
        }
    )


async def scorecard(session: AsyncSession, company_id: uuid.UUID) -> ScorecardRead:
    threads = list(
        await session.scalars(select(ClaimThread).where(ClaimThread.company_id == company_id))
    )
    ids = [t.id for t in threads]
    latest = await _latest_verifications(session, ids)
    mentions = await _mentions(session, ids)
    scored = []
    for thread in threads:
        verification = latest.get(thread.id)
        scored.append(
            ScoredThread(
                thread_id=thread.id,
                title=thread.title,
                metric=thread.metric_normalized,
                status=verification.status if verification else None,
                gap_pct=verification.gap_pct if verification else None,
                computed_gap=verification.computed_gap if verification else None,
                near_miss=verification.near_miss if verification else False,
                reasoning=verification.reasoning if verification else None,
                target_period=label_or_none(
                    thread.target_fiscal_year, thread.target_fiscal_quarter
                ),
                last_seen_period=thread.last_seen_period,
                change_types=tuple(c.change_type for c, _ in mentions.get(thread.id, [])),
            )
        )
    card = compute_scorecard(scored)
    as_of = await company_as_of(session, company_id)

    def item(t: ScoredThread) -> ScorecardThread:
        return ScorecardThread(
            thread_id=t.thread_id,
            title=t.title,
            metric=t.metric,
            status=t.status,
            gap_pct=t.gap_pct,
            computed_gap=t.computed_gap,
            near_miss=t.near_miss,
            reasoning=t.reasoning,
            target_period=t.target_period,
            last_seen_period=t.last_seen_period,
        )

    return ScorecardRead(
        company_id=company_id,
        as_of_period=ordinal_label(as_of) if as_of is not None else None,
        threads_total=card.threads_total,
        not_verified=card.not_verified,
        counts=card.counts,
        say_do=SayDoRead.model_validate(card.say_do, from_attributes=True),
        by_metric=[
            MetricScoreRead(
                metric=m.metric,
                label=m.label,
                counts=m.counts,
                say_do=SayDoRead.model_validate(m.say_do, from_attributes=True),
            )
            for m in card.by_metric
        ],
        guidance_behavior=GuidanceBehaviorRead.model_validate(card.guidance, from_attributes=True),
        top_misses=[item(t) for t in card.top_misses],
        dropped=[item(t) for t in card.dropped],
        notes=card.notes,
    )


def _actual_read(value: ResolvedActual, reported: bool) -> ActualRead:
    return ActualRead(
        id=value.actual_id if reported else None,
        metric_normalized=value.metric,
        fiscal_year=value.fiscal_year,
        fiscal_quarter=value.fiscal_quarter,
        period_label=value.period_label,
        value=value.value,
        unit=value.unit,
        derived=value.derived,
        derived_from=value.derived_from,
        source_note=value.source_note,
        source_page=value.source_page,
    )


async def list_actuals(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    metric: str | None = None,
    ordinal_from: int | None = None,
    ordinal_to: int | None = None,
) -> tuple[list[ActualRead], int, int]:
    """Reported actuals plus every derivable value (marked ``derived``), filtered."""
    index = await load_actuals(session, company_id)
    reported = index.reported_values()
    derived = index.derived_values()

    def keep(value: ResolvedActual) -> bool:
        if metric is not None and value.metric != metric:
            return False
        ordinal = end_ordinal(value.fiscal_year, value.fiscal_quarter)
        if ordinal_from is not None and ordinal < ordinal_from:
            return False
        return ordinal_to is None or ordinal <= ordinal_to

    rows = [(v, True) for v in reported if keep(v)] + [(v, False) for v in derived if keep(v)]
    rows.sort(key=lambda r: (r[0].metric, r[0].fiscal_year, r[0].fiscal_quarter or 5, r[1]))
    n_reported = sum(1 for _, is_reported in rows if is_reported)
    return [_actual_read(v, r) for v, r in rows], n_reported, len(rows) - n_reported
