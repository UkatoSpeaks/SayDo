"""Link the same promise across calls into claim threads.

A thread is one promise tracked over time ("FY27 EBITDA margin 19-20%" said on the
Q1, Q2 and Q3 calls). Linking is deterministic first; the LLM only sees what code
cannot decide:

1. Numeric/directional claims about a reported metric: same metric + same resolved
   target period -> same thread. One extra deterministic rule for "by <period>"
   metrics (debt, capacity, store count, ...): an identical target restated for a
   later period while the earlier period was still open, after which the earlier
   one is no longer mentioned, is the same promise, delayed.
2. Ambiguous claims (qualitative, metric "other", plant commissioning, product
   launch): linked when they share a name ("Line 4", "Aura") or, for qualitative
   claims on a standard metric, the same target period. What is left with an
   earlier same-metric candidate goes to the LLM in batched calls, within the run's
   budget. Without budget they stay separate threads.
3. Every mention gets a ``change_type`` against the previous one: new, reiterated,
   raised, lowered, narrowed, widened, delayed, pulled_forward.
4. A thread is dropped when it has a target period that has not ended as of the
   company's latest data, and it is absent from the company's last
   ``VERIFY_DROPPED_AFTER_CALLS`` calls. Threads without a target period are never
   dropped (they are unverifiable instead).

Thread ids are kept across re-runs: a rebuilt group reuses the thread most of its
claims already pointed at.
"""

import itertools
import logging
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Actual,
    ChangeType,
    Claim,
    ClaimThread,
    ClaimType,
    Direction,
    DocType,
    Document,
    DocumentStatus,
    MetricNormalized,
)
from app.services.llm import LLMError, LLMProvider
from app.services.llm_budget import BudgetExhausted, LLMBudget
from app.services.metrics import EVENT_METRICS, POINT_IN_TIME_METRICS, label
from app.services.periods import end_ordinal, label_or_none
from app.services.units import convert, format_range
from app.services.verification_prompts import LINK_SYSTEM_PROMPT, LinkResult

logger = logging.getLogger(__name__)

MAX_CANDIDATES_PER_ITEM = 5
MAX_QUOTE_CHARS_IN_PROMPT = 300
MAX_TITLE_CHARS = 255

# Metrics whose targets read "by <period>": restating one for a later period is a delay.
BY_PERIOD_METRICS = POINT_IN_TIME_METRICS | {MetricNormalized.COST_REDUCTION}
_DIRECTION_RANK = {Direction.DECREASE: 0, Direction.MAINTAIN: 1, Direction.INCREASE: 2}


# --------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Mention:
    """One claim, as linking sees it."""

    claim_id: uuid.UUID
    document_id: uuid.UUID
    call_ordinal: int
    call_label: str
    page_number: int
    metric: str | None
    metric_raw: str
    claim_type: ClaimType
    target_low: Decimal | None
    target_high: Decimal | None
    unit: str | None
    direction: Direction
    target_fiscal_year: int | None
    target_fiscal_quarter: int | None
    quote: str
    current_thread_id: uuid.UUID | None = None
    timeframe_raw: str | None = None

    @property
    def target_end(self) -> int | None:
        if self.target_fiscal_year is None:
            return None
        return end_ordinal(self.target_fiscal_year, self.target_fiscal_quarter)

    @property
    def target_label(self) -> str | None:
        return label_or_none(self.target_fiscal_year, self.target_fiscal_quarter)

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return self.call_ordinal, self.page_number, str(self.claim_id)


def is_ambiguous(mention: Mention) -> bool:
    return (
        mention.metric is None
        or mention.metric == MetricNormalized.OTHER
        or mention.metric in EVENT_METRICS
        or mention.claim_type is ClaimType.QUALITATIVE
    )


# ----------------------------------------------------------------- name keys

_WORD_NUMBER_RE = re.compile(
    r"\b(line|unit|phase|plant|block|tower|stage|project|site|furnace|kiln|mill)"
    r"[\s\-#]*(\d+|[ivx]+)\b",
    re.IGNORECASE,
)
_CAPITALISED_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z&]{2,})\b")
_NOT_NAMES = frozenset(
    {
        "we",
        "our",
        "the",
        "this",
        "that",
        "these",
        "with",
        "and",
        "fy",
        "inr",
        "rs",
        "crore",
        "crores",
        "lakh",
        "ebitda",
        "pat",
        "capex",
        "company",
        "board",
        "management",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "india",
        "indian",
        "quarter",
        "year",
        "phase",
        "line",
        "unit",
        "plant",
    }
)


