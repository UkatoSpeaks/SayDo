"""Actuals, claim threads, link/verify runs and the Say-Do scorecard.

GET endpoints read Postgres only. LLM and embedding calls happen in the background
link/verify runs started by the two POST endpoints.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Query, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.deps import (
    EmbedderDep,
    LLMDep,
    SessionDep,
    SessionmakerDep,
    SettingsDep,
    VerifyTrackerDep,
)
from app.core.errors import ConflictError, InvalidInputError, PayloadTooLargeError, error_body
from app.models import MetricNormalized, VerificationRun, VerificationRunKind, VerificationStatus
from app.schemas.common import ErrorResponse
from app.schemas.verification import (
    ActualImportError,
    ActualImportReport,
    ActualListResponse,
    LedgerRead,
    ScorecardRead,
    ThreadDetail,
    ThreadListResponse,
    VerificationRunRead,
)
from app.services.actuals_import import import_actuals
from app.services.documents import get_company_or_404
from app.services.fiscal import parse_period_label
from app.services.periods import end_ordinal, start_ordinal
from app.services.threads import ledger, list_actuals, list_threads, scorecard, thread_detail
from app.services.verification import run_in_background, start_run

router = APIRouter(tags=["verification"])

MAX_CSV_BYTES = 2 * 1024 * 1024


def _ordinal(label: str, name: str, *, upper: bool) -> int:
    try:
        fiscal_year, quarter = parse_period_label(label)
    except ValueError as exc:
        raise InvalidInputError(f"{name}: {exc}", code="invalid_period_label") from exc
    return end_ordinal(fiscal_year, quarter) if upper else start_ordinal(fiscal_year, quarter)


# ------------------------------------------------------------------ actuals


@router.post(
    "/companies/{company_id}/actuals/import",
    response_model=ActualImportReport,
    responses={
        404: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {
            "model": ErrorResponse,
            "description": "Invalid rows; nothing was imported. error.details lists every "
            "problem as {row, field, message}",
        },
    },
)
async def import_company_actuals(
    company_id: uuid.UUID,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="CSV in the actuals template format")],
) -> ActualImportReport | JSONResponse:
    """Upsert reported actuals from a CSV. Every row is validated first; if any row is
    invalid nothing is written and all errors are returned with their row numbers."""
    company = await get_company_or_404(session, company_id)
    content = await file.read(MAX_CSV_BYTES + 1)
    if len(content) > MAX_CSV_BYTES:
        raise PayloadTooLargeError(f"CSV files are limited to {MAX_CSV_BYTES // 1024} KB.")
    report = await import_actuals(session, company, content)
    errors = [ActualImportError(row=e.row, field=e.field, message=e.message) for e in report.errors]
    if errors:
        rows = len({e.row for e in errors})
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_body(
                "invalid_csv",
                f"{len(errors)} problem(s) in {rows} row(s); nothing was imported.",
                [e.model_dump() for e in errors],
            ),
        )
    return ActualImportReport(
        company_id=company_id,
        rows_total=report.rows_total,
        created=report.created,
        updated=report.updated,
        errors=[],
    )


@router.get(
    "/companies/{company_id}/actuals",
    response_model=ActualListResponse,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def get_company_actuals(
    company_id: uuid.UUID,
    session: SessionDep,
    metric: MetricNormalized | None = None,
    period_from: Annotated[str | None, Query(examples=["Q1FY26"])] = None,
    period_to: Annotated[str | None, Query(examples=["FY26"])] = None,
) -> ActualListResponse:
    """Reported actuals plus values derived in code (``derived: true`` with
    ``derived_from``). Derived values are computed on read and never stored."""
    await get_company_or_404(session, company_id)
    low = _ordinal(period_from, "period_from", upper=False) if period_from else None
    high = _ordinal(period_to, "period_to", upper=True) if period_to else None
    if low is not None and high is not None and low > high:
        raise InvalidInputError("period_from must not be after period_to.")
    actuals, reported, derived = await list_actuals(
        session,
        company_id,
        metric=metric.value if metric else None,
        ordinal_from=low,
        ordinal_to=high,
    )
    return ActualListResponse(
        company_id=company_id, reported=reported, derived=derived, actuals=actuals
    )


# --------------------------------------------------------------------- runs


async def _start(
    kind: VerificationRunKind,
    company_id: uuid.UUID,
    session: SessionDep,
    sessionmaker: SessionmakerDep,
    llm: LLMDep,
    embedder: EmbedderDep,
    settings: SettingsDep,
    tracker: VerifyTrackerDep,
    background_tasks: BackgroundTasks,
) -> VerificationRun:
    await get_company_or_404(session, company_id)
    if not tracker.try_start(company_id):
        raise ConflictError(
            "A link/verify run is already active for this company.",
            code="verification_in_progress",
        )
    try:
        run = await start_run(session, company_id, kind, settings)
    except BaseException:
        tracker.finish(company_id)
        raise
    background_tasks.add_task(
        run_in_background, sessionmaker, llm, embedder, settings, run.id, company_id, tracker
    )
    return run


_RUN_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse, "description": "A link/verify run is already active"},
}


@router.post(
    "/companies/{company_id}/link",
    response_model=VerificationRunRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_RUN_RESPONSES,
)
async def link_threads(
    company_id: uuid.UUID,
    session: SessionDep,
    sessionmaker: SessionmakerDep,
    llm: LLMDep,
    embedder: EmbedderDep,
    settings: SettingsDep,
    tracker: VerifyTrackerDep,
    background_tasks: BackgroundTasks,
) -> VerificationRun:
    """Build or refresh the company's claim threads in the background; poll the run."""
    return await _start(
        VerificationRunKind.LINK,
        company_id,
        session,
        sessionmaker,
        llm,
        embedder,
        settings,
        tracker,
        background_tasks,
    )


