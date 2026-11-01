"""The verification agent: one LangGraph run per claim thread.

    load_thread -> route
        dropped (absent from recent calls)          -> mark_dropped
        numeric/directional with a target period    -> deterministic_check
        qualitative / event claims                  -> gather_evidence -> llm_judge
        no resolvable target period                 -> unverifiable
    -> find_explanation (missed, delayed or dropped threads only) -> persist

- "As of" is the company's latest extracted call or entered actual, never today's
  date, so the same data always gives the same verdicts.
- The deterministic path never calls an LLM; its explanations are templates.
- ``gather_evidence`` vector-searches management chunks from calls after the
  promise was first made (Gemini query embedding, top 5). With no later commentary
  the verdict is decided in code (in progress / unverifiable) and no LLM call is made.
- ``llm_judge`` makes one Groq call; the quote it returns must be found in the chunk
  it cites (same strict matching as extraction, numbers unchanged). A failed check
  gets one retry; a second failure gives "unverifiable". Calls are capped by
  ``VERIFY_MAX_LLM_CALLS`` per run; once spent, the remaining LLM-route threads are
  unverifiable ("llm budget exhausted").
- ``find_explanation`` stores the top 2 later management chunks matching
  "<metric> <period> reason impact delay"; an LLM summary only with
  ``VERIFY_SUMMARIZE_EXPLANATIONS=true``.
- ``persist`` writes a new verification and clears ``is_latest`` on the thread's
  older ones (a partial unique index allows one latest per thread).
"""

import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.models import (
    Actual,
    Claim,
    ClaimThread,
    ClaimType,
    MetricNormalized,
    SpeakerRole,
    Verification,
    VerificationMethod,
    VerificationRun,
    VerificationRunKind,
    VerificationRunStatus,
    VerificationStatus,
)
from app.schemas.chunk import SearchRequest, SearchResult
from app.services.claim_validation import find_quote
from app.services.derived_metrics import ActualPoint, ActualsIndex
from app.services.deterministic_check import Outcome, ThreadView, deterministic_check
from app.services.embeddings import EmbeddingProvider
from app.services.ingestion import ProcessingTracker
from app.services.linking import (
    LinkStats,
    Mention,
    company_as_of,
    link_company,
    load_mentions,
    name_keys,
)
from app.services.llm import LLMError, LLMProvider, LLMQuotaExhausted
from app.services.llm_budget import BUDGET_EXHAUSTED, BudgetExhausted, LLMBudget
from app.services.metrics import EVENT_METRICS, label
from app.services.periods import ordinal_label
from app.services.search import search_chunks
from app.services.verification_prompts import (
    JUDGE_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    JudgeResult,
    SummaryResult,
    judge_retry_message,
)

logger = logging.getLogger(__name__)

VERIFIER_VERSION = "verify-v3"
EVIDENCE_TOP_K = 5
EXPLANATION_TOP_K = 2
MAX_ERROR_MESSAGE_CHARS = 1000
MAX_REASONING_CHARS = 600
LLM_QUOTA_EXHAUSTED = "llm quota exhausted"

Route = Literal["mark_dropped", "deterministic_check", "gather_evidence", "unverifiable"]


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ context


@dataclass
class VerifyContext:
    """Everything one verify run shares across threads."""

    session: AsyncSession
    company_id: uuid.UUID
    threads: dict[uuid.UUID, ThreadView]
    actuals: ActualsIndex
    as_of: int | None
    llm: LLMProvider
    embedder: EmbeddingProvider
    budget: LLMBudget
    settings: Settings
    run_id: uuid.UUID | None = None
    embedding_calls: int = 0
    llm_disabled_reason: str | None = None
    statuses: Counter[str] = field(default_factory=Counter)

    @property
    def as_of_label(self) -> str | None:
        return ordinal_label(self.as_of) if self.as_of is not None else None


class VerifyState(TypedDict, total=False):
    thread_id: uuid.UUID
    route: Route
    evidence: list[SearchResult]
    outcome: Outcome
    explanation_chunk_ids: list[uuid.UUID]
    explanation_summary: str | None
    verification_id: uuid.UUID


# ------------------------------------------------------------------- routing