def name_keys(text: str) -> frozenset[str]:
    """Distinctive names in a quote: "Line 4", "Unit II", and capitalised words not at the
    start of a sentence ("Pune", "Aura"), lower-cased."""
    keys = {f"{m.group(1).lower()} {m.group(2).lower()}" for m in _WORD_NUMBER_RE.finditer(text)}
    for match in _CAPITALISED_RE.finditer(text):
        word = match.group(1).lower()
        if word not in _NOT_NAMES and not re.fullmatch(r"(?:q|h)\d|fy\d+", word):
            keys.add(word)
    return frozenset(keys)


def _mention_names(mention: Mention) -> frozenset[str]:
    return name_keys(mention.quote) | name_keys(mention.metric_raw)


# ------------------------------------------------------------- union-find


class _Groups:
    """Union-find over mention indices that also keeps each group's members."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self._members: dict[int, list[int]] = {i: [i] for i in range(size)}

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        keep, gone = min(ra, rb), max(ra, rb)
        self.parent[gone] = keep
        self._members[keep] = sorted(self._members[keep] + self._members.pop(gone))

    def of(self, i: int) -> list[int]:
        return self._members[self.find(i)]

    def members(self) -> dict[int, list[int]]:
        return dict(self._members)


# ----------------------------------------------------------- grouping (pure)


@dataclass(frozen=True)
class LinkQuestion:
    """An ambiguous mention group and the earlier groups it might continue."""

    item: int  # root index of the group being asked about
    candidates: tuple[int, ...]  # root indices of earlier groups


@dataclass
class Grouping:
    mentions: list[Mention]
    groups: _Groups
    questions: list[LinkQuestion] = field(default_factory=list)

    def group_members(self, root: int) -> list[Mention]:
        return [self.mentions[i] for i in self.groups.of(root)]


def _calls(mentions: Iterable[Mention]) -> set[int]:
    return {m.call_ordinal for m in mentions}


def _same_targets(a: Mention, b: Mention) -> bool:
    if a.target_low is None or b.target_low is None:
        return False
    low = convert(b.target_low, b.unit, a.unit)
    high = convert(b.target_high, b.unit, a.unit) if b.target_high is not None else None
    return low == a.target_low and high == a.target_high


def group_mentions(mentions: Sequence[Mention]) -> Grouping:
    """Deterministic grouping plus the questions left for the LLM."""
    ordered = sorted(mentions, key=lambda m: m.sort_key)
    groups = _Groups(len(ordered))
    grouping = Grouping(list(ordered), groups)

    # 1. Same metric + same target period (reported metrics; qualitative claims on a
    #    standard metric with a period, too).
    by_key: dict[tuple[object, ...], int] = {}
    for i, m in enumerate(ordered):
        if not is_ambiguous(m):
            key: tuple[object, ...] = (
                "num",
                m.metric,
                m.target_fiscal_year,
                m.target_fiscal_quarter,
            )
        elif (
            m.claim_type is ClaimType.QUALITATIVE
            and m.metric not in (None, MetricNormalized.OTHER)
            and m.metric not in EVENT_METRICS
            and m.target_fiscal_year is not None
        ):
            key = ("qual", m.metric, m.target_fiscal_year, m.target_fiscal_quarter)
        else:
            continue
        if key in by_key:
            groups.union(by_key[key], i)
        else:
            by_key[key] = i

    def members(root: int) -> list[Mention]:
        return [ordered[j] for j in groups.of(root)]

    # 2. "By <period>" metrics: identical target restated later while still open.
    numeric_roots = sorted(
        {groups.find(i) for i, m in enumerate(ordered) if not is_ambiguous(m)},
        key=lambda r: ordered[groups.of(r)[0]].sort_key,
    )
    for later in numeric_roots:
        later_members = members(later)
        first_later = later_members[0]
        if first_later.metric not in BY_PERIOD_METRICS or first_later.target_end is None:
            continue
        for earlier in numeric_roots:
            if groups.find(earlier) == groups.find(later):
                continue
            earlier_members = members(earlier)
            last_earlier = max(earlier_members, key=lambda m: m.sort_key)
            if (
                last_earlier.metric == first_later.metric
                and last_earlier.target_end is not None
                and last_earlier.target_end != first_later.target_end
                and max(_calls(earlier_members)) < min(_calls(later_members))
                and first_later.call_ordinal < last_earlier.target_end
                and _same_targets(last_earlier, first_later)
            ):
                groups.union(earlier, later)
                break

    # 3. Ambiguous claims sharing a name, then questions for the rest.
    ambiguous = [i for i, m in enumerate(ordered) if is_ambiguous(m)]
    for position, i in enumerate(ambiguous):
        mention = ordered[i]
        names = _mention_names(mention)
        if not names:
            continue
        for j in reversed(ambiguous[:position]):
            other = ordered[j]
            if groups.find(i) == groups.find(j) or other.metric != mention.metric:
                continue
            if not names & _mention_names(other):
                continue
            if _calls(members(i)) & _calls(members(j)):
                continue
            groups.union(i, j)
            break

    asked: set[int] = set()
    for i in ambiguous:
        root = groups.find(i)
        if root in asked:
            continue
        asked.add(root)
        item_members = members(root)
        item_calls = _calls(item_members)
        first_call = min(item_calls)
        candidates: list[tuple[int, int]] = []
        for j in ambiguous:
            other_root = groups.find(j)
            if other_root == root or other_root in {c for c, _ in candidates}:
                continue
            other_members = members(other_root)
            if other_members[0].metric != item_members[0].metric:
                continue
            other_calls = _calls(other_members)
            if min(other_calls) >= first_call or other_calls & item_calls:
                continue
            candidates.append((other_root, max(other_calls)))
        if candidates:
            candidates.sort(key=lambda c: -c[1])
            grouping.questions.append(
                LinkQuestion(root, tuple(c for c, _ in candidates[:MAX_CANDIDATES_PER_ITEM]))
            )
    return grouping


# ------------------------------------------------------------- LLM linking


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return (
        text if len(text) <= MAX_QUOTE_CHARS_IN_PROMPT else text[:MAX_QUOTE_CHARS_IN_PROMPT] + "…"
    )


def _describe(members: list[Mention]) -> str:
    first, last = members[0], members[-1]
    target = last.target_label or "no date"
    said = ", ".join(sorted({m.call_label for m in members}, key=lambda s: s))
    return f'[{label(first.metric)}; said on {said}; target {target}] "{_clip(last.quote)}"'


def build_link_prompt(
    grouping: Grouping, questions: Sequence[LinkQuestion]
) -> tuple[str, dict[str, int], dict[str, int]]:
    """User prompt plus the id maps (N1 -> root, T1 -> root)."""
    item_ids: dict[str, int] = {}
    thread_ids: dict[str, int] = {}
    root_to_thread: dict[int, str] = {}
    lines = ["THREADS (earlier promises):"]
    for question in questions:
        for root in question.candidates:
            if root not in root_to_thread:
                tid = f"T{len(root_to_thread) + 1}"
                root_to_thread[root] = tid
                thread_ids[tid] = root
                lines.append(f"{tid} {_describe(grouping.group_members(root))}")
    lines.append("")
    lines.append("ITEMS (later promises):")
    for question in questions:
        nid = f"N{len(item_ids) + 1}"
        item_ids[nid] = question.item
        options = ", ".join(root_to_thread[c] for c in question.candidates)
        lines.append(f"{nid} {_describe(grouping.group_members(question.item))}")
        lines.append(f"   may be the same as: {options}")
    return "\n".join(lines), item_ids, thread_ids


async def resolve_questions(
    grouping: Grouping,
    llm: LLMProvider,
    budget: LLMBudget,
    *,
    model: str,
    batch_size: int,
) -> int:
    """Ask the LLM about ambiguous groups in batches (within budget). Returns links made."""
    links = 0
    questions = list(grouping.questions)
    for start in range(0, len(questions), batch_size):
        batch = questions[start : start + batch_size]
        user, item_ids, thread_ids = build_link_prompt(grouping, batch)
        try:
            result = await budget.complete_json(llm, LINK_SYSTEM_PROMPT, user, LinkResult, model)
        except BudgetExhausted:
            logger.info(
                "Linking: LLM budget exhausted; %d question(s) left unlinked",
                len(questions) - start,
            )
            break
        except LLMError as exc:
            logger.warning("Linking: LLM call failed (%s); batch left unlinked", exc)
            continue
        allowed = {item_ids[nid]: q.candidates for nid, q in zip(item_ids, batch, strict=True)}
        for decision in result.decisions:
            item_root = item_ids.get(decision.item)
            target_root = thread_ids.get(decision.same_as or "")
            if item_root is None or target_root is None:
                continue
            if target_root not in allowed[item_root]:
                continue  # not one of the item's candidates
            item_members = grouping.group_members(item_root)
            target_members = grouping.group_members(target_root)
            if _calls(item_members) & _calls(target_members):
                continue
            grouping.groups.union(item_root, target_root)
            links += 1
    return links


# ----------------------------------------------------------- change types


def target_change(previous: Mention, current: Mention) -> ChangeType:
    """Compare the targets only (values, else direction), ignoring the period."""
    if previous.target_low is not None and current.target_low is not None:
        low = convert(current.target_low, current.unit, previous.unit)
        high_raw = current.target_high if current.target_high is not None else current.target_low
        high = convert(high_raw, current.unit, previous.unit)
        prev_low = previous.target_low
        prev_high = previous.target_high if previous.target_high is not None else prev_low
        if low is None or high is None:
            return ChangeType.REITERATED  # units cannot be compared
        if low == prev_low and high == prev_high:
            return ChangeType.REITERATED
        if low >= prev_low and high <= prev_high:
            return ChangeType.NARROWED
        if low <= prev_low and high >= prev_high:
            return ChangeType.WIDENED
        return ChangeType.RAISED if low + high > prev_low + prev_high else ChangeType.LOWERED

    prev_rank = _DIRECTION_RANK.get(previous.direction)
    cur_rank = _DIRECTION_RANK.get(current.direction)
    if prev_rank is not None and cur_rank is not None and prev_rank != cur_rank:
        return ChangeType.RAISED if cur_rank > prev_rank else ChangeType.LOWERED
    return ChangeType.REITERATED


def change_type(previous: Mention, current: Mention) -> ChangeType:
    """How ``current`` differs from the previous mention of the same promise. A moved
    target period (delayed / pulled forward) wins over a changed value."""
    prev_end, cur_end = previous.target_end, current.target_end
    if prev_end is not None and cur_end is not None and prev_end != cur_end:
        return ChangeType.DELAYED if cur_end > prev_end else ChangeType.PULLED_FORWARD
    return target_change(previous, current)


def change_types(members: Sequence[Mention]) -> list[ChangeType]:
    ordered = sorted(members, key=lambda m: m.sort_key)
    return [ChangeType.NEW] + [change_type(a, b) for a, b in itertools.pairwise(ordered)]


# ---------------------------------------------------------------- dropped


def is_dropped(members: Sequence[Mention], recent_calls: Sequence[int], as_of: int | None) -> bool:
    """Absent from every one of ``recent_calls`` while its target period is still open."""
    latest = max(members, key=lambda m: m.sort_key)
    if latest.target_end is None or as_of is None or latest.target_end <= as_of:
        return False
    if not recent_calls:
        return False
    return not (_calls(members) & set(recent_calls))


# ------------------------------------------------------------------- titles


def _target_text(mention: Mention) -> str | None:
    if mention.target_low is not None:
        high = mention.target_high if mention.target_high is not None else mention.target_low
        return format_range(mention.target_low, high, mention.unit)
    return {
        Direction.INCREASE: "to increase",
        Direction.DECREASE: "to decrease",
        Direction.MAINTAIN: "to be maintained",
    }.get(mention.direction)


def thread_title(members: Sequence[Mention]) -> str:
    """E.g. "EBITDA margin 19-20% in FY27" (en dash), "Line 4 commissioning by Q1FY27"."""
    ordered = sorted(members, key=lambda m: m.sort_key)
    first, latest = ordered[0], ordered[-1]
    if is_ambiguous(first):
        name = " ".join(first.metric_raw.split())
        subject = name[:1].upper() + name[1:]
        target = None if first.claim_type is ClaimType.QUALITATIVE else _target_text(latest)
        preposition = "by"
    else:
        subject = label(latest.metric)
        target = _target_text(latest)
        preposition = "by" if latest.metric in BY_PERIOD_METRICS else "in"
    parts = [subject]
    if target:
        parts.append(target)
    if latest.target_label:
        parts.append(f"{preposition} {latest.target_label}")
    title = " ".join(parts)
    return title if len(title) <= MAX_TITLE_CHARS else title[: MAX_TITLE_CHARS - 3] + "..."


# ----------------------------------------------------------------- database


@dataclass
class LinkStats:
    threads_total: int = 0
    threads_created: int = 0
    threads_removed: int = 0
    llm_links: int = 0
    questions: int = 0
    as_of: int | None = None


async def company_calls(session: AsyncSession, company_id: uuid.UUID) -> list[int]:
    """End ordinals of the company's extracted call transcripts, latest first."""
    rows = await session.execute(
        select(Document.fiscal_year, Document.fiscal_quarter).where(
            Document.company_id == company_id,
            Document.doc_type == DocType.TRANSCRIPT,
            Document.status == DocumentStatus.EXTRACTED,
        )
    )
    return sorted({end_ordinal(fy, fq) for fy, fq in rows}, reverse=True)


