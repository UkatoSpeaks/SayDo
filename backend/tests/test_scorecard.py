"""Say-Do scorecard math (pure code)."""

import uuid
from decimal import Decimal

from app.models import ChangeType, VerificationStatus
from app.services.scorecard import ScoredThread, compute_scorecard, say_do

S = VerificationStatus
C = ChangeType


def scored(
    status: VerificationStatus | None,
    metric: str = "revenue",
    gap_pct: str | None = None,
    changes: tuple[ChangeType, ...] = (C.NEW,),
    title: str | None = None,
) -> ScoredThread:
    return ScoredThread(
        thread_id=uuid.uuid4(),
        title=title or f"{metric} {status}",
        metric=metric,
        status=status,
        gap_pct=Decimal(gap_pct) if gap_pct is not None else None,
        change_types=changes,
    )


def test_ratio_excludes_in_progress_and_unverifiable() -> None:
    card = compute_scorecard(
        [
            scored(S.DELIVERED),
            scored(S.DELIVERED),
            scored(S.DELIVERED),
            scored(S.MISSED, gap_pct="-5"),
            scored(S.DROPPED),
            scored(S.IN_PROGRESS),
            scored(S.UNVERIFIABLE),
        ]
    )
    assert card.say_do.delivered == 3
    assert card.say_do.denominator == 5
    assert card.say_do.ratio == 0.6
    assert not card.say_do.low_sample
    assert card.counts == {
        "delivered": 3,
        "missed": 1,
        "in_progress": 1,
        "dropped": 1,
        "unverifiable": 1,
    }


def test_low_sample_flag() -> None:
    card = compute_scorecard([scored(S.DELIVERED), scored(S.DELIVERED)])
    assert card.say_do.ratio == 1.0 and card.say_do.denominator == 2
    assert card.say_do.low_sample
    assert any("fewer than 5" in note for note in card.notes)


def test_no_scored_promises_gives_no_ratio() -> None:
    result = say_do({"in_progress": 3})
    assert result.ratio is None and result.denominator == 0 and result.low_sample


def test_breakdown_by_metric() -> None:
    card = compute_scorecard(
        [scored(S.DELIVERED, "revenue"), scored(S.MISSED, "revenue"), scored(S.DELIVERED, "pat")]
    )
    by_metric = {m.metric: m for m in card.by_metric}
    assert by_metric["revenue"].say_do.ratio == 0.5
    assert by_metric["revenue"].label == "Revenue"
    assert by_metric["pat"].counts["delivered"] == 1


def test_guidance_behavior() -> None:
    card = compute_scorecard(
        [
            scored(S.DELIVERED, changes=(C.NEW, C.RAISED, C.REITERATED)),
            scored(S.DELIVERED, changes=(C.NEW, C.LOWERED)),
            scored(S.IN_PROGRESS, changes=(C.NEW, C.DELAYED, C.DELAYED)),
            scored(S.MISSED, changes=(C.NEW,)),
        ]
    )
    guidance = card.guidance
    assert guidance.counts["raised"] == 1 and guidance.counts["delayed"] == 2
    assert guidance.revisions == 4
    assert guidance.raised_share == 0.25 and guidance.delayed_share == 0.5
    assert guidance.threads_revised == 3


def test_top_misses_by_absolute_gap_and_all_dropped() -> None:
    misses = [
        scored(S.MISSED, gap_pct=g, title=f"miss {g}")
        for g in ("-2", "-30", "-7", "-1", "-12", "-4")
    ]
    dropped = [scored(S.DROPPED, title=f"dropped {i}") for i in range(7)]
    card = compute_scorecard([*misses, scored(S.MISSED, title="no gap"), *dropped])
    assert [t.title for t in card.top_misses] == [
        "miss -30",
        "miss -12",
        "miss -7",
        "miss -4",
        "miss -2",
    ]
    assert len(card.dropped) == 7


def test_unverified_threads_are_counted_separately() -> None:
    card = compute_scorecard([scored(None), scored(S.DELIVERED)])
    assert card.not_verified == 1 and card.threads_total == 2
    assert card.say_do.denominator == 1
