"""Seed a FICTIONAL company, "Demo Industries Ltd" (DEMOIND), and verify it. No API calls.

Four calls (Q1-Q4 FY26) of chunks (fake 768-dim embeddings from the offline hash
embedder), claims and actuals that cover: a delivered margin target, a missed revenue
target with a later explanation, a plant commissioning delayed twice (renamed from
"Line 4" to "the Pune expansion", linked by the LLM step), a raised guidance, a
lowered-then-met target, a dropped promise, a directional claim, a qualitative launch
and an in-progress FY27 target. Then link + verify run with FakeLLMProvider and
FakeEmbeddingProvider, so the frontend has realistic data.

Idempotent: re-running keeps one copy of everything (actuals are upserted, threads
reused, one latest verification per thread). ``--reset`` deletes the company first.

    cd backend
    uv run python -m scripts.seed_demo            # seed (or refresh) and print
    uv run python -m scripts.seed_demo --reset    # start over
"""

import argparse
import asyncio
import hashlib
import json
import re
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_sessionmaker
from app.models import (
    Chunk,
    ChunkSection,
    Claim,
    ClaimType,
    Company,
    Direction,
    DocType,
    Document,
    DocumentPage,
    DocumentStatus,
    MetricNormalized,
    SpeakerRole,
)
from app.services.actuals_import import import_actuals
from app.services.embeddings import FakeEmbeddingProvider
from app.services.fiscal import parse_period_label
from app.services.linking import name_keys
from app.services.llm import FakeLLMProvider, Message
from app.services.threads import list_threads, scorecard, thread_detail
from app.services.tokens import count_tokens
from app.services.verification import link_and_verify

SYMBOL = "DEMOIND"
NAME = "Demo Industries Ltd"
MD = "Arjun Mehta"
CFO = "Kavita Rao"
ANALYST = "Rohan Gupta"
MODEL = "demo-seed"

M = MetricNormalized
MGMT, ANALYST_ROLE, MODERATOR = SpeakerRole.MANAGEMENT, SpeakerRole.ANALYST, SpeakerRole.MODERATOR
PREPARED, QNA = ChunkSection.PREPARED_REMARKS, ChunkSection.QNA


@dataclass(frozen=True)
class DemoChunk:
    speaker: str
    role: SpeakerRole
    section: ChunkSection
    page: int
    text: str


@dataclass(frozen=True)
class DemoClaim:
    chunk: int  # index into the call's chunks
    quote: str
    metric: MetricNormalized
    metric_raw: str
    claim_type: ClaimType
    timeframe_raw: str | None
    target_fy: int | None
    target_q: int | None = None
    low: str | None = None
    high: str | None = None
    unit: str | None = None
    direction: Direction = Direction.NONE
    is_range: bool = False


def _num(low: str, high: str | None = None, unit: str = "percent") -> dict[str, Any]:
    return {"low": low, "high": high or low, "unit": unit}