async def company_as_of(session: AsyncSession, company_id: uuid.UUID) -> int | None:
    """Latest period with an extracted call or an entered actual (not today's date, so
    results are reproducible)."""
    calls = await company_calls(session, company_id)
    actual = await session.execute(
        select(
            func.max(Actual.fiscal_year * 4 + func.coalesce(Actual.fiscal_quarter, 4) - 1)
        ).where(Actual.company_id == company_id)
    )
    latest_actual = actual.scalar_one_or_none()
    candidates = [*calls[:1], *([int(latest_actual)] if latest_actual is not None else [])]
    return max(candidates) if candidates else None


def mention_from(claim: Claim, document: Document) -> Mention:
    return Mention(
        claim_id=claim.id,
        document_id=claim.document_id,
        call_ordinal=end_ordinal(document.fiscal_year, document.fiscal_quarter),
        call_label=document.period_label,
        page_number=claim.page_number,
        metric=claim.metric_normalized,
        metric_raw=claim.metric_raw,
        claim_type=claim.claim_type,
        target_low=claim.target_low,
        target_high=claim.target_high,
        unit=claim.unit,
        direction=claim.direction,
        target_fiscal_year=claim.target_fiscal_year,
        target_fiscal_quarter=claim.target_fiscal_quarter,
        quote=claim.quote,
        current_thread_id=claim.thread_id,
        timeframe_raw=claim.timeframe_raw,
    )


