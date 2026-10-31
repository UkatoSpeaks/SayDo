"""Deterministic verdicts for numeric and directional promises. Pure code, no LLM.

Rules (``as_of`` is the company's latest call/actuals period, never today's date):

- Target period not over as of ``as_of`` -> in_progress.
- Over, but no actual reported or derivable -> unverifiable ("actual not yet entered").
- Units are converted (lakh/crore/bn, bps/percent); if they can't be, unverifiable.
- Numeric: the actual is compared with the thread's LATEST target (after raises and
  lowers); the original target stays visible in the explanation.
  - A range [low, high]: inside -> delivered. Outside: beyond the good end (above for
    higher-is-better metrics, below for debt) -> delivered (exceeded); otherwise missed.
    Neutral metrics (capex) are missed on either side.
  - Single-sided targets ("at least 25%", "up to ₹300 crore") respect their side. A
    single figure ("20% margin") is single-sided too, by the claim's direction or else
    the metric's polarity (a floor for margins, a ceiling for debt); neutral metrics
    need the exact figure.
  - Misses carry ``computed_gap`` (claim unit) and ``gap_pct`` (gap / target * 100);
    a miss within ``tolerance_pct`` is still missed but flagged ``near_miss``.
- Directional: the target-period actual vs a baseline (same period last year for
  growth metrics; otherwise the period the claim was made in, at the target's
  granularity). Changes within ±``band_pct`` % count as maintained.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.models import ChangeType, ClaimUnit, Direction, VerificationMethod, VerificationStatus
from app.services.derived_metrics import ActualsIndex, ResolvedActual
from app.services.linking import Mention, target_change
from app.services.metrics import GROWTH_METRICS, Polarity, label, polarity
from app.services.periods import end_ordinal, from_ordinal, label_or_none, ordinal_label
from app.services.units import convert, format_range, format_value, parse_unit, plain

HUNDRED = Decimal(100)
_CURRENCY = r"(?:rs\.?|inr|₹)?\s*"
_AT_LEAST_RE = re.compile(
    r"\b(?:at\s+least|minimum(?:\s+of)?|more\s+than|over|above|in\s+excess\s+of|upwards\s+of"
    r"|north\s+of|exceed(?:ing)?|no\s+less\s+than|not\s+less\s+than)\s+(?:of\s+)?"
    + _CURRENCY
    + r"\d|\d\s*%?\s*\+|\d\s*%?\s*(?:or|and)\s+(?:more|above|higher)\b",
    re.IGNORECASE,
)
_AT_MOST_RE = re.compile(
    r"\b(?:at\s+most|up\s+to|upto|maximum(?:\s+of)?|below|under|less\s+than|not\s+more\s+than"
    r"|no\s+more\s+than|capped\s+at|within)\s+(?:of\s+)?"
    + _CURRENCY
    + r"\d|\d\s*%?\s*(?:or|and)\s+(?:less|below|lower)\b",
    re.IGNORECASE,
)


OutcomeFn = Callable[..., "Outcome"]


class TargetKind(StrEnum):
    RANGE = "range"
    AT_LEAST = "at_least"
    AT_MOST = "at_most"
    EXACT = "exact"


@dataclass(frozen=True)
class Bounds:
    low: Decimal | None
    high: Decimal | None
    kind: TargetKind


@dataclass
class ThreadView:
    """A thread with its mentions in call order (what verification works on)."""

    id: uuid.UUID
    company_id: uuid.UUID
    title: str
    metric: str | None
    is_dropped: bool
    last_seen_period: str
    mentions: list[Mention]
    change_types: list[ChangeType | None] = field(default_factory=list)

    @property
    def first(self) -> Mention:
        return self.mentions[0]

    @property
    def latest(self) -> Mention:
        return self.mentions[-1]

    @property
    def was_delayed(self) -> bool:
        return ChangeType.DELAYED in self.change_types

    @property
    def original_target(self) -> Mention:
        """The first mention that stated a target (or the first mention)."""
        return next((m for m in self.mentions if m.target_low is not None), self.first)


@dataclass
class Outcome:
    status: VerificationStatus
    reasoning: str
    method: VerificationMethod | None = VerificationMethod.DETERMINISTIC
    actual: ResolvedActual | None = None
    baseline: ResolvedActual | None = None
    computed_gap: Decimal | None = None
    gap_pct: Decimal | None = None
    near_miss: bool = False
    evidence_chunk_ids: list[uuid.UUID] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ targets


def target_bounds(mention: Mention) -> Bounds | None:
    """The target as a range or a one-sided bound (``None`` without a numeric target)."""
    low = mention.target_low
    if low is None:
        return None
    high = mention.target_high if mention.target_high is not None else low
    if low != high:
        return Bounds(low, high, TargetKind.RANGE)
    if _AT_LEAST_RE.search(mention.quote):
        return Bounds(low, None, TargetKind.AT_LEAST)
    if _AT_MOST_RE.search(mention.quote):
        return Bounds(None, high, TargetKind.AT_MOST)
    if mention.direction is Direction.INCREASE:
        return Bounds(low, None, TargetKind.AT_LEAST)
    if mention.direction is Direction.DECREASE:
        return Bounds(None, high, TargetKind.AT_MOST)
    side = polarity(mention.metric)
    if side is Polarity.HIGHER_IS_BETTER:
        return Bounds(low, None, TargetKind.AT_LEAST)
    if side is Polarity.LOWER_IS_BETTER:
        return Bounds(None, high, TargetKind.AT_MOST)
    return Bounds(low, high, TargetKind.EXACT)


def describe_target(mention: Mention, bounds: Bounds | None = None) -> str:
    bounds = bounds or target_bounds(mention)
    unit = mention.unit
    if bounds is None:
        return {
            Direction.INCREASE: "an increase",
            Direction.DECREASE: "a decrease",
            Direction.MAINTAIN: "no change",
        }.get(mention.direction, "no stated target")
    if bounds.kind is TargetKind.RANGE:
        assert bounds.low is not None and bounds.high is not None
        return format_range(bounds.low, bounds.high, unit)
    if bounds.kind is TargetKind.AT_LEAST:
        assert bounds.low is not None
        return f"at least {format_value(bounds.low, unit)}"
    if bounds.kind is TargetKind.AT_MOST:
        assert bounds.high is not None
        return f"at most {format_value(bounds.high, unit)}"
    assert bounds.low is not None
    return format_value(bounds.low, unit)


def target_snapshot(mention: Mention) -> dict[str, Any]:
    return {
        "claim_id": str(mention.claim_id),
        "said_on": mention.call_label,
        "target_low": plain(mention.target_low) if mention.target_low is not None else None,
        "target_high": plain(mention.target_high) if mention.target_high is not None else None,
        "unit": mention.unit,
        "direction": mention.direction.value,
        "target_period": mention.target_label,
        "text": describe_target(mention),
    }


def _revision_note(thread: ThreadView) -> str | None:
    """'target lowered from ₹450 crore to ₹400 crore' when the latest target differs."""
    original, latest = thread.original_target, thread.latest
    if original.claim_id == latest.claim_id:
        return None
    changes = []
    if original.target_label != latest.target_label and original.target_label:
        changes.append(f"target period moved from {original.target_label} to {latest.target_label}")
    if original.target_low is not None and latest.target_low is not None:
        kind = target_change(original, latest)
        if kind is not ChangeType.REITERATED:
            changes.append(
                f"target {kind.value.replace('_', ' ')} from {describe_target(original)} "
                f"to {describe_target(latest)}"
            )
    return "; ".join(changes) or None


# ---------------------------------------------------------------- baselines


def baseline_period(mention: Mention) -> tuple[int, int | None] | None:
    """Which period a directional claim is compared against."""
    if mention.target_fiscal_year is None:
        return None
    target_fy, target_q = mention.target_fiscal_year, mention.target_fiscal_quarter
    if mention.metric in GROWTH_METRICS:
        return target_fy - 1, target_q
    call_fy, call_q = from_ordinal(mention.call_ordinal)
    if target_q is None:
        return (call_fy, None) if call_fy < target_fy else (target_fy - 1, None)
    if end_ordinal(call_fy, call_q) < end_ordinal(target_fy, target_q):
        return call_fy, call_q
    return target_fy - 1, target_q


# ------------------------------------------------------------------ helpers


def _pct(gap: Decimal, reference: Decimal) -> Decimal | None:
    return None if reference == 0 else gap / abs(reference) * HUNDRED


def _q(value: Decimal | None, places: str = "0.01") -> Decimal | None:
    return None if value is None else value.quantize(Decimal(places))


def _actual_text(actual: ResolvedActual, value: Decimal, unit: str | None) -> str:
    text = f"{actual.period_label} {label(actual.metric)} was {format_value(value, unit)}"
    if unit is not None and actual.unit != unit:
        text += f" (entered as {format_value(actual.value, actual.unit)})"
    if actual.derived and actual.derived_from:
        text += f" [derived: {actual.derived_from}]"
    return text


def _with_revision(status_word: str, thread: ThreadView) -> str:
    note = _revision_note(thread)
    return f"{status_word} ({note})" if note else status_word


# -------------------------------------------------------------------- check


def deterministic_check(
    thread: ThreadView,
    actuals: ActualsIndex,
    as_of: int | None,
    *,
    tolerance_pct: float = 0.0,
    band_pct: float = 2.0,
) -> Outcome:
    latest = thread.latest
    period = latest.target_label
    details: dict[str, Any] = {
        "target_used": target_snapshot(latest),
        "original_target": target_snapshot(thread.original_target),
        "as_of": ordinal_label(as_of) if as_of is not None else None,
    }

    def outcome(status: VerificationStatus, reasoning: str, **kwargs: Any) -> Outcome:
        return Outcome(status, reasoning, details=details, **kwargs)

    if latest.target_end is None or latest.metric is None:
        return outcome(VerificationStatus.UNVERIFIABLE, "Unverifiable: no target period was given.")
    if as_of is None or latest.target_end > as_of:
        latest_data = ordinal_label(as_of) if as_of is not None else "none"
        return outcome(
            VerificationStatus.IN_PROGRESS,
            f"In progress: the target period {period} ends after the latest data ({latest_data}).",
        )
    assert latest.target_fiscal_year is not None
    actual = actuals.resolve(latest.metric, latest.target_fiscal_year, latest.target_fiscal_quarter)
    if actual is None:
        return outcome(
            VerificationStatus.UNVERIFIABLE,
            f"Unverifiable: the {period} {label(latest.metric)} actual has not been entered yet "
            "and cannot be derived from other actuals.",
        )
    details["actual"] = actual.snapshot()

    bounds = target_bounds(latest)
    if bounds is not None:
        return _numeric(thread, actual, bounds, tolerance_pct, outcome)
    return _directional(thread, actual, actuals, band_pct, outcome)


def _numeric(
    thread: ThreadView,
    actual: ResolvedActual,
    bounds: Bounds,
    tolerance_pct: float,
    outcome: OutcomeFn,
) -> Outcome:
    latest = thread.latest
    unit = latest.unit
    if unit is None:
        # A bare count ("150 stores") compares with a count/units actual as is.
        countable = parse_unit(actual.unit) in (ClaimUnit.COUNT, ClaimUnit.UNITS)
        value = actual.value if countable else None
    else:
        value = convert(actual.value, actual.unit, unit)
    if value is None:
        return outcome(
            VerificationStatus.UNVERIFIABLE,
            f"Unverifiable: the target is in {unit or 'no unit'} but the "
            f"{actual.period_label} actual is in {actual.unit}, which cannot be converted.",
            actual=actual,
        )
    target = describe_target(latest, bounds)
    actual_text = _actual_text(actual, value, unit)
    side = polarity(latest.metric)

    below = bounds.low is not None and value < bounds.low
    above = bounds.high is not None and value > bounds.high
    if not below and not above:
        return outcome(
            VerificationStatus.DELIVERED,
            f"{_with_revision('Delivered', thread)}: {actual_text} vs target {target}.",
            actual=actual,
        )
    bound = bounds.low if below else bounds.high
    assert bound is not None
    gap = value - bound
    gap_pct = _pct(gap, bound)
    exceeded = bounds.kind is TargetKind.RANGE and (
        (above and side is Polarity.HIGHER_IS_BETTER)
        or (below and side is Polarity.LOWER_IS_BETTER)
    )
    if exceeded:
        return outcome(
            VerificationStatus.DELIVERED,
            f"{_with_revision('Delivered', thread)}: {actual_text}, beating the target "
            f"{target} by {format_value(abs(gap), unit)}.",
            actual=actual,
            computed_gap=gap,
            gap_pct=_q(gap_pct),
        )
    near_miss = gap_pct is not None and abs(gap_pct) <= Decimal(str(tolerance_pct))
    gap_text = f"{'+' if gap > 0 else '-'}{format_value(abs(gap), unit)}"
    if gap_pct is not None:
        gap_text += f", {plain(_q(gap_pct) or Decimal(0))}%"
    reasoning = (
        f"{_with_revision('Missed', thread)}: {actual_text} vs target {target} ({gap_text})."
    )
    if near_miss:
        reasoning += f" Within the {tolerance_pct:g}% tolerance, so flagged as a near miss."
    return outcome(
        VerificationStatus.MISSED,
        reasoning,
        actual=actual,
        computed_gap=gap,
        gap_pct=_q(gap_pct),
        near_miss=near_miss,
    )


def _directional(
    thread: ThreadView,
    actual: ResolvedActual,
    actuals: ActualsIndex,
    band_pct: float,
    outcome: OutcomeFn,
) -> Outcome:
    latest = thread.latest
    direction = latest.direction
    if direction is Direction.NONE:
        return outcome(
            VerificationStatus.UNVERIFIABLE,
            "Unverifiable: the claim states neither a target nor a direction.",
            actual=actual,
        )
    period = baseline_period(latest)
    assert period is not None and latest.metric is not None
    baseline = actuals.resolve(latest.metric, *period)
    baseline_label = label_or_none(*period)
    if baseline is None:
        return outcome(
            VerificationStatus.UNVERIFIABLE,
            f"Unverifiable: the baseline ({baseline_label} {label(latest.metric)}) has not been "
            "entered yet and cannot be derived.",
            actual=actual,
        )
    outcome_details = {"baseline": baseline.snapshot()}
    base_value = convert(baseline.value, baseline.unit, actual.unit)
    if base_value is None:
        return outcome(
            VerificationStatus.UNVERIFIABLE,
            f"Unverifiable: the baseline is in {baseline.unit} and the actual in {actual.unit}, "
            "which cannot be converted.",
            actual=actual,
            baseline=baseline,
        )
    change = actual.value - base_value
    change_pct = _pct(change, base_value)
    if change_pct is None:
        return outcome(
            VerificationStatus.UNVERIFIABLE,
            "Unverifiable: the baseline is zero, so the change cannot be measured.",
            actual=actual,
            baseline=baseline,
        )
    band = Decimal(str(band_pct))
    delivered = {
        Direction.INCREASE: change_pct > band,
        Direction.DECREASE: change_pct < -band,
        Direction.MAINTAIN: abs(change_pct) <= band,
    }[direction]
    word = {
        Direction.INCREASE: "an increase",
        Direction.DECREASE: "a decrease",
        Direction.MAINTAIN: "no change",
    }[direction]
    moved = (
        "roughly flat"
        if abs(change_pct) <= band
        else ("up" if change_pct > 0 else "down") + f" {plain(abs(_q(change_pct) or change_pct))}%"
    )
    reasoning = (
        f"{'Delivered' if delivered else 'Missed'}: promised {word} in {latest.target_label}; "
        f"{_actual_text(actual, actual.value, actual.unit)} vs "
        f"{format_value(baseline.value, baseline.unit)} in {baseline.period_label} ({moved}; "
        f"changes within ±{band_pct:g}% count as no change)."
    )
    result = outcome(
        VerificationStatus.DELIVERED if delivered else VerificationStatus.MISSED,
        reasoning,
        actual=actual,
        baseline=baseline,
        computed_gap=None if delivered else change,
        gap_pct=None if delivered else _q(change_pct),
    )
    result.details.update(outcome_details)
    result.details["change_pct"] = plain(_q(change_pct) or Decimal(0))
    return result