CALLS: dict[str, tuple[list[DemoChunk], list[DemoClaim]]] = {
    "Q1FY26": (
        [
            DemoChunk(
                "Moderator",
                MODERATOR,
                PREPARED,
                1,
                "Good day and welcome to the Demo Industries Q1 FY26 earnings conference call. "
                f"I now hand the call over to Mr. {MD}, Managing Director.",
            ),
            DemoChunk(
                MD,
                MGMT,
                PREPARED,
                2,
                "Thank you. Our Pune facility is being expanded to meet demand. We will "
                "commission Line 4 in Q2 FY26, adding 40,000 tonnes of capacity. We will launch "
                "our premium Aura range of products in H2 FY26.",
            ),
            DemoChunk(
                CFO,
                MGMT,
                PREPARED,
                3,
                "For FY26 we are guiding for an EBITDA margin of 18% to 19%. We expect revenue of "
                "Rs. 5,000 crore in FY26. Revenue growth for FY26 should be 10% to 12%. We expect "
                "PAT of Rs. 450 crore in FY26. Capex for FY26 will be Rs. 300 to 350 crore.",
            ),
            DemoChunk(
                ANALYST,
                ANALYST_ROLE,
                QNA,
                4,
                "What are your plans for the balance sheet, and how do you see utilisation?",
            ),
            DemoChunk(
                CFO,
                MGMT,
                QNA,
                4,
                "We aim to reduce net debt to Rs. 200 crore by the end of FY27. Capacity "
                "utilization will improve in FY26 from current levels as demand picks up.",
            ),
        ],
        [
            DemoClaim(
                1,
                "We will commission Line 4 in Q2 FY26, adding 40,000 tonnes of capacity.",
                M.PLANT_COMMISSIONING,
                "Line 4 commissioning",
                ClaimType.QUALITATIVE,
                "in Q2 FY26",
                2026,
                2,
            ),
            DemoClaim(
                1,
                "We will launch our premium Aura range of products in H2 FY26.",
                M.PRODUCT_LAUNCH,
                "Aura premium range launch",
                ClaimType.QUALITATIVE,
                "in H2 FY26",
                2026,
                4,
                is_range=True,
            ),
            DemoClaim(
                2,
                "For FY26 we are guiding for an EBITDA margin of 18% to 19%.",
                M.EBITDA_MARGIN,
                "EBITDA margin",
                ClaimType.NUMERIC,
                "FY26",
                2026,
                **_num("18", "19"),
            ),
            DemoClaim(
                2,
                "We expect revenue of Rs. 5,000 crore in FY26.",
                M.REVENUE,
                "revenue",
                ClaimType.NUMERIC,
                "in FY26",
                2026,
                **_num("5000", unit="inr_crore"),
            ),
            DemoClaim(
                2,
                "Revenue growth for FY26 should be 10% to 12%.",
                M.REVENUE_GROWTH,
                "revenue growth",
                ClaimType.NUMERIC,
                "FY26",
                2026,
                **_num("10", "12"),
            ),
            DemoClaim(
                2,
                "We expect PAT of Rs. 450 crore in FY26.",
                M.PAT,
                "PAT",
                ClaimType.NUMERIC,
                "in FY26",
                2026,
                **_num("450", unit="inr_crore"),
            ),
            DemoClaim(
                2,
                "Capex for FY26 will be Rs. 300 to 350 crore.",
                M.CAPEX,
                "capex",
                ClaimType.NUMERIC,
                "FY26",
                2026,
                **_num("300", "350", unit="inr_crore"),
            ),
            DemoClaim(
                4,
                "We aim to reduce net debt to Rs. 200 crore by the end of FY27.",
                M.NET_DEBT,
                "net debt",
                ClaimType.NUMERIC,
                "by the end of FY27",
                2027,
                direction=Direction.DECREASE,
                **_num("200", unit="inr_crore"),
            ),
            DemoClaim(
                4,
                "Capacity utilization will improve in FY26 from current levels",
                M.CAPACITY_UTILIZATION,
                "capacity utilization",
                ClaimType.DIRECTIONAL,
                "in FY26",
                2026,
                direction=Direction.INCREASE,
            ),
        ],
    ),
    "Q2FY26": (
        [
            DemoChunk(
                "Moderator",
                MODERATOR,
                PREPARED,
                1,
                "Welcome to the Demo Industries Q2 FY26 earnings call. Over to the management.",
            ),
            DemoChunk(
                MD,
                MGMT,
                PREPARED,
                2,
                "Line 4 will now be commissioned in Q3 FY26. The delay is because equipment "
                "delivery from our supplier slipped by a quarter. The Aura launch preparations "
                "are progressing well.",
            ),
            DemoChunk(
                CFO,
                MGMT,
                PREPARED,
                3,
                "We reiterate our EBITDA margin guidance of 18% to 19% for FY26. Given strong "
                "demand, we are raising our FY26 revenue growth guidance to 12% to 14%. We "
                "continue to expect revenue of Rs. 5,000 crore in FY26. Capex for FY26 will "
                "remain Rs. 300 to 350 crore.",
            ),
            DemoChunk(ANALYST, ANALYST_ROLE, QNA, 4, "Any update on deleveraging?"),
            DemoChunk(
                CFO,
                MGMT,
                QNA,
                4,
                "Our target remains to reduce net debt to Rs. 200 crore by the end of FY27.",
            ),
        ],
        [
            DemoClaim(
                1,
                "Line 4 will now be commissioned in Q3 FY26.",
                M.PLANT_COMMISSIONING,
                "Line 4 commissioning",
                ClaimType.QUALITATIVE,
                "in Q3 FY26",
                2026,
                3,
            ),
            DemoClaim(
                2,
                "We reiterate our EBITDA margin guidance of 18% to 19% for FY26.",
                M.EBITDA_MARGIN,
                "EBITDA margin",
                ClaimType.NUMERIC,
                "for FY26",
                2026,
                **_num("18", "19"),
            ),
            DemoClaim(
                2,
                "we are raising our FY26 revenue growth guidance to 12% to 14%.",
                M.REVENUE_GROWTH,
                "revenue growth",
                ClaimType.NUMERIC,
                "FY26",
                2026,
                **_num("12", "14"),
            ),
            DemoClaim(
                2,
                "We continue to expect revenue of Rs. 5,000 crore in FY26.",
                M.REVENUE,
                "revenue",
                ClaimType.NUMERIC,
                "in FY26",
                2026,
                **_num("5000", unit="inr_crore"),
            ),
            DemoClaim(
                2,
                "Capex for FY26 will remain Rs. 300 to 350 crore.",
                M.CAPEX,
                "capex",
                ClaimType.NUMERIC,
                "FY26",
                2026,
                **_num("300", "350", unit="inr_crore"),
            ),
            DemoClaim(
                4,
                "Our target remains to reduce net debt to Rs. 200 crore by the end of FY27.",
                M.NET_DEBT,
                "net debt",
                ClaimType.NUMERIC,
                "by the end of FY27",
                2027,
                direction=Direction.DECREASE,
                **_num("200", unit="inr_crore"),
            ),
        ],
    ),
    "Q3FY26": (
        [
            DemoChunk(
                "Moderator",
                MODERATOR,
                PREPARED,
                1,
                "Welcome to the Demo Industries Q3 FY26 earnings call. Over to the management.",
            ),
            DemoChunk(
                MD,
                MGMT,
                PREPARED,
                2,
                "We successfully launched the Aura premium range in November, and the early "
                "response from dealers has been encouraging.",
            ),
            DemoChunk(
                CFO,
                MGMT,
                PREPARED,
                3,
                "We maintain our EBITDA margin guidance of 18% to 19% for FY26. Given input cost "
                "pressures, we now expect PAT of Rs. 400 crore in FY26. For FY27, we are "
                "targeting an EBITDA margin of 19% to 20%.",
            ),
            DemoChunk(
                ANALYST,
                ANALYST_ROLE,
                QNA,
                4,
                "Can you comment on revenue this quarter, and on the new capacity?",
            ),
            DemoChunk(
                CFO,
                MGMT,
                QNA,
                4,
                "Revenue this quarter was impacted by weak rural demand and a delayed monsoon, "
                "which is the main reason revenue growth was below our expectations.",
            ),
            DemoChunk(
                MD,
                MGMT,
                QNA,
                5,
                "We now expect the Pune expansion to be commissioned in Q1 FY27, as the "
                "environmental clearance is still pending. The reason for this delay is the "
                "longer approval process.",
            ),
        ],
        [
            DemoClaim(
                2,
                "We maintain our EBITDA margin guidance of 18% to 19% for FY26.",
                M.EBITDA_MARGIN,
                "EBITDA margin",
                ClaimType.NUMERIC,
                "for FY26",
                2026,
                **_num("18", "19"),
            ),
            DemoClaim(
                2,
                "Given input cost pressures, we now expect PAT of Rs. 400 crore in FY26.",
                M.PAT,
                "PAT",
                ClaimType.NUMERIC,
                "in FY26",
                2026,
                **_num("400", unit="inr_crore"),
            ),
            DemoClaim(
                2,
                "For FY27, we are targeting an EBITDA margin of 19% to 20%.",
                M.EBITDA_MARGIN,
                "EBITDA margin",
                ClaimType.NUMERIC,
                "For FY27",
                2027,
                **_num("19", "20"),
            ),
            DemoClaim(
                5,
                "We now expect the Pune expansion to be commissioned in Q1 FY27, as the "
                "environmental clearance is still pending.",
                M.PLANT_COMMISSIONING,
                "Pune expansion commissioning",
                ClaimType.QUALITATIVE,
                "in Q1 FY27",
                2027,
                1,
            ),
        ],
    ),
    "Q4FY26": (
        [
            DemoChunk(
                "Moderator",
                MODERATOR,
                PREPARED,
                1,
                "Welcome to the Demo Industries Q4 FY26 earnings call. Over to the management.",
            ),
            DemoChunk(
                MD,
                MGMT,
                PREPARED,
                2,
                "FY26 was a year of steady progress for the company. The Pune expansion remains "
                "on track for Q1 FY27.",
            ),
            DemoChunk(
                CFO,
                MGMT,
                PREPARED,
                3,
                "For FY27 we reiterate our EBITDA margin guidance of 19% to 20%. Revenue for "
                "FY26 was below our guidance; the main reason was the impact of weak rural "
                "demand in the first half.",
            ),
        ],
        [
            DemoClaim(
                1,
                "The Pune expansion remains on track for Q1 FY27.",
                M.PLANT_COMMISSIONING,
                "Pune expansion commissioning",
                ClaimType.QUALITATIVE,
                "for Q1 FY27",
                2027,
                1,
            ),
            DemoClaim(
                2,
                "For FY27 we reiterate our EBITDA margin guidance of 19% to 20%.",
                M.EBITDA_MARGIN,
                "EBITDA margin",
                ClaimType.NUMERIC,
                "For FY27",
                2027,
                **_num("19", "20"),
            ),
        ],
    ),
}