@router.post(
    "/companies/{company_id}/verify",
    response_model=VerificationRunRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_RUN_RESPONSES,
)
async def verify_threads(
    company_id: uuid.UUID,
    session: SessionDep,
    sessionmaker: SessionmakerDep,
    llm: LLMDep,
    embedder: EmbedderDep,
    settings: SettingsDep,
    tracker: VerifyTrackerDep,
    background_tasks: BackgroundTasks,
) -> VerificationRun:
    """Link, then verify every thread in the background (at most VERIFY_MAX_LLM_CALLS LLM
    calls); poll ``GET /companies/{id}/verification-runs``."""
    return await _start(
        VerificationRunKind.VERIFY,
        company_id,
        session,
        sessionmaker,
        llm,
        embedder,
        settings,
        tracker,
        background_tasks,
    )


@router.get(
    "/companies/{company_id}/verification-runs",
    response_model=list[VerificationRunRead],
    responses={404: {"model": ErrorResponse}},
)
async def list_verification_runs(
    company_id: uuid.UUID,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[VerificationRun]:
    """Link/verify runs, newest first."""
    await get_company_or_404(session, company_id)
    runs = await session.scalars(
        select(VerificationRun)
        .where(VerificationRun.company_id == company_id)
        .order_by(VerificationRun.started_at.desc(), VerificationRun.id)
        .limit(limit)
    )
    return list(runs)


# ------------------------------------------------------------------ threads


@router.get(
    "/companies/{company_id}/threads",
    response_model=ThreadListResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_company_threads(
    company_id: uuid.UUID,
    session: SessionDep,
    status_: Annotated[
        VerificationStatus | None, Query(alias="status", description="Latest verdict")
    ] = None,
    metric: MetricNormalized | None = None,
    dropped: bool | None = None,
) -> ThreadListResponse:
    """Claim threads with their latest verdict, in the order they were first promised."""
    await get_company_or_404(session, company_id)
    threads = await list_threads(
        session,
        company_id,
        status=status_,
        metric=metric.value if metric else None,
        dropped=dropped,
    )
    return ThreadListResponse(company_id=company_id, total=len(threads), threads=threads)


@router.get(
    "/threads/{thread_id}", response_model=ThreadDetail, responses={404: {"model": ErrorResponse}}
)
async def get_thread(thread_id: uuid.UUID, session: SessionDep) -> ThreadDetail:
    """A thread with every mention, its latest verdict, the actual it used, and the text
    of the evidence and explanation chunks."""
    return await thread_detail(session, thread_id)


@router.get(
    "/companies/{company_id}/scorecard",
    response_model=ScorecardRead,
    responses={404: {"model": ErrorResponse}},
)
async def get_scorecard(company_id: uuid.UUID, session: SessionDep) -> ScorecardRead:
    """Say-Do scorecard from the latest verifications."""
    await get_company_or_404(session, company_id)
    return await scorecard(session, company_id)


@router.get(
    "/companies/{company_id}/ledger",
    response_model=LedgerRead,
    responses={404: {"model": ErrorResponse}},
)
async def get_ledger(company_id: uuid.UUID, session: SessionDep) -> LedgerRead:
    """The promise x call matrix: one row per thread (open, missed and dropped first), one
    cell per quarter saying what happened to the promise then. Pure code over stored data."""
    await get_company_or_404(session, company_id)
    return await ledger(session, company_id)
