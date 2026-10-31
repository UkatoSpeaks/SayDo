"""Token cost and claim comparison of the extraction prompts on the synthetic transcript.

    uv run python -m scripts.extraction_tokens                  # offline (FakeLLMProvider)
    uv run python -m scripts.extraction_tokens --live           # real Groq, v1 and v2
    uv run python -m scripts.extraction_tokens --live --prompt extract-v2
    uv run python -m scripts.extraction_tokens --live --cache-probe

Runs the full extraction pipeline (pre-filter, batching, validation, dedupe) on the
synthetic call in ``tests/concall_fixture.py``. It uses TEST_DATABASE_URL (the
database the test suite wipes; never the dev database) with fake embeddings, and
deletes what it created when done. Do not run it while the tests are running.

For each prompt version it prints the tokens per run split into system prompt
instructions, few-shot examples, chunk text, analyst context, labels (CALL line,
chunk headers, repair messages), schema/formatting overhead and output. The
prompt split is a tiktoken estimate; "schema + formatting" is what the API
counted on top of it (the JSON schema, chat formatting, tokenizer differences).
Offline, output is 0 and the schema is estimated.

With ``--live`` both versions run against Groq and their claims are compared.
``--cache-probe`` sends the same small v2 request three times and prints
``prompt_tokens_details.cached_tokens`` and the ``x-ratelimit-remaining-tokens``
header, to check whether cached tokens count against the per-minute limit.

The Groq free tier for openai/gpt-oss-120b is 200K tokens/day; a live v1+v2 run
of the synthetic call uses roughly 30K.
"""

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from alembic import command
from alembic.config import Config
from pydantic import BaseModel
from sqlalchemy import delete, make_url, select

from app.core.config import BACKEND_DIR, Settings
from app.main import create_app
from app.models import Claim, Company, Document, ExtractionRun
from app.services import extraction
from app.services.claim_schema import llm_json_schema
from app.services.claim_validation import quote_similarity
from app.services.llm import (
    MESSAGE_OVERHEAD_TOKENS,
    GroqProvider,
    LLMError,
    LLMProvider,
    LLMUsage,
    Message,
    create_llm_provider,
    usage_from_response,
)
from app.services.tokens import count_tokens
from tests.concall_fixture import build_concall_pdf

VERSIONS = (extraction.PROMPT_V1, extraction.PROMPT_V2)
FREE_TIER_TPD = 200_000


# ------------------------------------------------------------- recording


@dataclass
class CallRecord:
    request: extraction.ExtractionRequest
    messages: list[Message]
    usage: LLMUsage
    error: str | None = None


@dataclass
class Recorder:
    calls: list[CallRecord] = field(default_factory=list)
    last_request: extraction.ExtractionRequest | None = None


class RecordingProvider(LLMProvider):
    """Wraps a provider and records every API call (repairs included) with its usage."""

    def __init__(self, inner: LLMProvider, recorder: Recorder) -> None:
        self.inner = inner
        self.recorder = recorder
        self.default_model = inner.default_model

    async def _complete(
        self, messages: list[Message], schema: type[BaseModel], model: str
    ) -> tuple[str, LLMUsage]:
        request = self.recorder.last_request
        assert request is not None
        try:
            content, usage = await self.inner._complete(messages, schema, model)
        except LLMError as exc:
            self.recorder.calls.append(CallRecord(request, list(messages), exc.usage, str(exc)))
            raise
        self.recorder.calls.append(CallRecord(request, list(messages), usage))
        return content, usage


def _record_requests(recorder: Recorder) -> Callable[[], None]:
    """Patch ``extraction.build_request`` so each call knows which request it belongs to."""
    original = extraction.build_request

    def recording(*args: Any, **kwargs: Any) -> extraction.ExtractionRequest:
        request = original(*args, **kwargs)
        recorder.last_request = request
        return request

    extraction.build_request = recording

    def restore() -> None:
        extraction.build_request = original

    return restore


# -------------------------------------------------------------- accounting