ACTUALS_CSV = """\
nse_symbol,metric_normalized,fiscal_year,fiscal_quarter,value,unit,source_note,source_page
DEMOIND,revenue,2025,,4100,inr_crore,FY25 annual report (demo),12
DEMOIND,revenue,2026,1,1100,inr_crore,Q1 FY26 results (demo),2
DEMOIND,revenue,2026,2,1150,inr_crore,Q2 FY26 results (demo),2
DEMOIND,revenue,2026,3,1170,inr_crore,Q3 FY26 results (demo),2
DEMOIND,revenue,2026,4,1200,inr_crore,Q4 FY26 results (demo),2
DEMOIND,ebitda,2026,1,198,inr_crore,Q1 FY26 results (demo),2
DEMOIND,ebitda,2026,2,212,inr_crore,Q2 FY26 results (demo),2
DEMOIND,ebitda,2026,3,218,inr_crore,Q3 FY26 results (demo),2
DEMOIND,ebitda,2026,4,228,inr_crore,Q4 FY26 results (demo),2
DEMOIND,pat,2026,1,95,inr_crore,Q1 FY26 results (demo),3
DEMOIND,pat,2026,2,100,inr_crore,Q2 FY26 results (demo),3
DEMOIND,pat,2026,3,105,inr_crore,Q3 FY26 results (demo),3
DEMOIND,pat,2026,4,110,inr_crore,Q4 FY26 results (demo),3
DEMOIND,capex,2026,,32000,inr_lakh,FY26 cash flow statement (demo),8
DEMOIND,capacity_utilization,2025,,72,percent,FY25 investor presentation (demo),5
DEMOIND,capacity_utilization,2026,,78,percent,FY26 investor presentation (demo),5
DEMOIND,net_debt,2026,4,260,inr_crore,Q4 FY26 balance sheet (demo),4
"""


