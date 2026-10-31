"""Read side of threads, verifications, actuals and the scorecard.

Database reads and pure code only: nothing here calls an LLM or an embedding API.
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.errors import NotFoundError
from app.models import (
    Actual,
    Chunk,
    Claim,
    ClaimThread,
    Company,
    DocType,
    Document,
    DocumentStatus,
    Verification,
    VerificationStatus,
)
from app.schemas.company import CompanyListItem, CompanySayDo
from app.schemas.verification import (
    ActualRead,
    ChunkExcerpt,
    GuidanceBehaviorRead,
    KeptRatioRead,
    LedgerCell,
    LedgerComparison,
    LedgerRead,
    LedgerRow,
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
from app.services.deterministic_check import OriginalCheck, check_against_original
from app.services.fiscal import parse_period_label
from app.services.ledger import LedgerThread, build_ledger
from app.services.linking import company_as_of, company_calls, mention_from
from app.services.periods import end_ordinal, label_or_none, ordinal_label
from app.services.scorecard import ScoredThread, compute_scorecard, say_do
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


def _actual_snapshot(verification: Verification | None) -> tuple[Decimal | None, str | None]:
    actual = (verification.details or {}).get("actual") if verification else None
    if not isinstance(actual, dict) or actual.get("value") is None:
        return None, None
    try:
        return Decimal(str(actual["value"])), actual.get("unit")
    except InvalidOperation:
        return None, None


def _actual_period(verification: Verification | None) -> tuple[int | None, str | None]:
    """End ordinal and label of the actual the verdict used."""
    actual = (verification.details or {}).get("actual") if verification else None
    if not isinstance(actual, dict) or not actual.get("period"):
        return None, None
    label = str(actual["period"])
    return _label_ordinal(label) or None, label


def _original_check(
    verification: Verification | None, mentions: list[tuple[Claim, Document]]
) -> OriginalCheck:
    value, unit = _actual_snapshot(verification)
    return check_against_original(
        [mention_from(claim, document) for claim, document in mentions],
        verification.status if verification else None,
        value,
        unit,
    )


def _summary(
    verification: Verification | None, mentions: list[tuple[Claim, Document]]
) -> VerificationSummary | None:
    if verification is None:
        return None
    summary = VerificationSummary.model_validate(verification)
    check = _original_check(verification, mentions)
    return summary.model_copy(update={"kept_after_lowering": check.kept_after_lowering})


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
        "verification": _summary(verification, mentions),
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
                VerificationDetail.model_validate(verification).model_copy(
                    update={
                        "kept_after_lowering": _original_check(
                            verification, mentions
                        ).kept_after_lowering
                    }
                )
                if verification
                else None
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
        check = _original_check(verification, mentions.get(thread.id, []))
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
                kept_original=check.kept,
                kept_after_lowering=check.kept_after_lowering,
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
            kept_after_lowering=t.kept_after_lowering,
        )

    return ScorecardRead(
        company_id=company_id,
        as_of_period=ordinal_label(as_of) if as_of is not None else None,
        threads_total=card.threads_total,
        not_verified=card.not_verified,
        counts=card.counts,
        say_do=SayDoRead.model_validate(card.say_do, from_attributes=True),
        say_do_latest=KeptRatioRead.model_validate(card.say_do_latest, from_attributes=True),
        say_do_original=KeptRatioRead.model_validate(card.say_do_original, from_attributes=True),
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


async def _evidence_ordinals(
    session: AsyncSession, verifications: Sequence[Verification]
) -> dict[uuid.UUID, int]:
    """Per verification, the earliest call among its evidence chunks."""
    chunk_ids = {c for v in verifications for c in v.evidence_chunk_ids}
    if not chunk_ids:
        return {}
    rows = await session.execute(
        select(Chunk.id, Document.fiscal_year, Document.fiscal_quarter)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(chunk_ids))
    )
    by_chunk = {cid: end_ordinal(fy, fq) for cid, fy, fq in rows.tuples()}
    out = {}
    for verification in verifications:
        found = [by_chunk[c] for c in verification.evidence_chunk_ids if c in by_chunk]
        if found:
            out[verification.id] = min(found)
    return out


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


async def ledger(session: AsyncSession, company_id: uuid.UUID) -> LedgerRead:
    """The promise x call matrix (``services/ledger.py``). Database reads and pure code."""
    threads = list(
        await session.scalars(select(ClaimThread).where(ClaimThread.company_id == company_id))
    )
    ids = [t.id for t in threads]
    latest = await _latest_verifications(session, ids)
    mentions = await _mentions(session, ids)
    evidence = await _evidence_ordinals(session, list(latest.values()))
    as_of = await company_as_of(session, company_id)
    inputs = []
    for thread in threads:
        verification = latest.get(thread.id)
        items = mentions.get(thread.id, [])
        value, unit = _actual_snapshot(verification)
        actual_ordinal, actual_label = _actual_period(verification)
        inputs.append(
            LedgerThread(
                thread_id=thread.id,
                title=thread.title,
                metric=thread.metric_normalized,
                mentions=[mention_from(claim, document) for claim, document in items],
                change_types={claim.id: claim.change_type for claim, _ in items},
                status=verification.status if verification else None,
                kept_after_lowering=_original_check(verification, items).kept_after_lowering,
                actual_value=value,
                actual_unit=unit,
                actual_ordinal=actual_ordinal,
                actual_label=actual_label,
                evidence_ordinal=evidence.get(verification.id) if verification else None,
                gap_pct=verification.gap_pct if verification else None,
                as_of=as_of,
            )
        )
    result = build_ledger(inputs, await company_calls(session, company_id))
    rows = []
    for row in result.rows:
        comparison = None
        if row.comparison is not None:
            c = row.comparison
            comparison = LedgerComparison(
                unit=c.unit,
                target_low=_float(c.target.low),
                target_high=_float(c.target.high),
                target_kind=c.target.kind.value,
                original_low=_float(c.original.low) if c.original else None,
                original_high=_float(c.original.high) if c.original else None,
                original_kind=c.original.kind.value if c.original else None,
                actual=float(c.actual),
                actual_period=c.actual_period,
                gap_pct=_float(c.gap_pct),
            )
        rows.append(
            LedgerRow(
                thread_id=row.thread_id,
                title=row.title,
                metric_normalized=row.metric,
                unit=row.unit,
                verdict=row.verdict,
                kept_after_lowering=row.kept_after_lowering,
                first_seen_period=row.first_seen_period,
                first_target_period=row.first_target_period,
                target_period=row.target_period,
                slipped_quarters=row.slipped_quarters,
                mention_count=row.mention_count,
                comparison=comparison,
                cells=[LedgerCell(period=c.period, kind=c.kind, text=c.text) for c in row.cells],
            )
        )
    return LedgerRead(
        company_id=company_id,
        as_of_period=ordinal_label(as_of) if as_of is not None else None,
        periods=result.periods,
        rows=rows,
    )


async def company_summaries(
    session: AsyncSession, companies: Sequence[Company]
) -> list[CompanyListItem]:
    """Companies with their Say-Do ratio (latest targets), open count and latest period,
    in three grouped queries whatever the number of companies."""
    ids = [c.id for c in companies]
    counts: dict[uuid.UUID, dict[str, int]] = {i: {} for i in ids}
    latest_period: dict[uuid.UUID, int] = {}
    if ids:
        status_rows = await session.execute(
            select(ClaimThread.company_id, Verification.status, func.count())
            .join(Verification, Verification.thread_id == ClaimThread.id)
            .where(ClaimThread.company_id.in_(ids), Verification.is_latest.is_(True))
            .group_by(ClaimThread.company_id, Verification.status)
        )
        for company_id, status, n in status_rows.tuples():
            counts[company_id][status.value] = n
        call_rows = await session.execute(
            select(
                Document.company_id,
                func.max(Document.fiscal_year * 4 + func.coalesce(Document.fiscal_quarter, 4) - 1),
            )
            .where(
                Document.company_id.in_(ids),
                Document.doc_type == DocType.TRANSCRIPT,
                Document.status == DocumentStatus.EXTRACTED,
            )
            .group_by(Document.company_id)
        )
        actual_rows = await session.execute(
            select(
                Actual.company_id,
                func.max(Actual.fiscal_year * 4 + func.coalesce(Actual.fiscal_quarter, 4) - 1),
            )
            .where(Actual.company_id.in_(ids))
            .group_by(Actual.company_id)
        )
        # Same rule as company_as_of: latest extracted call or entered actual.
        for company_id, ordinal in [*call_rows.tuples(), *actual_rows.tuples()]:
            if ordinal is not None:
                latest_period[company_id] = max(latest_period.get(company_id, 0), int(ordinal))
    out = []
    for company in companies:
        ratio = say_do(counts[company.id])
        out.append(
            CompanyListItem(
                id=company.id,
                name=company.name,
                nse_symbol=company.nse_symbol,
                bse_code=company.bse_code,
                sector=company.sector,
                created_at=company.created_at,
                updated_at=company.updated_at,
                say_do_latest=CompanySayDo(
                    kept=ratio.delivered, total=ratio.denominator, ratio=ratio.ratio
                ),
                open_count=counts[company.id].get(VerificationStatus.IN_PROGRESS.value, 0),
                latest_period=(
                    ordinal_label(latest_period[company.id])
                    if company.id in latest_period
                    else None
                ),
            )
        )
    return out


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
