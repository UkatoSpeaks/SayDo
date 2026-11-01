import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.models import (
    ChangeType,
    ChunkSection,
    ClaimType,
    Direction,
    SpeakerRole,
    VerificationMethod,
    VerificationRunKind,
    VerificationRunStatus,
    VerificationStatus,
)


def _plain(value: Decimal | None) -> Decimal | None:
    # asyncpg decodes numeric 40000 as Decimal("4E+4"); serialise it as "40000".
    return None if value is None else Decimal(format(value, "f"))


# ------------------------------------------------------------------ actuals


class ActualImportError(BaseModel):
    row: int
    field: str | None
    message: str


class ActualImportReport(BaseModel):
    company_id: uuid.UUID
    rows_total: int
    created: int
    updated: int
    errors: list[ActualImportError]


class ActualRead(BaseModel):
    id: uuid.UUID | None
    metric_normalized: str
    fiscal_year: int
    fiscal_quarter: int | None
    period_label: str
    value: Decimal
    unit: str
    derived: bool
    derived_from: str | None
    source_note: str | None
    source_page: int | None
    # "company", "services" or "segment:<name>".
    scope: str = "company"

    @field_validator("value", mode="after")
    @classmethod
    def _plain_value(cls, value: Decimal) -> Decimal:
        return Decimal(format(value, "f"))


class ActualListResponse(BaseModel):
    company_id: uuid.UUID
    reported: int
    derived: int
    actuals: list[ActualRead]


# --------------------------------------------------------------------- runs


class VerificationRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    kind: VerificationRunKind
    status: VerificationRunStatus
    verifier_version: str
    model: str | None
    as_of_period: str | None
    threads_total: int
    threads_created: int
    threads_verified: int
    llm_calls: int
    embedding_calls: int
    tokens_in: int
    tokens_out: int
    status_counts: dict[str, int]
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


# ------------------------------------------------------------------ threads


class VerificationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: VerificationStatus
    method: VerificationMethod | None
    as_of_period: str | None
    verifier_version: str | None
    computed_gap: Decimal | None
    gap_pct: Decimal | None
    near_miss: bool
    reasoning: str
    checked_at: datetime
    # Display flag, computed on read: the latest target was met but the original one was
    # not (the status stays "delivered").
    kept_after_lowering: bool = False

    @field_validator("computed_gap", "gap_pct", mode="after")
    @classmethod
    def _plain_decimal(cls, value: Decimal | None) -> Decimal | None:
        return _plain(value)


class ThreadRead(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    title: str
    metric_normalized: str | None
    target_fiscal_year: int | None
    target_fiscal_quarter: int | None
    target_period: str | None
    first_seen_period: str
    last_seen_period: str
    mention_count: int
    is_dropped: bool
    latest_change_type: ChangeType | None
    status: VerificationStatus | None
    verification: VerificationSummary | None
    created_at: datetime
    updated_at: datetime


class ThreadListResponse(BaseModel):
    company_id: uuid.UUID
    total: int
    threads: list[ThreadRead]


class ThreadMention(BaseModel):
    claim_id: uuid.UUID
    document_id: uuid.UUID
    period_label: str
    change_type: ChangeType | None
    quote: str
    speaker: str | None
    page_number: int
    metric_raw: str
    claim_type: ClaimType
    target_low: Decimal | None
    target_high: Decimal | None
    unit: str | None
    direction: Direction
    timeframe_raw: str | None
    target_period: str | None

    @field_validator("target_low", "target_high", mode="after")
    @classmethod
    def _plain_decimal(cls, value: Decimal | None) -> Decimal | None:
        return _plain(value)


class ChunkExcerpt(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    period_label: str
    speaker: str | None
    speaker_role: SpeakerRole
    section: ChunkSection
    page_start: int
    page_end: int
    text: str


class VerificationDetail(VerificationSummary):
    claim_id: uuid.UUID
    actual_id: uuid.UUID | None
    explanation_summary: str | None
    details: dict[str, Any]


class ThreadDetail(ThreadRead):
    mentions: list[ThreadMention]
    latest_verification: VerificationDetail | None
    # Snapshot of the actual (and baseline) the verdict used, from the verification.
    actual_used: dict[str, Any] | None
    baseline_used: dict[str, Any] | None
    evidence_chunks: list[ChunkExcerpt]
    explanation_chunks: list[ChunkExcerpt]


# ---------------------------------------------------------------- scorecard


class SayDoRead(BaseModel):
    delivered: int
    denominator: int
    ratio: float | None
    low_sample: bool


class KeptRatioRead(BaseModel):
    kept: int
    total: int
    ratio: float | None


class MetricScoreRead(BaseModel):
    metric: str | None
    label: str
    counts: dict[str, int]
    say_do: SayDoRead


class GuidanceBehaviorRead(BaseModel):
    counts: dict[str, int]
    revisions: int
    raised_share: float | None
    lowered_share: float | None
    delayed_share: float | None
    threads_revised: int


class ScorecardThread(BaseModel):
    thread_id: uuid.UUID
    title: str
    metric: str | None
    status: VerificationStatus | None
    gap_pct: Decimal | None
    computed_gap: Decimal | None
    near_miss: bool
    reasoning: str | None
    target_period: str | None
    last_seen_period: str | None
    kept_after_lowering: bool = False

    @field_validator("computed_gap", "gap_pct", mode="after")
    @classmethod
    def _plain_decimal(cls, value: Decimal | None) -> Decimal | None:
        return _plain(value)


class ScorecardRead(BaseModel):
    company_id: uuid.UUID
    as_of_period: str | None
    threads_total: int
    not_verified: int
    counts: dict[str, int]
    say_do: SayDoRead
    # Kept against each thread's latest target (same as say_do) and against its first.
    say_do_latest: KeptRatioRead
    say_do_original: KeptRatioRead
    by_metric: list[MetricScoreRead]
    guidance_behavior: GuidanceBehaviorRead
    top_misses: list[ScorecardThread]
    dropped: list[ScorecardThread]
    # Threads with no resolvable target period ("medium term", "over the next few years"):
    # stored and searchable, but not scored and not counted above.
    long_term: list[ScorecardThread] = []
    notes: list[str]


# ------------------------------------------------------------------- ledger

LedgerCellKind = Literal[
    "said", "raised", "lowered", "moved", "kept", "missed", "silent", "open", "none"
]
TargetKindName = Literal["range", "at_least", "at_most", "exact"]


class LedgerCell(BaseModel):
    period: str
    kind: LedgerCellKind
    text: str


class LedgerComparison(BaseModel):
    """Numbers for a target-vs-actual chart, all in ``unit`` (the latest claim's unit)."""

    unit: str | None
    target_low: float | None
    target_high: float | None
    target_kind: TargetKindName
    # Set only when the first target differs from the latest one.
    original_low: float | None
    original_high: float | None
    original_kind: TargetKindName | None
    actual: float
    actual_period: str
    gap_pct: float | None


class LedgerRow(BaseModel):
    thread_id: uuid.UUID
    title: str
    metric_normalized: str | None
    unit: str | None
    verdict: VerificationStatus | None
    kept_after_lowering: bool
    first_seen_period: str
    first_target_period: str | None
    target_period: str | None
    # Quarters the deadline moved later since it was first stated (0 if never).
    slipped_quarters: int
    mention_count: int
    comparison: LedgerComparison | None
    cells: list[LedgerCell]


class LedgerRead(BaseModel):
    company_id: uuid.UUID
    as_of_period: str | None
    periods: list[str]
    rows: list[LedgerRow]
    # Threads left out because they have no target period (unless include_long_term).
    long_term_hidden: int = 0