def choose_route(thread: ThreadView) -> Route:
    latest = thread.latest
    if thread.is_dropped:
        return "mark_dropped"
    if latest.metric in EVENT_METRICS:
        return "gather_evidence"
    reported_metric = latest.metric not in (None, MetricNormalized.OTHER)
    if latest.claim_type in (ClaimType.NUMERIC, ClaimType.DIRECTIONAL) and reported_metric:
        return "unverifiable" if latest.target_end is None else "deterministic_check"
    # Qualitative claims, metric "other", cost reduction without numbers.
    return "unverifiable" if latest.target_end is None else "gather_evidence"


def needs_explanation(thread: ThreadView, outcome: Outcome) -> bool:
    return (
        outcome.status in (VerificationStatus.MISSED, VerificationStatus.DROPPED)
        or thread.was_delayed
    )


# --------------------------------------------------------------- search helper


async def _later_management_chunks(
    ctx: VerifyContext, query: str, after_call: int, top_k: int
) -> list[SearchResult]:
    ctx.embedding_calls += 1
    request = SearchRequest(
        query=query[:2000],
        top_k=top_k,
        speaker_role=SpeakerRole.MANAGEMENT,
        from_period=ordinal_label(after_call + 1),
    )
    return await search_chunks(ctx.session, ctx.embedder, ctx.company_id, request)


def _evidence_block(
    evidence: list[SearchResult], prefix: str
) -> tuple[str, dict[str, SearchResult]]:
    ids: dict[str, SearchResult] = {}
    lines = []
    for i, chunk in enumerate(evidence, start=1):
        eid = f"{prefix}{i}"
        ids[eid] = chunk
        who = chunk.speaker or "management"
        lines.append(
            f"[{eid}] {chunk.period_label} call, {who}, p.{chunk.page_start}:\n{chunk.text}"
        )
    return "\n\n".join(lines), ids


_AMOUNT_RE = re.compile(
    r"(?:(?:us)?\$|rs\.?|inr|usd)\s*\d[\d,.]*\s*(?:bn|billion|mn|million|crore|cr|lakh)?"
    r"|\d[\d,.]*\s*(?:bn|billion|mn|million|crore|cr|lakh)\b(?:\s*(?:dollars?|usd))?",
    re.IGNORECASE,
)
MAX_KEY_TERMS = 12


def key_terms(thread: ThreadView) -> list[str]:
    """What identifies the promise: names in its quotes ("Line 4", "Aura"), amounts
    ("$2 billion") and the metric as said, first mention first."""
    terms: dict[str, None] = {}
    for mention in thread.mentions:
        terms.setdefault(" ".join(mention.metric_raw.split()).lower(), None)
        for name in sorted(name_keys(mention.quote)):
            terms.setdefault(name, None)
        for amount in _AMOUNT_RE.findall(mention.quote):
            terms.setdefault(" ".join(amount.split()).lower(), None)
    return list(terms)[:MAX_KEY_TERMS]


def judge_prompt(
    thread: ThreadView, evidence: list[SearchResult], as_of: str | None
) -> tuple[str, dict[str, SearchResult]]:
    latest = thread.latest
    first = thread.first
    block, ids = _evidence_block(evidence, "E")
    history = "\n".join(
        f'- {m.call_label}: "{" ".join(m.quote.split())}" (target {m.target_label or "none"})'
        for m in thread.mentions
    )
    user = (
        f"PROMISE ({label(latest.metric)}): {thread.title}\n"
        f'ORIGINAL QUOTE ({first.call_label} call): "{" ".join(first.quote.split())}"\n'
        f"KEY TERMS: {'; '.join(key_terms(thread)) or 'none'}\n"
        f"Said on these calls:\n{history}\n"
        f"Target period: {latest.target_label or 'none given'}. "
        f"Latest data available: {as_of or 'none'}.\n\n"
        "Use only evidence clearly about this same promise; if an excerpt may be about a "
        "different deal, project or product, answer unverifiable.\n\n"
        f"EVIDENCE (later calls, management):\n{block}"
    )
    return user, ids


def validate_judgment(
    result: JudgeResult, ids: dict[str, SearchResult]
) -> tuple[str | None, SearchResult | None, str | None]:
    """(error, cited chunk, verbatim quote). The quote must be in the cited chunk."""
    if result.status == "unverifiable" and not result.quote.strip():
        return None, None, None
    chunk = ids.get(result.quote_evidence_id)
    if chunk is None:
        return (
            f"quote_evidence_id {result.quote_evidence_id!r} is not one of {list(ids)}",
            None,
            None,
        )
    match = find_quote(result.quote, chunk.text)
    if match is None:
        return f"the quote is not in {result.quote_evidence_id}", None, None
    return None, chunk, match.source_span