async def load_mentions(session: AsyncSession, company_id: uuid.UUID) -> list[Mention]:
    rows = await session.execute(
        select(Claim, Document)
        .join(Document, Document.id == Claim.document_id)
        .where(Claim.company_id == company_id)
    )
    return [mention_from(claim, document) for claim, document in rows]


async def link_company(
    session: AsyncSession,
    company_id: uuid.UUID,
    llm: LLMProvider,
    budget: LLMBudget,
    *,
    model: str,
    batch_size: int,
    dropped_after_calls: int,
) -> LinkStats:
    """Build or refresh the company's threads. Commits."""
    mentions = await load_mentions(session, company_id)
    stats = LinkStats(as_of=await company_as_of(session, company_id))
    recent_calls = (await company_calls(session, company_id))[:dropped_after_calls]

    grouping = group_mentions(mentions)
    stats.questions = len(grouping.questions)
    if grouping.questions:
        stats.llm_links = await resolve_questions(
            grouping, llm, budget, model=model, batch_size=batch_size
        )

    existing = {
        t.id: t
        for t in await session.scalars(
            select(ClaimThread).where(ClaimThread.company_id == company_id)
        )
    }
    used: set[uuid.UUID] = set()
    groups = sorted(
        (
            sorted((grouping.mentions[i] for i in idx), key=lambda m: m.sort_key)
            for idx in grouping.groups.members().values()
        ),
        key=lambda ms: ms[0].sort_key,
    )
    for members in groups:
        votes = Counter(m.current_thread_id for m in members if m.current_thread_id in existing)
        thread = None
        for thread_id, _ in votes.most_common():
            if thread_id is not None and thread_id not in used:
                thread = existing[thread_id]
                break
        if thread is None:
            thread = ClaimThread(id=uuid.uuid4(), company_id=company_id)
            session.add(thread)
            stats.threads_created += 1
        used.add(thread.id)
        latest = members[-1]
        thread.metric_normalized = latest.metric
        thread.title = thread_title(members)
        thread.target_fiscal_year = latest.target_fiscal_year
        thread.target_fiscal_quarter = latest.target_fiscal_quarter
        thread.first_seen_period = members[0].call_label
        thread.last_seen_period = latest.call_label
        thread.mention_count = len(members)
        thread.is_dropped = is_dropped(members, recent_calls, stats.as_of)
        await session.flush()
        for mention, kind in zip(members, change_types(members), strict=True):
            await session.execute(
                update(Claim)
                .where(Claim.id == mention.claim_id)
                .values(thread_id=thread.id, change_type=kind)
            )

    stale = set(existing) - used
    if stale:
        await session.execute(delete(ClaimThread).where(ClaimThread.id.in_(stale)))
    stats.threads_removed = len(stale)
    stats.threads_total = len(groups)
    await session.commit()
    logger.info(
        "Linked company %s: %d threads (%d new, %d removed), %d LLM questions, %d LLM links",
        company_id,
        stats.threads_total,
        stats.threads_created,
        stats.threads_removed,
        stats.questions,
        stats.llm_links,
    )
    return stats