# ----------------------------------------------------------- fake LLM


_EVIDENCE_RE = re.compile(r"^\[(E\d+)\] [^\n]*\n(.*?)(?=^\[E\d+\] |\Z)", re.M | re.S)
_ITEM_RE = re.compile(r"^(N\d+) .*?\n\s+may be the same as: ([T\d, ]+)$", re.M)
_VERDICTS = (
    (
        "delivered",
        re.compile(r"successfully (?:launched|commissioned)|has been commissioned", re.I),
    ),
    ("missed", re.compile(r"cancelled|shelved|abandoned", re.I)),
    ("in_progress", re.compile(r"on track|progressing", re.I)),
)


def _sentence_with(text: str, pattern: re.Pattern[str]) -> str | None:
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        if pattern.search(sentence):
            return sentence
    return None


def demo_responder(messages: list[Message]) -> dict[str, Any]:
    """Stands in for Groq: links each ambiguous item to its first candidate, and judges
    from keywords in the excerpts that name the promise, quoting the sentence verbatim."""
    user = messages[1]["content"]
    if "ITEMS (later promises):" in user:
        decisions = [
            {"item": item, "same_as": options.split(",")[0].strip()}
            for item, options in _ITEM_RE.findall(user)
        ]
        return {"decisions": decisions}
    # Like a real model, only use excerpts about this promise: they must name it.
    history = user.split("EVIDENCE", 1)[0]
    names = name_keys(history)
    evidence = [
        (eid, text)
        for eid, text in _EVIDENCE_RE.findall(user)
        if any(re.search(rf"\b{re.escape(n)}\b", text, re.I) for n in names)
    ]
    for status, pattern in _VERDICTS:
        for eid, text in evidence:
            sentence = _sentence_with(text, pattern)
            if sentence:
                return {
                    "status": status,
                    "reasoning": f"A later call says so: management stated it in {eid}.",
                    "evidence_ids": [eid],
                    "quote": sentence,
                    "quote_evidence_id": eid,
                }
    return {
        "status": "unverifiable",
        "reasoning": "The excerpts do not say.",
        "evidence_ids": [],
        "quote": "",
        "quote_evidence_id": "",
    }


