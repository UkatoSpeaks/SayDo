"""The promise x call ledger: one row per thread, one cell per quarter. Pure code, no LLM.

Columns run from the company's first call to the latest target period any mention
refers to (or the latest call, if later). Each cell, in priority order:

1. ``kept`` / ``missed``: the quarter whose actual decided the verdict (for promises
   judged from commentary, the call whose excerpt was the evidence), with the actual
   as text.
2. ``said`` / ``raised`` / ``lowered`` / ``moved``: a call that mentioned the promise,
   from the mention's change type, with the target as text ("450", "18-19%",
   "-> Q3FY26").
3. ``open``: the current target quarter of a promise still in progress.
4. ``silent``: a call after the first mention that did not mention it while it was
   still undecided (up to its current deadline).
5. ``none``.

Rows are ordered open, missed, dropped, unverifiable, not verified, then kept.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.models import ChangeType, ClaimUnit, Direction, VerificationStatus
from app.services.deterministic_check import Bounds, target_bounds
from app.services.linking import Mention
from app.services.periods import ordinal_label
from app.services.units import EN_DASH, convert, parse_unit

ARROW = "\u2192"

_KIND_BY_CHANGE = {
    ChangeType.RAISED: "raised",
    ChangeType.LOWERED: "lowered",
    ChangeType.DELAYED: "moved",
    ChangeType.PULLED_FORWARD: "moved",
}
# A call with several mentions shows the most telling one.
_CHANGE_RANK = {"moved": 3, "lowered": 2, "raised": 2, "said": 0}
_ROW_ORDER = {
    VerificationStatus.IN_PROGRESS: 0,
    VerificationStatus.MISSED: 1,
    VerificationStatus.DROPPED: 2,
    VerificationStatus.UNVERIFIABLE: 3,
    None: 4,
    VerificationStatus.DELIVERED: 5,
}


@dataclass(frozen=True)
class LedgerThread:
    thread_id: uuid.UUID
    title: str
    metric: str | None
    mentions: list[Mention]  # with change_type, in any order
    change_types: dict[uuid.UUID, ChangeType | None]  # by claim id
    status: VerificationStatus | None
    kept_after_lowering: bool = False
    actual_value: Decimal | None = None
    actual_unit: str | None = None
    actual_ordinal: int | None = None  # end ordinal of the actual's period
    actual_label: str | None = None  # "FY26", "Q4FY26"
    evidence_ordinal: int | None = None  # earliest call with judging evidence
    gap_pct: Decimal | None = None
    as_of: int | None = None


@dataclass(frozen=True)
class Cell:
    period: str
    kind: str
    text: str


@dataclass(frozen=True)
class Comparison:
    unit: str | None
    target: Bounds
    original: Bounds | None
    actual: Decimal
    actual_period: str
    gap_pct: Decimal | None


@dataclass(frozen=True)
class Row:
    thread_id: uuid.UUID
    title: str
    metric: str | None
    unit: str | None
    verdict: VerificationStatus | None
    kept_after_lowering: bool
    first_seen_period: str
    first_target_period: str | None
    target_period: str | None
    slipped_quarters: int
    mention_count: int
    comparison: Comparison | None
    cells: list[Cell]


@dataclass(frozen=True)
class Ledger:
    periods: list[str]
    rows: list[Row]


# -------------------------------------------------------------- formatting


def group_indian(integer: int) -> str:
    """Indian digit grouping: 4620 -> "4,620", 1234567 -> "12,34,567"."""
    sign, digits = ("-" if integer < 0 else ""), str(abs(integer))
    if len(digits) <= 3:
        return sign + digits
    head, tail = digits[:-3], digits[-3:]
    pairs: list[str] = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    return sign + ",".join([head, *pairs, tail])


def compact_number(value: Decimal) -> str:
    """At most two decimals, Indian grouping, no unit: Decimal("18.50") -> "18.5"."""
    rounded = value.quantize(Decimal("0.01"))
    integer = int(rounded)
    fraction = format(abs(rounded - integer), "f")[1:].rstrip("0").rstrip(".")
    if integer == 0 and rounded < 0:
        return "-0" + fraction
    return group_indian(integer) + fraction


def _percent_text(value: Decimal) -> str:
    """Percentages show at most one decimal: 18.528 -> "18.5"."""
    return compact_number(value.quantize(Decimal("0.1")))


def _with_unit(text: str, unit: str | None) -> str:
    parsed = parse_unit(unit)
    if parsed is ClaimUnit.PERCENT:
        return f"{text}%"
    if parsed is ClaimUnit.BPS:
        return f"{text} bps"
    if parsed is ClaimUnit.X:
        return f"{text}x"
    return text  # money and counts: the unit is on the row


def target_text(mention: Mention) -> str:
    """Short cell text for a mention's target: "450", "18-19%", "up", "Q2FY26"."""
    if mention.target_low is not None:
        high = mention.target_high if mention.target_high is not None else mention.target_low
        number = _percent_text if parse_unit(mention.unit) is ClaimUnit.PERCENT else compact_number
        text = number(mention.target_low)
        if high != mention.target_low:
            text += EN_DASH + number(high)
        return _with_unit(text, mention.unit)
    word = {Direction.INCREASE: "up", Direction.DECREASE: "down", Direction.MAINTAIN: "flat"}
    if mention.direction in word:
        return word[mention.direction]
    return mention.target_label or ""