@dataclass
class Breakdown:
    instructions: int = 0
    examples: int = 0
    chunk_text: int = 0
    context: int = 0
    labels: int = 0
    overhead: int = 0
    output: int = 0
    reasoning: int = 0
    cached: int = 0
    prompt_api: int = 0
    requests: int = 0

    @property
    def prompt(self) -> int:
        return (
            self.instructions
            + self.examples
            + self.chunk_text
            + self.context
            + self.labels
            + self.overhead
        )

    @property
    def total(self) -> int:
        return self.prompt + self.output


def breakdown(calls: list[CallRecord], *, live: bool) -> Breakdown:
    b = Breakdown()
    for call in calls:
        if live and call.usage.tokens_in == 0:
            continue  # refused before it ran (e.g. daily quota): no tokens used
        request = call.request
        system = count_tokens(request.system)
        examples = count_tokens(request.examples_text)
        chunk_text = sum(count_tokens(t) for t in request.chunk_texts)
        context = sum(count_tokens(t) for t in request.context_texts)
        user_messages = sum(count_tokens(m["content"]) for m in call.messages[1:])
        estimate = system + user_messages + MESSAGE_OVERHEAD_TOKENS * len(call.messages)
        if live:
            overhead = call.usage.tokens_in - (system + user_messages)
        else:
            schema = json.dumps(llm_json_schema(request.schema))
            overhead = estimate - (system + user_messages) + count_tokens(schema)
        b.instructions += system - examples
        b.examples += examples
        b.chunk_text += chunk_text
        b.context += context
        b.labels += user_messages - chunk_text - context
        b.overhead += overhead
        b.output += call.usage.tokens_out
        b.reasoning += call.usage.tokens_reasoning
        b.cached += call.usage.tokens_cached
        b.prompt_api += call.usage.tokens_in
        b.requests += 1
    return b


# ------------------------------------------------------------------- runs


@dataclass
class RunResult:
    version: str
    run: ExtractionRun
    claims: list[Claim]
    calls: list[CallRecord]
    seconds: float


def _migrate(database_url: str) -> None:
    async def ensure_database() -> None:
        url = make_url(database_url)
        conn = await asyncpg.connect(
            host=url.host,
            port=url.port or 5432,
            user=url.username,
            password=url.password,
            database="postgres",
        )
        try:
            if not await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", url.database
            ):
                await conn.execute(f'CREATE DATABASE "{url.database}"')
        finally:
            await conn.close()

    asyncio.run(ensure_database())
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


async def run_versions(
    settings: Settings, provider: LLMProvider, versions: list[str]
) -> list[RunResult]:
    app = create_app(settings)
    sessionmaker = app.state.sessionmaker
    transport = httpx.ASGITransport(app=app)
    company_id: str | None = None
    results: list[RunResult] = []
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://script") as client:
            symbol = f"TOK{uuid.uuid4().hex[:6].upper()}"
            response = await client.post(
                "/companies", json={"name": "Acme Industries Limited", "nse_symbol": symbol}
            )
            response.raise_for_status()
            company_id = response.json()["id"]
            response = await client.post(
                f"/companies/{company_id}/documents",
                files={"file": ("synthetic.pdf", build_concall_pdf(), "application/pdf")},
                data={"doc_type": "transcript", "period_label": "Q2FY26"},
            )
            response.raise_for_status()
            document_id = uuid.UUID(response.json()["id"])
            status = (await client.get(f"/documents/{document_id}")).json()["status"]
            if status != "embedded":
                raise SystemExit(f"Synthetic transcript not embedded (status {status!r}).")

        for n, version in enumerate(versions):
            recorder = Recorder()
            restore = _record_requests(recorder)
            run_settings = settings.model_copy(
                update={
                    "extraction_prompt_version": version,
                    # The v1 baseline is the pipeline as it was: keyword pre-filter only.
                    "extraction_strict_prefilter": version != extraction.PROMPT_V1,
                }
            )
            started = time.perf_counter()
            try:
                run = await extraction.extract_claims(
                    sessionmaker,
                    RecordingProvider(provider, recorder),
                    document_id,
                    reextract=n > 0,
                    settings=run_settings,
                )
            finally:
                restore()
            seconds = time.perf_counter() - started
            async with sessionmaker() as session:
                claims = list(
                    await session.scalars(
                        select(Claim)
                        .where(Claim.document_id == document_id)
                        .order_by(Claim.page_number, Claim.quote)
                    )
                )
            results.append(RunResult(version, run, claims, recorder.calls, seconds))
    finally:
        if company_id is not None:
            async with sessionmaker() as session:
                company = uuid.UUID(company_id)
                await session.execute(delete(Document).where(Document.company_id == company))
                await session.execute(delete(Company).where(Company.id == company))
                await session.commit()
        await app.state.engine.dispose()
    return results


