"""The Say-Do scorecard: pure code over a company's latest verifications.

- Say-Do ratio = delivered / (delivered + missed + dropped). In-progress and
  unverifiable promises are excluded; the denominator is always shown and
  ``low_sample`` is set below ``LOW_SAMPLE_THRESHOLD`` so 2/2 = 100% isn't read
  as a strong record.
- Breakdown per metric, guidance behaviour (how often targets were raised,
  lowered or delayed, from every mention's change_type), the biggest misses by
  |gap_pct| and every dropped promise.
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from app.models import ChangeType, VerificationStatus
from app.services.metrics import label

LOW_SAMPLE_THRESHOLD = 5
TOP_MISSES = 5
_SCORED = (VerificationStatus.DELIVERED, VerificationStatus.MISSED, VerificationStatus.DROPPED)


@dataclass(frozen=True)
class ScoredThread:
    thread_id: uuid.UUID
    title: str
    metric: str | None
    status: VerificationStatus | None  # None: never verified
    gap_pct: Decimal | None = None
    computed_gap: Decimal | None = None
    near_miss: bool = False
    reasoning: str | None = None
    target_period: str | None = None
    last_seen_period: str | None = None
    change_types: tuple[ChangeType | None, ...] = ()


@dataclass
class SayDo:
    delivered: int
    denominator: int
    ratio: float | None
    low_sample: bool


@dataclass
class MetricScore:
    metric: str | None
    label: str
    counts: dict[str, int]
    say_do: SayDo


@dataclass
class GuidanceBehavior:
    counts: dict[str, int]
    revisions: int  # raised + lowered + delayed
    raised_share: float | None
    lowered_share: float | None
    delayed_share: float | None
    threads_revised: int


@dataclass
class Scorecard:
    counts: dict[str, int]
    say_do: SayDo
    by_metric: list[MetricScore]
    guidance: GuidanceBehavior
    top_misses: list[ScoredThread]
    dropped: list[ScoredThread]
    threads_total: int
    not_verified: int = 0
    notes: list[str] = field(default_factory=list)


def _counts(threads: list[ScoredThread]) -> dict[str, int]:
    tally = Counter(t.status.value for t in threads if t.status is not None)
    return {status.value: tally.get(status.value, 0) for status in VerificationStatus}


def say_do(counts: dict[str, int]) -> SayDo:
    delivered = counts.get(VerificationStatus.DELIVERED.value, 0)
    denominator = sum(counts.get(s.value, 0) for s in _SCORED)
    return SayDo(
        delivered=delivered,
        denominator=denominator,
        ratio=round(delivered / denominator, 4) if denominator else None,
        low_sample=denominator < LOW_SAMPLE_THRESHOLD,
    )


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def compute_scorecard(threads: list[ScoredThread]) -> Scorecard:
    counts = _counts(threads)
    overall = say_do(counts)

    by_metric: dict[str | None, list[ScoredThread]] = {}
    for thread in threads:
        by_metric.setdefault(thread.metric, []).append(thread)
    metric_scores = [
        MetricScore(metric, label(metric), _counts(group), say_do(_counts(group)))
        for metric, group in sorted(by_metric.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
    ]

    changes = Counter(c.value for t in threads for c in t.change_types if c is not None)
    change_counts = {c.value: changes.get(c.value, 0) for c in ChangeType}
    raised = change_counts[ChangeType.RAISED.value]
    lowered = change_counts[ChangeType.LOWERED.value]
    delayed = change_counts[ChangeType.DELAYED.value]
    revisions = raised + lowered + delayed
    revised_kinds = {ChangeType.RAISED, ChangeType.LOWERED, ChangeType.DELAYED}
    guidance = GuidanceBehavior(
        counts=change_counts,
        revisions=revisions,
        raised_share=_share(raised, revisions),
        lowered_share=_share(lowered, revisions),
        delayed_share=_share(delayed, revisions),
        threads_revised=sum(1 for t in threads if revised_kinds & set(t.change_types)),
    )

    misses = [t for t in threads if t.status is VerificationStatus.MISSED and t.gap_pct is not None]
    misses.sort(key=lambda t: (-abs(t.gap_pct or Decimal(0)), t.title))
    # Misses without a measurable gap (directional, LLM-judged) come after.
    misses += sorted(
        (t for t in threads if t.status is VerificationStatus.MISSED and t.gap_pct is None),
        key=lambda t: t.title,
    )
    dropped = sorted(
        (t for t in threads if t.status is VerificationStatus.DROPPED),
        key=lambda t: (t.last_seen_period or "", t.title),
    )
    scorecard = Scorecard(
        counts=counts,
        say_do=overall,
        by_metric=metric_scores,
        guidance=guidance,
        top_misses=misses[:TOP_MISSES],
        dropped=dropped,
        threads_total=len(threads),
        not_verified=sum(1 for t in threads if t.status is None),
    )
    if overall.low_sample:
        scorecard.notes.append(
            f"Only {overall.denominator} promise(s) can be scored so far (fewer than "
            f"{LOW_SAMPLE_THRESHOLD}); treat the ratio as indicative."
        )
    if scorecard.not_verified:
        scorecard.notes.append(
            f"{scorecard.not_verified} thread(s) have not been verified yet; run "
            "POST /companies/{id}/verify."
        )
    return scorecard