# --------------------------------------------------------------------- nodes


def build_graph(ctx: VerifyContext) -> Any:
    """Compile the per-thread graph. Nodes close over the run's context.

    LangGraph's ``add_node`` overloads don't type-check under strict mypy, so the
    builder is typed ``Any``; ``verify_thread`` is the typed entry point.
    """

    def thread_of(state: VerifyState) -> ThreadView:
        return ctx.threads[state["thread_id"]]

    async def load_thread(state: VerifyState) -> dict[str, Any]:
        return {"route": choose_route(thread_of(state))}

    async def mark_dropped(state: VerifyState) -> dict[str, Any]:
        thread = thread_of(state)
        latest = thread.latest
        recent = ctx.settings.verify_dropped_after_calls
        return {
            "outcome": Outcome(
                VerificationStatus.DROPPED,
                f"Dropped: last mentioned on the {thread.last_seen_period} call and absent from "
                f"the company's last {recent} call(s), while the target period "
                f"{latest.target_label} has not ended (latest data: {ctx.as_of_label}).",
                details={"last_seen_period": thread.last_seen_period},
            )
        }

    async def deterministic(state: VerifyState) -> dict[str, Any]:
        outcome = deterministic_check(
            thread_of(state),
            ctx.actuals,
            ctx.as_of,
            tolerance_pct=ctx.settings.verify_tolerance_pct,
            band_pct=ctx.settings.verify_directional_band_pct,
        )
        return {"outcome": outcome}

    async def unverifiable(state: VerifyState) -> dict[str, Any]:
        thread = thread_of(state)
        latest = thread.latest
        if latest.target_end is None:
            spoken = f' ("{latest.timeframe_raw}")' if latest.timeframe_raw else ""
            reason = (
                "Unverifiable: no target period could be resolved from what was said on the "
                f"{latest.call_label} call{spoken}."
            )
        else:
            reason = "Unverifiable: the claim has no reported metric to check against."
        return {"outcome": Outcome(VerificationStatus.UNVERIFIABLE, reason)}

    async def gather_evidence(state: VerifyState) -> dict[str, Any]:
        thread = thread_of(state)
        latest = thread.latest
        try:
            evidence = await _later_management_chunks(
                ctx, latest.quote, thread.first.call_ordinal, EVIDENCE_TOP_K
            )
        except AppError as exc:
            return {
                "outcome": Outcome(
                    VerificationStatus.UNVERIFIABLE,
                    f"Unverifiable: evidence search failed ({exc.message}).",
                    method=None,
                )
            }
        if evidence:
            return {"evidence": evidence}
        over = (
            latest.target_end is not None
            and ctx.as_of is not None
            and latest.target_end <= ctx.as_of
        )
        if over:
            outcome = Outcome(
                VerificationStatus.UNVERIFIABLE,
                "Unverifiable: no later management commentary was found to judge it.",
            )
        else:
            outcome = Outcome(
                VerificationStatus.IN_PROGRESS,
                f"In progress: no later commentary yet and the target period "
                f"{latest.target_label or '(none)'} has not ended (latest data: "
                f"{ctx.as_of_label}).",
            )
        return {"outcome": outcome}

    async def llm_judge(state: VerifyState) -> dict[str, Any]:
        thread = thread_of(state)
        evidence = state.get("evidence", [])
        user, ids = judge_prompt(thread, evidence, ctx.as_of_label)
        if ctx.llm_disabled_reason:
            return {"outcome": _no_llm(ctx.llm_disabled_reason)}
        error = None
        for attempt in (1, 2):
            prompt = user if error is None else f"{user}\n\n{judge_retry_message(error)}"
            try:
                result = await ctx.budget.complete_json(
                    ctx.llm, JUDGE_SYSTEM_PROMPT, prompt, JudgeResult, ctx.settings.verify_model
                )
            except BudgetExhausted:
                return {"outcome": _no_llm(BUDGET_EXHAUSTED)}
            except LLMQuotaExhausted:
                ctx.llm_disabled_reason = LLM_QUOTA_EXHAUSTED
                return {"outcome": _no_llm(LLM_QUOTA_EXHAUSTED)}
            except LLMError as exc:
                return {
                    "outcome": Outcome(
                        VerificationStatus.UNVERIFIABLE,
                        f"Unverifiable: the LLM call failed ({str(exc)[:200]}).",
                        method=VerificationMethod.LLM,
                    )
                }
            error, chunk, quote = validate_judgment(result, ids)
            if error is None:
                return {"outcome": _judged(result, chunk, quote, ids)}
            logger.info("Judge quote check failed (attempt %d): %s", attempt, error)
        return {
            "outcome": Outcome(
                VerificationStatus.UNVERIFIABLE,
                "Unverifiable: the model's supporting quote could not be found in the evidence "
                "twice, so its verdict was discarded.",
                method=VerificationMethod.LLM,
                evidence_chunk_ids=[c.chunk_id for c in evidence],
                details={"rejected_reason": error},
            )
        }

    async def find_explanation(state: VerifyState) -> dict[str, Any]:
        thread = thread_of(state)
        latest = thread.latest
        name = latest.metric_raw if thread.metric in EVENT_METRICS else label(latest.metric)
        query = f"{name} {latest.target_label or ''} reason impact delay".strip()
        try:
            chunks = await _later_management_chunks(
                ctx, query, thread.first.call_ordinal, EXPLANATION_TOP_K
            )
        except AppError as exc:
            logger.warning("Explanation search failed for thread %s: %s", thread.id, exc.message)
            return {"explanation_chunk_ids": []}
        summary = None
        if chunks and ctx.settings.verify_summarize_explanations:
            summary = await _summarize(ctx, thread, chunks)
        return {
            "explanation_chunk_ids": [c.chunk_id for c in chunks],
            "explanation_summary": summary,
        }

    async def persist(state: VerifyState) -> dict[str, Any]:
        thread = thread_of(state)
        outcome = state["outcome"]
        await ctx.session.execute(
            update(Verification)
            .where(Verification.thread_id == thread.id, Verification.is_latest.is_(True))
            .values(is_latest=False)
        )
        verification = Verification(
            id=uuid.uuid4(),
            claim_id=thread.latest.claim_id,
            thread_id=thread.id,
            verification_run_id=ctx.run_id,
            actual_id=outcome.actual.actual_id if outcome.actual else None,
            status=outcome.status,
            method=outcome.method,
            as_of_period=ctx.as_of_label,
            verifier_version=VERIFIER_VERSION,
            computed_gap=outcome.computed_gap,
            gap_pct=outcome.gap_pct,
            near_miss=outcome.near_miss,
            reasoning=outcome.reasoning[:4000],
            evidence_chunk_ids=list(outcome.evidence_chunk_ids),
            explanation_chunk_ids=state.get("explanation_chunk_ids", []),
            explanation_summary=state.get("explanation_summary"),
            details=outcome.details,
            is_latest=True,
        )
        ctx.session.add(verification)
        await ctx.session.flush()
        ctx.statuses[outcome.status.value] += 1
        return {"verification_id": verification.id}

    def after_route(state: VerifyState) -> Route:
        return state["route"]

    def after_evidence(state: VerifyState) -> str:
        return after_verdict(state) if "outcome" in state else "llm_judge"

    def after_verdict(state: VerifyState) -> str:
        explain = needs_explanation(thread_of(state), state["outcome"])
        return "find_explanation" if explain else "persist"

    builder: Any = StateGraph(VerifyState)
    builder.add_node("load_thread", load_thread)
    builder.add_node("mark_dropped", mark_dropped)
    builder.add_node("deterministic_check", deterministic)
    builder.add_node("unverifiable", unverifiable)
    builder.add_node("gather_evidence", gather_evidence)
    builder.add_node("llm_judge", llm_judge)
    builder.add_node("find_explanation", find_explanation)
    builder.add_node("persist", persist)

    builder.add_edge(START, "load_thread")
    builder.add_conditional_edges(
        "load_thread",
        after_route,
        {
            name: name
            for name in ("mark_dropped", "deterministic_check", "gather_evidence", "unverifiable")
        },
    )
    verdict_targets = {"find_explanation": "find_explanation", "persist": "persist"}
    builder.add_conditional_edges(
        "gather_evidence", after_evidence, {"llm_judge": "llm_judge", **verdict_targets}
    )
    for node in ("mark_dropped", "deterministic_check", "unverifiable", "llm_judge"):
        builder.add_conditional_edges(node, after_verdict, verdict_targets)
    builder.add_edge("find_explanation", "persist")
    builder.add_edge("persist", END)
    return builder.compile()


