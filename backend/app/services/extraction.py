"""Claim extraction: management chunks -> LLM -> validation -> ``claims`` rows.

    embedded -> extract_claims -> extracted

- Only ``speaker_role = management`` chunks are sent. For a Q&A answer the
  analyst question just before it is included as context only; quotes are
  checked against the management chunk alone, so a claim can never come from
  analyst or moderator text.
- A keyword pre-filter (``EXTRACTION_PREFILTER``) skips chunks with no
  forward-looking signal before any LLM call. The strict pre-filter
  (``EXTRACTION_STRICT_PREFILTER``) first drops pleasantries and hand-overs ("I will
  now hand over to our CFO") whose "will" would otherwise count as a signal, and
  skips chunks under ``MIN_CHUNK_TOKENS`` unless they hold a forward-looking
  statement with a number or a period.
- ``extract-v2`` packs consecutive chunks into one request (up to
  ``EXTRACTION_BATCH_MAX_TOKENS`` of chunk text and
  ``EXTRACTION_BATCH_MAX_OUTPUT_TOKENS`` of expected reply), so the system prompt
  is paid once per batch. Each claim names its chunk and is validated against
  that chunk only. A batch whose output is invalid even after the repair retry is
  retried one chunk at a time; one cut off at the output limit is split in half.
  ``extract-v1`` sends one chunk per request.
- Resumable: a chunk gets ``claims_extracted_at`` only once its request succeeded,
  and the session commits after each request, so a run paused by the daily quota
  (status ``paused_quota``) or a crash continues from the next unhandled chunk.
- Near-duplicate claims within a document are merged as they arrive; the
  survivor is the highest-confidence version and ``mention_count`` counts them.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer, selectinload

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError
from app.models import (
    Chunk,
    ChunkSection,
    Claim,
    DocType,
    Document,
    DocumentPage,
    DocumentStatus,
    ExtractionRun,
    ExtractionRunStatus,
    SpeakerRole,
)
from app.services import extraction_prompt as prompt_v2
from app.services import extraction_prompt_v1 as prompt_v1
from app.services.claim_schema import BatchExtractionResult, ExtractedClaim, ExtractionResult
from app.services.claim_validation import (
    DedupeKey,
    ValidatedClaim,
    is_duplicate,
    normalize_text,
    numbers_in,
    validate_claims,
)
from app.services.embeddings import EmbeddingProvider
from app.services.ingestion import ProcessingTracker, process_document
from app.services.llm import (
    LLMError,
    LLMOutputError,
    LLMProvider,
    LLMQuotaExhausted,
    LLMTruncatedError,
    LLMUsage,
)
from app.services.tokens import count_tokens, get_encoding

logger = logging.getLogger(__name__)

MAX_ERROR_MESSAGE_CHARS = 1000
PROMPT_V1 = prompt_v1.PROMPT_VERSION
PROMPT_V2 = prompt_v2.PROMPT_VERSION
# Strict pre-filter: chunks shorter than this need a dated or numeric outlook to be sent.
MIN_CHUNK_TOKENS = 40
# Batch sizing: reply tokens per expected claim (compact JSON, measured on gpt-oss-120b,
# rounded up) and per request (reasoning and wrapper).
OUTPUT_TOKENS_PER_CLAIM = 130
OUTPUT_TOKENS_PER_REQUEST = 300
# Analyst context is trimmed to its last N tokens (the question is usually at the end).
MAX_CONTEXT_TOKENS = 400
# How far back to look past continuation pieces of the same answer for the question.
MAX_CONTEXT_LOOKBACK = 3
_PAGE_PROBE_WORDS = 8

_FORWARD_LOOKING_RE = re.compile(
    r"\b(?:expect\w*|guidance|guid(?:e|es|ed|ing)|target\w*|aim\w*|plan(?:s|ned|ning)?|will"
    r"|\w+'ll|going\s+forward|outlook|anticipat\w*|by\s+fy|next\s+(?:year|quarter|fiscal)"
    r"|h[12]|commission\w*|capex|ramp(?:s|ed|ing)?[\s-]+up|doubl\w*|sustain\w*|maintain\w*"
    r"|trajector\w*)\b",
    re.IGNORECASE,
)


def has_forward_looking_signal(text: str) -> bool:
    return bool(_FORWARD_LOOKING_RE.search(text.replace("\u2019", "'")))


# Sentences that are never claims but contain a forward-looking keyword ("will", "'ll").
_CEREMONY_SENTENCE_RE = re.compile(
    r"^(?:(?:so|and|now|with (?:that|this)),?\s+)*(?:i|we)\s+(?:will|'ll|shall|would like to"
    r"|can|may)?\s*(?:now\s+)?(?:hand(?:\s+it)?\s+over|request|invite|pass(?:\s+it)?\s+on"
    r"|turn(?:\s+it)?\s+over|open\s+the\s+(?:floor|line)|ask\s+the\s+moderator)\b"
    r"|^(?:i|we)\s+(?:will|'ll|would|shall)\s+be\s+(?:happy|glad|pleased)\s+to\s+(?:take|answer)\b"
    r"|^(?:i|we)\s+(?:will|'ll)\s+(?:come|get)\s+back\s+to\s+you\b"
    r"|\b(?:take|discuss|connect)\s+(?:it|that|this|with you)?\s*offline\b",
    re.IGNORECASE,
)
# "...and look forward to speaking with you next quarter": the clause is dropped.
_LOOK_FORWARD_RE = re.compile(
    r",?\s*(?:and\s+)?(?:i|we)?\s*(?:look|are\s+looking)\s+forward\s+to\s+"
    r"(?:speaking|talking|interacting|meeting|connecting|seeing|engaging|hearing|your)\b.*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_PERIOD_RE = re.compile(
    r"\b(?:fy\s?'?\d{2,4}|q[1-4]|h[12]|quarters?|years?|months?|annum)\b", re.IGNORECASE
)


def substantive_text(text: str) -> str:
    """``text`` without hand-overs and other ceremony that is never a claim."""
    kept = []
    for sentence in _SENTENCE_SPLIT_RE.split(" ".join(text.replace("\u2019", "'").split())):
        if _CEREMONY_SENTENCE_RE.search(sentence):
            continue
        sentence = _LOOK_FORWARD_RE.sub("", sentence).strip()
        if sentence:
            kept.append(sentence)
    return " ".join(kept)


def expected_claims(text: str) -> int:
    """Rough claim count: sentences with a forward-looking keyword (at least 1)."""
    sentences = _SENTENCE_SPLIT_RE.split(substantive_text(text))
    return max(1, sum(1 for sentence in sentences if has_forward_looking_signal(sentence)))


def skip_reason(text: str, *, strict: bool, token_count: int | None = None) -> str | None:
    """Why the pre-filter skips a management chunk, or ``None`` to send it."""
    if not strict:
        return None if has_forward_looking_signal(text) else "no forward-looking keyword"
    substantive = substantive_text(text)
    if not has_forward_looking_signal(substantive):
        return (
            "pleasantries only"
            if has_forward_looking_signal(text)
            else ("no forward-looking keyword")
        )
    tokens = token_count if token_count is not None else count_tokens(text)
    if tokens < MIN_CHUNK_TOKENS and not (
        numbers_in(substantive) or _PERIOD_RE.search(substantive)
    ):
        return f"under {MIN_CHUNK_TOKENS} tokens with no number or period"
    return None


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_message(exc: BaseException) -> str:
    message = str(exc) if isinstance(exc, LLMError) else f"{type(exc).__name__}: {exc}"
    settings = get_settings()
    for secret in (settings.groq_api_key, settings.gemini_api_key):
        if secret is not None and secret.get_secret_value():
            message = message.replace(secret.get_secret_value(), "[redacted]")
    return message[:MAX_ERROR_MESSAGE_CHARS]


# ------------------------------------------------------------------ context


def _tail_tokens(text: str, max_tokens: int) -> str:
    encoding = get_encoding()
    tokens = encoding.encode(text, disallowed_special=())
    if len(tokens) <= max_tokens:
        return text
    return "... " + encoding.decode(tokens[-max_tokens:]).lstrip()


def analyst_context(chunks: list[Chunk], index: int) -> Chunk | None:
    """The analyst chunk a management Q&A chunk answers, if any.

    Looks at the previous chunk, skipping back over earlier pieces of the same
    management answer (a long answer is split into several chunks).
    """
    chunk = chunks[index]
    if chunk.section is not ChunkSection.QNA:
        return None
    j = index - 1
    steps = 0
    while (
        j >= 0 and chunks[j].speaker_role is SpeakerRole.MANAGEMENT and steps < MAX_CONTEXT_LOOKBACK
    ):
        j -= 1
        steps += 1
    if j >= 0 and chunks[j].speaker_role is SpeakerRole.ANALYST:
        return chunks[j]
    return None


# ---------------------------------------------------------------- batching


def plan_batches(
    chunks: list[Chunk],
    indices: list[int],
    *,
    max_tokens: int,
    max_chunks: int,
    max_output_tokens: int | None = None,
) -> list[list[int]]:
    """Group ``indices`` (in order) into requests of at most ``max_chunks`` chunks,
    ``max_tokens`` of chunk text and ``max_output_tokens`` of expected reply; a chunk
    over a limit goes alone."""
    batches: list[list[int]] = []
    current: list[int] = []
    tokens = output = 0
    for index in indices:
        size = chunks[index].token_count or count_tokens(chunks[index].text)
        reply = OUTPUT_TOKENS_PER_CLAIM * expected_claims(chunks[index].text)
        if current and (
            tokens + size > max_tokens
            or len(current) >= max_chunks
            or (
                max_output_tokens is not None
                and OUTPUT_TOKENS_PER_REQUEST + output + reply > max_output_tokens
            )
        ):
            batches.append(current)
            current, tokens, output = [], 0, 0
        current.append(index)
        tokens += size
        output += reply
    if current:
        batches.append(current)
    return batches


@dataclass
class ExtractionRequest:
    system: str
    user: str
    schema: type[ExtractionResult] | type[BatchExtractionResult]
    # Prompt chunk id ("C1") -> chunk.
    chunks: dict[str, Chunk]
    # For token accounting (scripts/extraction_tokens.py).
    chunk_texts: list[str]
    context_texts: list[str]
    examples_text: str


def build_request(
    prompt_version: str,
    document: Document,
    chunks: list[Chunk],
    batch: list[int],
) -> ExtractionRequest:
    company = document.company.name
    if prompt_version == PROMPT_V1:
        if len(batch) != 1:
            raise ValueError("extract-v1 sends exactly one chunk per request")
        [index] = batch
        chunk = chunks[index]
        context = analyst_context(chunks, index)
        context_text = _tail_tokens(context.text, MAX_CONTEXT_TOKENS) if context else None
        return ExtractionRequest(
            system=prompt_v1.build_system_prompt(
                company, document.fiscal_year, document.fiscal_quarter
            ),
            user=prompt_v1.build_user_prompt(
                chunk_text=chunk.text,
                speaker=chunk.speaker,
                section=chunk.section,
                context_text=context_text,
                context_speaker=context.speaker if context else None,
            ),
            schema=ExtractionResult,
            chunks={"C1": chunk},
            chunk_texts=[chunk.text],
            context_texts=[context_text] if context_text else [],
            examples_text="\n\n".join(
                prompt_v1._render_example(i, shot)
                for i, shot in enumerate(prompt_v1.FEW_SHOTS, start=1)
            ),
        )
    if prompt_version != PROMPT_V2:
        raise ValueError(f"Unknown prompt version {prompt_version!r}")

    questions: list[prompt_v2.PromptQuestion] = []
    question_of: dict[uuid.UUID, int] = {}
    prompt_chunks: list[prompt_v2.PromptChunk] = []
    request_chunks: dict[str, Chunk] = {}
    for n, index in enumerate(batch, start=1):
        chunk = chunks[index]
        context = analyst_context(chunks, index)
        question: int | None = None
        if context is not None:
            if context.id not in question_of:
                question_of[context.id] = len(questions)
                questions.append(
                    prompt_v2.PromptQuestion(
                        _tail_tokens(context.text, MAX_CONTEXT_TOKENS), context.speaker
                    )
                )
            question = question_of[context.id]
        chunk_id = f"C{n}"
        request_chunks[chunk_id] = chunk
        prompt_chunks.append(
            prompt_v2.PromptChunk(chunk_id, chunk.text, chunk.speaker, chunk.section, question)
        )
    return ExtractionRequest(
        system=prompt_v2.SYSTEM_PROMPT,
        user=prompt_v2.build_user_prompt(
            company=company,
            call_fiscal_year=document.fiscal_year,
            call_fiscal_quarter=document.fiscal_quarter,
            chunks=prompt_chunks,
            questions=questions,
        ),
        schema=BatchExtractionResult,
        chunks=request_chunks,
        chunk_texts=[c.text for c in prompt_chunks],
        context_texts=[q.text for q in questions],
        examples_text=prompt_v2.examples_text(),
    )


def group_claims(
    request: ExtractionRequest, result: ExtractionResult | BatchExtractionResult
) -> tuple[dict[str, list[ExtractedClaim]], int]:
    """Claims per prompt chunk id, and how many named a chunk not in the request."""
    if isinstance(result, ExtractionResult):
        [only] = request.chunks
        return {only: list(result.claims)}, 0
    grouped: dict[str, list[ExtractedClaim]] = {chunk_id: [] for chunk_id in request.chunks}
    unknown = 0
    for claim in result.claims:
        if claim.chunk_id in grouped:
            grouped[claim.chunk_id].append(claim)
        else:
            unknown += 1
    return grouped, unknown


# ------------------------------------------------------------------- dedupe


def _key(item: Claim | ValidatedClaim) -> DedupeKey:
    if isinstance(item, Claim):
        return DedupeKey(
            metric_normalized=item.metric_normalized,
            target_fiscal_year=item.target_fiscal_year,
            target_fiscal_quarter=item.target_fiscal_quarter,
            target_low=item.target_low,
            target_high=item.target_high,
            unit=item.unit,
            direction=item.direction.value,
            quote=item.quote,
        )
    return DedupeKey(
        metric_normalized=item.metric_normalized.value,
        target_fiscal_year=item.target_fiscal_year,
        target_fiscal_quarter=item.target_fiscal_quarter,
        target_low=item.target_low,
        target_high=item.target_high,
        unit=item.unit.value if item.unit else None,
        direction=item.direction.value,
        quote=item.quote,
    )


def _apply(claim: Claim, validated: ValidatedClaim) -> None:
    claim.quote = validated.quote
    claim.metric_raw = validated.metric_raw
    claim.metric_normalized = validated.metric_normalized.value
    claim.claim_type = validated.claim_type
    claim.target_low = validated.target_low
    claim.target_high = validated.target_high
    claim.unit = validated.unit.value if validated.unit else None
    claim.direction = validated.direction
    claim.timeframe_raw = validated.timeframe_raw
    claim.target_fiscal_year = validated.target_fiscal_year
    claim.target_fiscal_quarter = validated.target_fiscal_quarter
    claim.timeframe_is_range = validated.timeframe_is_range
    claim.extraction_confidence = validated.confidence
    claim.validation_notes = "; ".join(validated.notes) or None


# ------------------------------------------------------------------ pages


@dataclass
class _PageFinder:
    """Finds the page a quote is on, for chunks that span a page break."""

    pages: dict[int, str]

    def page_of(self, quote: str, page_start: int, page_end: int) -> int:
        if page_start == page_end:
            return page_start
        probe = " ".join(normalize_text(quote).split()[:_PAGE_PROBE_WORDS])
        for page in range(page_start, page_end + 1):
            if probe and probe in self.pages.get(page, ""):
                return page
        return page_start


# -------------------------------------------------------------------- runs


@dataclass
class _RunState:
    claims: list[Claim]
    # chunk id -> (chunk_index, speaker), to spot repeats caused by chunk overlap.
    chunk_positions: dict[uuid.UUID, tuple[int, str | None]] = field(default_factory=dict)
    failed_chunks: list[int] = field(default_factory=list)
    merged: int = 0
    downgraded: int = 0
    usage: LLMUsage = field(default_factory=LLMUsage)


async def _load_document(session: AsyncSession, document_id: uuid.UUID) -> Document:
    document = await session.scalar(
        select(Document).options(selectinload(Document.company)).where(Document.id == document_id)
    )
    if document is None:
        raise NotFoundError(f"Document {document_id} not found.", code="document_not_found")
    return document


def check_can_extract(document: Document, *, reextract: bool) -> None:
    """Raise ``ConflictError`` unless the document is ready for (re-)extraction."""
    if document.status is DocumentStatus.EXTRACTED and not reextract:
        raise ConflictError(
            "Claims have already been extracted; pass reextract=true to rebuild them.",
            code="already_extracted",
        )
    if document.status not in {DocumentStatus.EMBEDDED, DocumentStatus.EXTRACTED}:
        raise ConflictError(
            f"Document must be embedded before extraction (status is '{document.status.value}').",
            code="document_not_ready",
        )


async def start_extraction(
    session: AsyncSession,
    document_id: uuid.UUID,
    *,
    model: str,
    prompt_version: str = PROMPT_V2,
    reextract: bool = False,
) -> ExtractionRun:
    """Validate the document and create a ``running`` run row (committed).

    A ``running`` row left behind by a crashed process is marked failed first;
    callers make sure no run is active in this process (``ProcessingTracker``).
    """
    document = await _load_document(session, document_id)
    check_can_extract(document, reextract=reextract)

    await session.execute(
        update(ExtractionRun)
        .where(
            ExtractionRun.document_id == document_id,
            ExtractionRun.status == ExtractionRunStatus.RUNNING,
        )
        .values(
            status=ExtractionRunStatus.FAILED,
            error_message="Interrupted: the process running it stopped.",
            finished_at=_now(),
        )
    )
    run = ExtractionRun(document_id=document_id, model=model, prompt_version=prompt_version)
    session.add(run)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "An extraction run is already active for this document.",
            code="extraction_in_progress",
        ) from exc
    await session.refresh(run)
    return run


async def _reset_document(session: AsyncSession, document: Document) -> None:
    await session.execute(delete(Claim).where(Claim.document_id == document.id))
    await session.execute(
        update(Chunk).where(Chunk.document_id == document.id).values(claims_extracted_at=None)
    )
    document.status = DocumentStatus.EMBEDDED
    await session.commit()
    logger.info("Document %s: claims deleted for re-extraction", document.id)


async def execute_extraction(
    session: AsyncSession,
    run_id: uuid.UUID,
    provider: LLMProvider,
    settings: Settings,
    *,
    reextract: bool = False,
) -> ExtractionRun:
    """Process every pending management chunk of the run's document.

    Commits after each chunk. Ends with the run ``completed`` (document
    ``extracted``), ``paused_quota`` or ``failed``; errors are recorded on the
    run, not raised.
    """
    run = await session.get(ExtractionRun, run_id)
    if run is None:
        raise NotFoundError(f"Extraction run {run_id} not found.", code="run_not_found")
    document = await _load_document(session, run.document_id)
    try:
        if reextract:
            await _reset_document(session, document)
        await _extract(session, run, document, provider, settings)
    except Exception as exc:
        await session.rollback()
        message = _safe_message(exc)
        logger.error("Extraction run %s failed: %s", run_id, message)
        run = await session.get(ExtractionRun, run_id) or run
        run.status = ExtractionRunStatus.FAILED
        run.error_message = message
        run.finished_at = _now()
        await session.commit()
    return run


async def _extract(
    session: AsyncSession,
    run: ExtractionRun,
    document: Document,
    provider: LLMProvider,
    settings: Settings,
) -> None:
    chunks = list(
        await session.scalars(
            select(Chunk)
            .options(defer(Chunk.embedding))
            .where(Chunk.document_id == document.id)
            .order_by(Chunk.chunk_index)
        )
    )
    pending = [
        i
        for i, c in enumerate(chunks)
        if c.speaker_role is SpeakerRole.MANAGEMENT and c.claims_extracted_at is None
    ]
    already_done = sum(
        1
        for c in chunks
        if c.speaker_role is SpeakerRole.MANAGEMENT and c.claims_extracted_at is not None
    )
    run.chunks_total = len(pending)
    await session.commit()
    if already_done:
        logger.info(
            "Document %s: resuming, %d management chunks already handled", document.id, already_done
        )

    pages = await session.execute(
        select(DocumentPage.page_number, DocumentPage.text).where(
            DocumentPage.document_id == document.id
        )
    )
    finder = _PageFinder({number: normalize_text(text) for number, text in pages})
    state = _RunState(
        claims=list(await session.scalars(select(Claim).where(Claim.document_id == document.id))),
        chunk_positions={c.id: (c.chunk_index, c.speaker) for c in chunks},
    )
    ctx = _Context(session, run, document, provider, settings, chunks, state, finder)

    to_send: list[int] = []
    for index in pending:
        chunk = chunks[index]
        reason = (
            skip_reason(
                chunk.text,
                strict=settings.extraction_strict_prefilter,
                token_count=chunk.token_count,
            )
            if settings.extraction_prefilter
            else None
        )
        if reason is None:
            to_send.append(index)
            continue
        logger.debug("Chunk %d skipped by pre-filter: %s", chunk.chunk_index, reason)
        chunk.claims_extracted_at = _now()
        run.chunks_skipped += 1
    await session.commit()

    if run.prompt_version == PROMPT_V1:
        batches = [[index] for index in to_send]
    else:
        batches = plan_batches(
            chunks,
            to_send,
            max_tokens=settings.extraction_batch_max_tokens,
            max_chunks=settings.extraction_batch_max_chunks,
            max_output_tokens=settings.extraction_batch_max_output_tokens,
        )
    for batch in batches:
        if not await _send(ctx, batch):
            return

    _finish(run, document, state)
    await session.commit()


@dataclass
class _Context:
    session: AsyncSession
    run: ExtractionRun
    document: Document
    provider: LLMProvider
    settings: Settings
    chunks: list[Chunk]
    state: _RunState
    finder: _PageFinder


async def _send(ctx: _Context, batch: list[int]) -> bool:
    """Extract one request's chunks. Returns ``False`` when the run paused (quota)."""
    run, state = ctx.run, ctx.state
    request = build_request(run.prompt_version, ctx.document, ctx.chunks, batch)
    indexes = [ctx.chunks[i].chunk_index for i in batch]
    try:
        result, usage = await ctx.provider.complete_json(
            request.system, request.user, request.schema, ctx.settings.extraction_model
        )
    except LLMQuotaExhausted as exc:
        _add_usage(run, state, exc.usage)
        run.status = ExtractionRunStatus.PAUSED_QUOTA
        run.error_message = _safe_message(exc)
        run.finished_at = _now()
        await ctx.session.commit()
        logger.warning(
            "Extraction run %s paused (daily quota): %d/%d chunks handled",
            run.id,
            run.chunks_processed + run.chunks_skipped,
            run.chunks_total,
        )
        return False
    except LLMOutputError as exc:
        _add_usage(run, state, exc.usage)
        await ctx.session.commit()
        if len(batch) > 1:
            # Cut off at the output limit: halve the batch. Invalid twice: one at a time.
            if isinstance(exc, LLMTruncatedError):
                middle = len(batch) // 2
                parts = [batch[:middle], batch[middle:]]
                how = "splitting the batch in two"
            else:
                parts = [[index] for index in batch]
                how = "retrying them one at a time"
            logger.warning("Chunks %s: %s; %s", indexes, _safe_message(exc), how)
            for part in parts:
                if not await _send(ctx, part):
                    return False
            return True
        state.failed_chunks.append(indexes[0])
        logger.warning("Chunk %d: %s", indexes[0], _safe_message(exc))
        return True
    except LLMError as exc:
        _add_usage(run, state, exc.usage)
        raise

    _add_usage(run, state, usage)
    grouped, unknown = group_claims(request, result)
    if unknown:
        run.claims_rejected += unknown
        logger.info("Chunks %s: rejected %d claim(s) naming an unknown chunk", indexes, unknown)
    for chunk_id, chunk in request.chunks.items():
        _store_claims(ctx, chunk, grouped[chunk_id])
        chunk.claims_extracted_at = _now()
    run.chunks_processed += len(batch)
    await ctx.session.commit()
    return True