# ------------------------------------------------------------- seeding


async def _delete_company(session: AsyncSession, company: Company) -> None:
    # Documents block company deletion (ON DELETE RESTRICT); their chunks, claims and
    # pages cascade. Threads, runs and actuals cascade from the company.
    await session.execute(delete(Document).where(Document.company_id == company.id))
    await session.execute(delete(Company).where(Company.id == company.id))
    await session.commit()


async def _seed_call(
    session: AsyncSession,
    company: Company,
    label: str,
    chunks: list[DemoChunk],
    claims: list[DemoClaim],
    embedder: FakeEmbeddingProvider,
) -> bool:
    symbol = company.nse_symbol or SYMBOL
    file_hash = hashlib.sha256(f"demo:{symbol}:{label}".encode()).hexdigest()
    if await session.scalar(select(Document.id).where(Document.file_hash == file_hash)):
        return False
    fiscal_year, quarter = parse_period_label(label)
    pages = sorted({c.page for c in chunks})
    document = Document(
        id=uuid.uuid4(),
        company_id=company.id,
        doc_type=DocType.TRANSCRIPT,
        fiscal_year=fiscal_year,
        fiscal_quarter=quarter,
        period_label=label,
        original_filename=f"{symbol}_{label}_transcript.pdf",
        file_path=f"demo/{symbol}_{label}.pdf",
        file_hash=file_hash,
        page_count=max(pages),
        status=DocumentStatus.EXTRACTED,
    )
    session.add(document)
    await session.flush()  # chunks and claims reference it (no ORM relationship to order by)
    for page in pages:
        text = "\n".join(f"{c.speaker}:\n{c.text}" for c in chunks if c.page == page)
        session.add(
            DocumentPage(document_id=document.id, page_number=page, text=text, char_count=len(text))
        )
    rows = []
    for index, chunk in enumerate(chunks):
        row = Chunk(
            id=uuid.uuid4(),
            document_id=document.id,
            company_id=company.id,
            chunk_index=index,
            page_start=chunk.page,
            page_end=chunk.page,
            speaker=chunk.speaker,
            speaker_role=chunk.role,
            section=chunk.section,
            text=chunk.text,
            token_count=count_tokens(chunk.text),
            embedding=embedder.vector(chunk.text),
        )
        rows.append(row)
        session.add(row)
    await session.flush()
    for claim in claims:
        chunk = chunks[claim.chunk]
        if claim.quote not in chunk.text:
            raise AssertionError(f"Demo quote not in its chunk: {claim.quote!r}")
        session.add(
            Claim(
                company_id=company.id,
                document_id=document.id,
                chunk_id=rows[claim.chunk].id,
                metric_raw=claim.metric_raw,
                metric_normalized=claim.metric.value,
                claim_type=claim.claim_type,
                target_low=Decimal(claim.low) if claim.low else None,
                target_high=Decimal(claim.high) if claim.high else None,
                unit=claim.unit,
                direction=claim.direction,
                timeframe_raw=claim.timeframe_raw,
                target_fiscal_year=claim.target_fy,
                target_fiscal_quarter=claim.target_q,
                timeframe_is_range=claim.is_range,
                quote=claim.quote,
                speaker=chunk.speaker,
                page_number=chunk.page,
                extraction_confidence=0.9,
                model=MODEL,
                prompt_version="demo",
            )
        )
    await session.commit()
    return True