# ----------------------------------------------------------------- report


def _row(label: str, values: list[int | str]) -> str:
    return f"  {label:34}" + "".join(f"{v:>12}" for v in values)


def report_tokens(results: list[RunResult], *, live: bool) -> dict[str, Breakdown]:
    b = {r.version: breakdown(r.calls, live=live) for r in results}
    print("\n======== Tokens per run (synthetic transcript) ========")
    print(_row("", [r.version for r in results]))
    rows: list[tuple[str, Callable[[Breakdown], int | str]]] = [
        ("requests", lambda x: x.requests),
        ("system prompt: instructions", lambda x: x.instructions),
        ("system prompt: few-shot examples", lambda x: x.examples),
        ("chunk text", lambda x: x.chunk_text),
        ("analyst context", lambda x: x.context),
        ("labels / CALL line / repairs", lambda x: x.labels),
        ("schema + formatting" + ("" if live else " (est.)"), lambda x: x.overhead),
        ("= prompt tokens", lambda x: x.prompt),
        ("  of which cached", lambda x: x.cached if live else "n/a"),
        ("output tokens", lambda x: x.output if live else "n/a"),
        ("  of which reasoning", lambda x: x.reasoning if live else "n/a"),
        ("= total", lambda x: x.total),
    ]
    for label, get in rows:
        print(_row(label, [get(b[r.version]) for r in results]))
    print(
        _row(
            "chunks sent / skipped",
            [f"{r.run.chunks_processed}/{r.run.chunks_skipped}" for r in results],
        )
    )
    print(
        _row(
            "total per chunk sent",
            [round(b[r.version].total / max(1, r.run.chunks_processed)) for r in results],
        )
    )
    print(_row("seconds", [f"{r.seconds:.1f}" for r in results]))
    return b


@dataclass(frozen=True)
class Transcript:
    """A typical real transcript, for the projection."""

    chunks: int = 40  # management chunks sent after the pre-filter
    chunk_tokens: int = 450  # chunks are cut at 500 tokens; most long turns are near that
    claims: int = 40


def project(
    results: list[RunResult], b: dict[str, Breakdown], settings: Settings, t: Transcript
) -> None:
    """Scale measured per-request, per-chunk and per-claim costs to transcript ``t``.

    The synthetic call is far denser in claims (and its chunks shorter) than a real
    one, so its own totals are not a good per-transcript figure.
    """
    print(
        f"\n======== Projection: {t.chunks} chunks x {t.chunk_tokens} tokens, "
        f"{t.claims} claims ========"
    )
    for r in results:
        x = b[r.version]
        if not x.requests:
            continue
        sent = max(1, r.run.chunks_processed)
        returned = sum(c.mention_count for c in r.claims) + r.run.claims_rejected
        fixed = (x.instructions + x.examples + x.overhead + x.reasoning) / x.requests
        per_chunk = (x.context + x.labels) / sent + t.chunk_tokens
        per_claim = (x.output - x.reasoning) / max(1, returned)
        if r.version == extraction.PROMPT_V1:
            requests = t.chunks
        else:
            claims_per_chunk = t.claims / t.chunks
            per_batch = max(
                1,
                min(
                    settings.extraction_batch_max_chunks,
                    settings.extraction_batch_max_tokens // t.chunk_tokens,
                    # The batch planner budgets on forward-looking sentences, ~2 per claim.
                    int(
                        (
                            settings.extraction_batch_max_output_tokens
                            - extraction.OUTPUT_TOKENS_PER_REQUEST
                        )
                        // (extraction.OUTPUT_TOKENS_PER_CLAIM * max(1.0, 2 * claims_per_chunk))
                    ),
                ),
            )
            requests = math.ceil(t.chunks / per_batch)
        total = round(fixed * requests + per_chunk * t.chunks + per_claim * t.claims)
        cached = round(x.cached / x.requests * requests)
        print(
            f"  {r.version}: {requests} requests x {fixed:,.0f} (prompt + reasoning) "
            f"+ {t.chunks} chunks x {per_chunk:,.0f} + {t.claims} claims x {per_claim:,.0f} "
            f"= {total:,} tokens -> {FREE_TIER_TPD / total:.1f} transcripts/day"
            + (
                f"; ~{cached:,} of them cached ({FREE_TIER_TPD / (total - cached):.1f}/day "
                "if cached tokens did not count)"
                if cached
                else ""
            )
        )