def _no_llm(reason: str) -> Outcome:
    return Outcome(VerificationStatus.UNVERIFIABLE, f"Unverifiable: {reason}.", method=None)


def _judged(
    result: JudgeResult, chunk: SearchResult | None, quote: str | None, ids: dict[str, SearchResult]
) -> Outcome:
    evidence = [ids[e].chunk_id for e in result.evidence_ids if e in ids]
    if chunk is not None and chunk.chunk_id not in evidence:
        evidence.insert(0, chunk.chunk_id)
    reasoning = " ".join(result.reasoning.split())[:MAX_REASONING_CHARS]
    if quote and chunk is not None:
        reasoning += f' Evidence ({chunk.period_label} call): "{quote}"'
    return Outcome(
        VerificationStatus(result.status),
        reasoning,
        method=VerificationMethod.LLM,
        evidence_chunk_ids=evidence,
        details={
            "llm_status": result.status,
            "llm_reasoning": result.reasoning,
            "quote": quote,
            "quote_chunk_id": str(chunk.chunk_id) if chunk else None,
        },
    )


async def _summarize(
    ctx: VerifyContext, thread: ThreadView, chunks: list[SearchResult]
) -> str | None:
    block, ids = _evidence_block(chunks, "X")
    user = f"PROMISE: {thread.title}\n\nEXCERPTS:\n{block}"
    try:
        result = await ctx.budget.complete_json(
            ctx.llm, SUMMARY_SYSTEM_PROMPT, user, SummaryResult, ctx.settings.verify_model
        )
    except (BudgetExhausted, LLMError) as exc:
        logger.info("Explanation summary skipped for thread %s: %s", thread.id, exc)
        return None
    if result.quote.strip():
        chunk = ids.get(result.quote_excerpt_id)
        if chunk is None or find_quote(result.quote, chunk.text) is None:
            return None  # unsupported summary: keep only the chunks
    return " ".join(result.summary.split())[:MAX_REASONING_CHARS]