async def seed_demo(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, *, reset: bool = False
) -> uuid.UUID:
    """Seed (or refresh) Demo Industries and run link + verify. Returns the company id."""
    embedder = FakeEmbeddingProvider(settings.embedding_dim)
    async with sessionmaker() as session:
        company = await session.scalar(select(Company).where(Company.nse_symbol == SYMBOL))
        if company is not None and reset:
            await _delete_company(session, company)
            company = None
        if company is None:
            company = Company(name=NAME, nse_symbol=SYMBOL, sector="Industrials (fictional)")
            session.add(company)
            await session.commit()
            await session.refresh(company)
        for label, (chunks, claims) in CALLS.items():
            await _seed_call(session, company, label, chunks, claims, embedder)
        report = await import_actuals(session, company, ACTUALS_CSV.encode())
        if report.errors:
            raise AssertionError(f"Demo actuals rejected: {report.errors}")
        company_id = company.id

    llm = FakeLLMProvider(responder=demo_responder, default_model=MODEL)
    demo_settings = settings.model_copy(update={"verify_model": MODEL})
    run = await link_and_verify(sessionmaker, llm, embedder, demo_settings, company_id)
    if run.error_message:
        raise RuntimeError(f"Verification run failed: {run.error_message}")
    return company_id


async def _print_summary(
    sessionmaker: async_sessionmaker[AsyncSession], company_id: uuid.UUID
) -> None:
    async with sessionmaker() as session:
        card = await scorecard(session, company_id)
        threads = await list_threads(session, company_id)
        plant = next(t for t in threads if t.metric_normalized == M.PLANT_COMMISSIONING)
        detail = await thread_detail(session, plant.id)
    print("=== Threads ===")
    for t in threads:
        print(f"  [{t.status or '-':12}] {t.title}  ({t.first_seen_period}..{t.last_seen_period})")
    print("\n=== Scorecard ===")
    print(json.dumps(card.model_dump(mode="json"), indent=2, ensure_ascii=False))
    print("\n=== Thread detail: delayed plant ===")
    print(json.dumps(detail.model_dump(mode="json"), indent=2, ensure_ascii=False))


async def _main(reset: bool) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    try:
        company_id = await seed_demo(sessionmaker, settings, reset=reset)
        await _print_summary(sessionmaker, company_id)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--reset", action="store_true", help="delete Demo Industries first")
    args = parser.parse_args(argv)
    asyncio.run(_main(args.reset))


if __name__ == "__main__":
    main(sys.argv[1:])