@dataclass(frozen=True)
class _Key:
    metric: str
    low: str | None
    high: str | None
    year: int | None
    quarter: int | None


def _key(c: Claim) -> _Key:
    return _Key(
        c.metric_normalized or "",
        str(c.target_low) if c.target_low is not None else None,
        str(c.target_high) if c.target_high is not None else None,
        c.target_fiscal_year,
        c.target_fiscal_quarter,
    )


def _label(c: Claim) -> str:
    target = f" {c.target_low}-{c.target_high} {c.unit}" if c.target_low is not None else ""
    period = f" FY{c.target_fiscal_year}" if c.target_fiscal_year else ""
    period += f"Q{c.target_fiscal_quarter}" if c.target_fiscal_quarter else ""
    return f"{c.metric_normalized} {c.claim_type.value}{target}{period} | {c.quote[:90]!r}"


def compare(a: RunResult, b: RunResult) -> None:
    print(f"\n======== Claims: {a.version} vs {b.version} ========")
    for r in (a, b):
        print(
            f"  {r.version}: {len(r.claims)} claims (run: {r.run.claims_created} created, "
            f"{r.run.claims_rejected} rejected), status {r.run.status.value}"
        )
    unmatched_b = list(b.claims)
    matched = 0
    only_a: list[Claim] = []
    differ: list[tuple[Claim, Claim]] = []
    field_notes: list[str] = []
    for claim in a.claims:
        exact = next(
            (
                c
                for c in unmatched_b
                if _key(c) == _key(claim) and quote_similarity(c.quote, claim.quote) >= 0.6
            ),
            None,
        )
        if exact is not None:
            unmatched_b.remove(exact)
            matched += 1
            notes = [
                f"{name} {getattr(claim, attr)!s} -> {getattr(exact, attr)!s}"
                for name, attr in (
                    ("type", "claim_type"),
                    ("direction", "direction"),
                    ("mentions", "mention_count"),
                )
                if getattr(claim, attr) != getattr(exact, attr)
            ]
            if notes:
                field_notes.append(
                    f"{claim.metric_normalized} {_key(claim).low}: {', '.join(notes)}"
                )
            continue
        near = next(
            (
                c
                for c in unmatched_b
                if quote_similarity(c.quote, claim.quote) >= 0.8
                and c.metric_normalized == claim.metric_normalized
            ),
            None,
        )
        if near is not None:
            unmatched_b.remove(near)
            differ.append((claim, near))
        else:
            only_a.append(claim)
    print(f"  matched (same metric, targets, period and a similar quote): {matched}")
    for note in field_notes:
        print(f"    differs: {note}")
    for x, y in differ:
        print(
            f"  ~ same claim, fields differ:\n      {a.version}: {_label(x)}\n"
            f"      {b.version}: {_label(y)}"
        )
    for c in only_a:
        print(f"  - only {a.version}: {_label(c)}")
    for c in unmatched_b:
        print(f"  + only {b.version}: {_label(c)}")


def list_claims(r: RunResult) -> None:
    print(f"\n======== {r.version} claims ========")
    for c in r.claims:
        print(
            f"  [{c.page_number}] x{c.mention_count} conf={c.extraction_confidence:.2f} {_label(c)}"
        )


# ------------------------------------------------------------ cache probe