def _add_usage(run: ExtractionRun, state: _RunState, usage: LLMUsage) -> None:
    run.tokens_in += usage.tokens_in
    run.tokens_out += usage.tokens_out
    state.usage += usage
    if usage.tokens_cached:
        logger.info(
            "Extraction run %s: %d of %d prompt tokens served from the prompt cache",
            run.id,
            usage.tokens_cached,
            usage.tokens_in,
        )


def _is_overlap_repeat(
    existing: Claim, validated: ValidatedClaim, chunk: Chunk, state: _RunState
) -> bool:
    """The same sentence seen again only because consecutive pieces of one long turn
    overlap by ~50 tokens; that is one mention, not two."""
    if existing.chunk_id is None or existing.chunk_id not in state.chunk_positions:
        return False
    index, speaker = state.chunk_positions[existing.chunk_id]
    return (
        abs(index - chunk.chunk_index) == 1
        and speaker == chunk.speaker
        and normalize_text(existing.quote) == normalize_text(validated.quote)
    )


def _store_claims(ctx: _Context, chunk: Chunk, claims: list[ExtractedClaim]) -> None:
    """Validate ``claims`` against ``chunk`` (the chunk they name) and store or merge them."""
    session, run, document, state, finder = (
        ctx.session,
        ctx.run,
        ctx.document,
        ctx.state,
        ctx.finder,
    )
    outcome = validate_claims(claims, chunk.text, document.fiscal_year, document.fiscal_quarter)
    run.claims_rejected += outcome.rejected
    state.downgraded += outcome.numbers_downgraded
    if outcome.rejected:
        logger.info(
            "Chunk %d: rejected %d claim(s) whose quote is not in the chunk",
            chunk.chunk_index,
            outcome.rejected,
        )
    for validated in outcome.accepted:
        page = finder.page_of(validated.quote, chunk.page_start, chunk.page_end)
        key = _key(validated)
        duplicate = next((c for c in state.claims if is_duplicate(_key(c), key)), None)
        if duplicate is not None:
            if not _is_overlap_repeat(duplicate, validated, chunk, state):
                duplicate.mention_count += 1
            state.merged += 1
            if validated.confidence > duplicate.extraction_confidence:
                _apply(duplicate, validated)
                duplicate.chunk_id = chunk.id
                duplicate.speaker = chunk.speaker
                duplicate.page_number = page
                duplicate.extraction_run_id = run.id
                duplicate.model = run.model
                duplicate.prompt_version = run.prompt_version
            continue
        claim = Claim(
            company_id=document.company_id,
            document_id=document.id,
            chunk_id=chunk.id,
            speaker=chunk.speaker,
            page_number=page,
            extraction_run_id=run.id,
            model=run.model,
            prompt_version=run.prompt_version,
            mention_count=1,
        )
        _apply(claim, validated)
        session.add(claim)
        state.claims.append(claim)
        run.claims_created += 1