async def verify_thread(graph: Any, thread_id: uuid.UUID) -> VerifyState:
    state: VerifyState = await graph.ainvoke({"thread_id": thread_id})
    return state


# ------------------------------------------------------------------ loading


async def load_thread_views(
    session: AsyncSession, company_id: uuid.UUID
) -> dict[uuid.UUID, ThreadView]:
    threads = {
        t.id: t
        for t in await session.scalars(
            select(ClaimThread).where(ClaimThread.company_id == company_id)
        )
    }
    change = dict(
        (
            await session.execute(
                select(Claim.id, Claim.change_type).where(Claim.company_id == company_id)
            )
        )
        .tuples()
        .all()
    )
    by_thread: dict[uuid.UUID, list[Mention]] = {}
    for mention in await load_mentions(session, company_id):
        if mention.current_thread_id in threads:
            by_thread.setdefault(mention.current_thread_id, []).append(mention)
    views = {}
    for thread_id, mentions in by_thread.items():
        mentions.sort(key=lambda m: m.sort_key)
        thread = threads[thread_id]
        views[thread_id] = ThreadView(
            id=thread.id,
            company_id=company_id,
            title=thread.title,
            metric=thread.metric_normalized,
            is_dropped=thread.is_dropped,
            last_seen_period=thread.last_seen_period,
            mentions=mentions,
            change_types=[change.get(m.claim_id) for m in mentions],
        )
    return views


async def load_actuals(session: AsyncSession, company_id: uuid.UUID) -> ActualsIndex:
    rows = await session.scalars(select(Actual).where(Actual.company_id == company_id))
    return ActualsIndex(
        ActualPoint(
            metric=a.metric_normalized,
            fiscal_year=a.fiscal_year,
            fiscal_quarter=a.fiscal_quarter,
            value=a.value,
            unit=a.unit,
            id=a.id,
            source_note=a.source_note,
            source_page=a.source_page,
            scope=a.scope,
        )
        for a in rows
    )