async def cache_probe(provider: GroqProvider, settings: Settings) -> None:
    """Same v2 request three times; print cached tokens and the TPM header after each."""
    from app.models import ChunkSection
    from app.services.claim_schema import BatchExtractionResult
    from app.services.extraction_prompt import SYSTEM_PROMPT, PromptChunk, build_user_prompt

    user = build_user_prompt(
        company="Acme Industries Limited",
        call_fiscal_year=2026,
        call_fiscal_quarter=2,
        chunks=[
            PromptChunk(
                "C1",
                "Thank you. Revenue grew 18% this quarter.",
                "CFO",
                ChunkSection.PREPARED_REMARKS,
            )
        ],
        questions=[],
    )
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    client = provider._get_client()
    kwargs = provider._request_kwargs(messages, BatchExtractionResult, settings.extraction_model)
    print("\n======== Prompt cache probe (3 identical requests) ========")
    previous: tuple[int, float] | None = None
    for i in range(1, 4):
        raw = await client.chat.completions.with_raw_response.create(**kwargs)
        at = time.monotonic()
        response = await raw.parse()
        usage = usage_from_response(response)
        remaining = int(raw.headers.get("x-ratelimit-remaining-tokens", "-1"))
        limit = int(raw.headers.get("x-ratelimit-limit-tokens", "-1"))
        line = (
            f"  #{i}: prompt {usage.tokens_in}, cached {usage.tokens_cached}, output "
            f"{usage.tokens_out} | TPM remaining {remaining}/{limit}"
        )
        if previous is not None and limit > 0:
            refill = (at - previous[1]) * limit / 60
            used = previous[0] - remaining + refill
            line += (
                f" | used since last ~{used:,.0f} (refill {refill:,.0f}); "
                f"full count would be {usage.total:,}, without cached "
                f"{usage.total - usage.tokens_cached:,}"
            )
        print(line)
        previous = (remaining, at)
        raw_usage = getattr(response, "usage", None)
        if i == 1 and raw_usage is not None:
            details = {k: v for k, v in vars(raw_usage).items() if "details" in k}
            print(f"     usage detail fields: {details}")


# ------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--live", action="store_true", help="call the real Groq API")
    parser.add_argument("--prompt", choices=VERSIONS, action="append", help="default: both")
    parser.add_argument("--cache-probe", action="store_true", help="live: check prompt caching")
    parser.add_argument("--show-claims", action="store_true")
    defaults = Transcript()
    parser.add_argument("--chunks", type=int, default=defaults.chunks, help="projection")
    parser.add_argument("--chunk-tokens", type=int, default=defaults.chunk_tokens)
    parser.add_argument("--claims", type=int, default=defaults.claims)
    args = parser.parse_args()

    base = Settings()
    if make_url(base.test_database_url) == make_url(base.database_url):
        sys.exit("TEST_DATABASE_URL must differ from DATABASE_URL; refusing to use it.")
    if args.live and base.groq_api_key is None:
        sys.exit("GROQ_API_KEY is not set in backend/.env.")
    upload_dir = Path(tempfile.mkdtemp(prefix="saydo-tokens-"))
    settings = base.model_copy(
        update={
            "database_url": base.test_database_url,
            "embedding_provider": "fake",
            "llm_provider": "groq" if args.live else "fake",
            "auto_extract": False,
            "upload_dir": upload_dir,
            "log_level": "WARNING",
        }
    )
    _migrate(settings.database_url)
    versions = args.prompt or list(VERSIONS)

    async def run() -> None:
        provider = create_llm_provider(settings)
        if args.cache_probe:
            if not isinstance(provider, GroqProvider):
                sys.exit("--cache-probe needs --live.")
            await cache_probe(provider, settings)
            if not args.prompt:
                return
        results = await run_versions(settings, provider, versions)
        b = report_tokens(results, live=args.live)
        project(results, b, settings, Transcript(args.chunks, args.chunk_tokens, args.claims))
        if args.show_claims or (args.live and len(results) == 1):
            for r in results:
                list_claims(r)
        if len(results) == 2 and args.live:
            compare(results[0], results[1])
            for r in results:
                list_claims(r)

    asyncio.run(run())


if __name__ == "__main__":
    main()