def _finish(run: ExtractionRun, document: Document, state: _RunState) -> None:
    run.finished_at = _now()
    logger.info(
        "Extraction run %s: %d chunks total, %d skipped by pre-filter, %d processed, "
        "%d claims created, %d merged as duplicates, %d rejected (quote not in chunk), "
        "%d downgraded (numbers not in source), %d requests, %d+%d tokens "
        "(%d prompt tokens cached, %d reasoning)",
        run.id,
        run.chunks_total,
        run.chunks_skipped,
        run.chunks_processed,
        run.claims_created,
        state.merged,
        run.claims_rejected,
        state.downgraded,
        state.usage.requests,
        run.tokens_in,
        run.tokens_out,
        state.usage.tokens_cached,
        state.usage.tokens_reasoning,
    )
    if state.failed_chunks:
        run.status = ExtractionRunStatus.FAILED
        run.error_message = (
            f"{len(state.failed_chunks)} chunk(s) returned invalid output twice "
            f"(chunk_index {', '.join(map(str, state.failed_chunks))}); "
            "start extraction again to retry only those."
        )
        return
    run.status = ExtractionRunStatus.COMPLETED
    if document.status is DocumentStatus.FAILED:
        document.error_message = None
    document.status = DocumentStatus.EXTRACTED