def _actual_text(thread: LedgerThread, unit: str | None) -> str:
    if thread.actual_value is None:
        return ""
    value = thread.actual_value
    shown_unit = thread.actual_unit
    if unit is not None:
        converted = convert(value, thread.actual_unit, unit)
        if converted is not None:
            value, shown_unit = converted, unit
    if parse_unit(shown_unit) is ClaimUnit.PERCENT:
        return _with_unit(_percent_text(value), shown_unit)
    return _with_unit(compact_number(value), shown_unit)


# ------------------------------------------------------------------- ledger


def _mention_kind(change: ChangeType | None) -> str:
    return _KIND_BY_CHANGE.get(change, "said") if change is not None else "said"


def _first_deadline(mentions: list[Mention]) -> int | None:
    return next((m.target_end for m in mentions if m.target_end is not None), None)


def _decided_ordinal(thread: LedgerThread, latest: Mention) -> int | None:
    if thread.status not in (VerificationStatus.DELIVERED, VerificationStatus.MISSED):
        return None
    if thread.actual_ordinal is not None:
        return thread.actual_ordinal
    if thread.evidence_ordinal is not None:
        return thread.evidence_ordinal
    if latest.target_end is not None:
        if thread.as_of is not None:
            return min(latest.target_end, thread.as_of)
        return latest.target_end
    return thread.as_of


def _comparison(thread: LedgerThread, ordered: list[Mention]) -> Comparison | None:
    latest = ordered[-1]
    bounds = target_bounds(latest)
    if bounds is None or thread.actual_value is None or thread.actual_ordinal is None:
        return None
    if latest.unit is None:
        actual: Decimal | None = thread.actual_value
    else:
        actual = convert(thread.actual_value, thread.actual_unit, latest.unit)
    if actual is None:
        return None
    original_mention = next((m for m in ordered if m.target_low is not None), latest)
    original = None
    if original_mention.claim_id != latest.claim_id:
        candidate = target_bounds(original_mention)
        same_unit = parse_unit(original_mention.unit) == parse_unit(latest.unit)
        if candidate is not None and same_unit and candidate != bounds:
            original = candidate
    return Comparison(
        unit=latest.unit,
        target=bounds,
        original=original,
        actual=actual,
        actual_period=thread.actual_label or ordinal_label(thread.actual_ordinal),
        gap_pct=thread.gap_pct,
    )


def _row(thread: LedgerThread, ordinals: list[int], calls: set[int]) -> Row:
    ordered = sorted(thread.mentions, key=lambda m: m.sort_key)
    latest = ordered[-1]
    first_call = ordered[0].call_ordinal
    decided = _decided_ordinal(thread, latest)

    by_call: dict[int, tuple[str, Mention]] = {}
    for mention in ordered:
        kind = _mention_kind(thread.change_types.get(mention.claim_id))
        current = by_call.get(mention.call_ordinal)
        if current is None or _CHANGE_RANK[kind] >= _CHANGE_RANK[current[0]]:
            by_call[mention.call_ordinal] = (kind, mention)

    cells = []
    for ordinal in ordinals:
        period = ordinal_label(ordinal)
        if decided is not None and ordinal == decided:
            kind = "kept" if thread.status is VerificationStatus.DELIVERED else "missed"
            cells.append(Cell(period, kind, _actual_text(thread, latest.unit)))
        elif ordinal in by_call:
            kind, mention = by_call[ordinal]
            text = target_text(mention)
            if kind == "moved" and mention.target_label:
                text = f"{ARROW} {mention.target_label}"
            cells.append(Cell(period, kind, text))
        elif thread.status is VerificationStatus.IN_PROGRESS and ordinal == latest.target_end:
            cells.append(Cell(period, "open", target_text(latest)))
        elif (
            ordinal in calls
            and ordinal > first_call
            and (decided is None or ordinal < decided)
            and (latest.target_end is None or ordinal <= latest.target_end)
        ):
            cells.append(Cell(period, "silent", ""))
        else:
            cells.append(Cell(period, "none", ""))

    first_deadline = _first_deadline(ordered)
    slipped = 0
    if first_deadline is not None and latest.target_end is not None:
        slipped = max(0, latest.target_end - first_deadline)
    first_target = next((m.target_label for m in ordered if m.target_label), None)
    return Row(
        thread_id=thread.thread_id,
        title=thread.title,
        metric=thread.metric,
        unit=latest.unit,
        verdict=thread.status,
        kept_after_lowering=thread.kept_after_lowering,
        first_seen_period=ordered[0].call_label,
        first_target_period=first_target,
        target_period=latest.target_label,
        slipped_quarters=slipped,
        mention_count=len(ordered),
        comparison=_comparison(thread, ordered),
        cells=cells,
    )


def build_ledger(threads: list[LedgerThread], call_ordinals: list[int]) -> Ledger:
    threads = [t for t in threads if t.mentions]
    if not threads:
        return Ledger(periods=[], rows=[])
    mentions = [m for t in threads for m in t.mentions]
    starts = [*call_ordinals, *(m.call_ordinal for m in mentions)]
    ends = [
        *call_ordinals,
        *(m.call_ordinal for m in mentions),
        *(m.target_end for m in mentions if m.target_end is not None),
        *(t.actual_ordinal for t in threads if t.actual_ordinal is not None),
    ]
    ordinals = list(range(min(starts), max(ends) + 1))
    calls = set(call_ordinals) | {m.call_ordinal for m in mentions}
    first_call = {t.thread_id: min(m.call_ordinal for m in t.mentions) for t in threads}
    rows = [_row(t, ordinals, calls) for t in threads]
    rows.sort(
        key=lambda r: (_ROW_ORDER[r.verdict], first_call[r.thread_id], r.title, str(r.thread_id))
    )
    return Ledger(periods=[ordinal_label(o) for o in ordinals], rows=rows)