async def verify_company(
    session: AsyncSession,
    company_id: uuid.UUID,
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    settings: Settings,
    budget: LLMBudget,
    *,
    run_id: uuid.UUID | None = None,
) -> VerifyContext:
    """Verify every thread of the company (threads must be linked). Commits per thread."""
    ctx = VerifyContext(
        session=session,
        company_id=company_id,
        threads=await load_thread_views(session, company_id),
        actuals=await load_actuals(session, company_id),
        as_of=await company_as_of(session, company_id),
        llm=llm,
        embedder=embedder,
        budget=budget,
        settings=settings,
        run_id=run_id,
    )
    graph = build_graph(ctx)
    ordered = sorted(ctx.threads.values(), key=lambda t: t.first.sort_key)
    for thread in ordered:
        await verify_thread(graph, thread.id)
        await session.commit()
    return ctx


# ---------------------------------------------------------------------- runs


async def start_run(
    session: AsyncSession, company_id: uuid.UUID, kind: VerificationRunKind, settings: Settings
) -> VerificationRun:
    """Create a ``running`` run (committed); 409 if one is already running.

    A ``running`` row left by a crashed process is marked failed first; callers make
    sure no job is active in this process (``ProcessingTracker``).
    """
    await session.execute(
        update(VerificationRun)
        .where(
            VerificationRun.company_id == company_id,
            VerificationRun.status == VerificationRunStatus.RUNNING,
        )
        .values(
            status=VerificationRunStatus.FAILED,
            error_message="Interrupted: the process running it stopped.",
            finished_at=_now(),
        )
    )
    run = VerificationRun(
        company_id=company_id,
        kind=kind,
        verifier_version=VERIFIER_VERSION,
        model=settings.verify_model,
    )
    session.add(run)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "A link/verify run is already active for this company.", code="verification_in_progress"
        ) from exc
    await session.refresh(run)
    return run


def _record(run: VerificationRun, budget: LLMBudget, link: LinkStats | None) -> None:
    run.llm_calls = budget.calls
    run.tokens_in = budget.usage.tokens_in
    run.tokens_out = budget.usage.tokens_out
    if link is not None:
        run.threads_total = link.threads_total
        run.threads_created = link.threads_created
        if link.as_of is not None:
            run.as_of_period = ordinal_label(link.as_of)


async def execute_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    settings: Settings,
) -> VerificationRun:
    """Link (and for ``verify`` runs, verify) the run's company. Errors are recorded on
    the run, not raised."""
    run = await session.get(VerificationRun, run_id)
    if run is None:
        raise NotFoundError(f"Verification run {run_id} not found.", code="run_not_found")
    company_id = run.company_id
    budget = LLMBudget(settings.verify_max_llm_calls)
    link: LinkStats | None = None
    try:
        link = await link_company(
            session,
            company_id,
            llm,
            budget,
            model=settings.verify_model,
            batch_size=settings.verify_link_batch_size,
            dropped_after_calls=settings.verify_dropped_after_calls,
        )
        if run.kind is VerificationRunKind.VERIFY:
            ctx = await verify_company(
                session, company_id, llm, embedder, settings, budget, run_id=run_id
            )
            run = await session.get(VerificationRun, run_id) or run
            run.threads_verified = sum(ctx.statuses.values())
            run.embedding_calls = ctx.embedding_calls
            run.status_counts = dict(ctx.statuses)
        _record(run, budget, link)
        run.status = VerificationRunStatus.COMPLETED
        run.finished_at = _now()
        await session.commit()
    except Exception as exc:
        await session.rollback()
        message = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_MESSAGE_CHARS]
        logger.exception("Verification run %s failed", run_id)
        run = await session.get(VerificationRun, run_id) or run
        _record(run, budget, link)
        run.status = VerificationRunStatus.FAILED
        run.error_message = message
        run.finished_at = _now()
        await session.commit()
    return run


async def run_in_background(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    settings: Settings,
    run_id: uuid.UUID,
    company_id: uuid.UUID,
    tracker: ProcessingTracker | None = None,
) -> None:
    """Background-task wrapper around ``execute_run``. Never raises."""
    try:
        async with sessionmaker() as session:
            await execute_run(session, run_id, llm, embedder, settings)
    except Exception:
        logger.exception("Verification run %s crashed", run_id)
    finally:
        if tracker is not None:
            tracker.finish(company_id)


async def link_and_verify(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    settings: Settings,
    company_id: uuid.UUID,
    *,
    kind: VerificationRunKind = VerificationRunKind.VERIFY,
) -> VerificationRun:
    """Start and run in one go (scripts and tests)."""
    async with sessionmaker() as session:
        run = await start_run(session, company_id, kind, settings)
        return await execute_run(session, run.id, llm, embedder, settings)