# ------------------------------------------------------------- entry points


async def run_extraction(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    settings: Settings,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    reextract: bool = False,
    tracker: ProcessingTracker | None = None,
) -> None:
    """Background-task wrapper around ``execute_extraction``. Never raises."""
    try:
        async with sessionmaker() as session:
            await execute_extraction(session, run_id, provider, settings, reextract=reextract)
    except Exception:
        logger.exception("Extraction run %s crashed", run_id)
    finally:
        if tracker is not None:
            tracker.finish(document_id)


async def extract_claims(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    document_id: uuid.UUID,
    *,
    reextract: bool = False,
    settings: Settings | None = None,
) -> ExtractionRun:
    """Start and run an extraction in one go (scripts and tests)."""
    settings = settings or get_settings()
    async with sessionmaker() as session:
        run = await start_extraction(
            session,
            document_id,
            model=settings.extraction_model,
            prompt_version=settings.extraction_prompt_version,
            reextract=reextract,
        )
        return await execute_extraction(session, run.id, provider, settings, reextract=reextract)


async def process_then_extract(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: EmbeddingProvider,
    llm: LLMProvider,
    settings: Settings,
    document_id: uuid.UUID,
    *,
    rechunk: bool = False,
    tracker: ProcessingTracker | None = None,
) -> None:
    """Chunk + embed, then extract claims when ``AUTO_EXTRACT`` is on. Never raises."""
    try:
        await process_document(sessionmaker, embedder, document_id, rechunk=rechunk)
        if not settings.auto_extract:
            return
        async with sessionmaker() as session:
            document = await session.get(Document, document_id)
            if (
                document is None
                or document.doc_type is not DocType.TRANSCRIPT
                or document.status is not DocumentStatus.EMBEDDED
            ):
                return
            try:
                run = await start_extraction(
                    session,
                    document_id,
                    model=settings.extraction_model,
                    prompt_version=settings.extraction_prompt_version,
                )
            except ConflictError as exc:
                logger.info("Auto-extract skipped for %s: %s", document_id, exc.message)
                return
            await execute_extraction(session, run.id, llm, settings)
    except Exception:
        logger.exception("Processing/extraction of document %s crashed", document_id)
    finally:
        if tracker is not None:
            tracker.finish(document_id)
