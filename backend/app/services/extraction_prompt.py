"""Prompt ``extract-v2``: several management chunks per request.

- ``SYSTEM_PROMPT`` is a constant: the same text starts every request, for every
  company and call, so Groq's prompt cache can reuse it (cache hits need an
  exact prefix). Everything call-specific goes in the user message.
- The user message has a CALL line (company, period and what "this year" /
  "next year" mean on this call), the analyst questions as ``Q1``, ``Q2``, ...
  (context only, each sent once even when several chunks answer it) and the
  chunks as ``[C1]``, ``[C2]``, .... Each claim names the chunk it quotes.

Bump ``PROMPT_VERSION`` whenever the wording or examples change; every claim and
extraction run stores the version that produced it. ``extract-v1`` (one chunk
per request) is kept unchanged in ``extraction_prompt_v1.py``.
"""

import json
from dataclasses import dataclass

from app.models import ChunkSection
from app.services.fiscal import period_label
from app.services.timeframe import CallAnchor

PROMPT_VERSION = "extract-v2"

_METRICS = (
    "revenue, revenue_growth, ebitda, ebitda_margin, gross_margin, pat, pat_margin, capex, "
    "capacity, capacity_utilization, volume_growth, debt, net_debt, order_book, market_share, "
    "store_count, product_launch, plant_commissioning, dividend_payout, cost_reduction, other"
)
_UNITS = "percent, inr_crore, inr_lakh, inr_bn, usd_mn, units, mw, tonnes, bps, x, count, null"

INSTRUCTIONS = f"""\
You extract management's forward-looking claims from Indian earnings-call transcript chunks.

Input: a CALL line, optional analyst questions Q1, Q2... (context only) and management \
chunks [C1], [C2]...
Indian FY runs Apr-Mar and is named by the year it ends (FY27 = Apr 2026-Mar 2027); \
Q1 Apr-Jun, Q2 Jul-Sep, Q3 Oct-Dec, Q4 Jan-Mar. Resolve relative timeframes with the CALL line.

A claim is a specific statement by management about the company's OWN future performance or \
actions (guidance, target, plan, commitment) that can later be checked: a number, date, \
direction or concrete action.
Not claims: past or current results ("margin was 18.2%"); anything in a Q block; industry or \
macro forecasts; generic optimism; targets mentioned but not stated ("committed to our FY27 \
targets"); safe-harbour disclaimers.

Return {{"claims": [...]}} with one object per claim, [] if none (normal for thanks, small \
talk and past results). Fields:
- chunk_id: the chunk the quote is from, e.g. "C2".
- quote: EXACT words from that chunk: the sentence or clause that states the claim. Never \
paraphrase, stitch sentences or quote a Q block.
- metric_raw: the metric as said.
- metric_normalized: {_METRICS}.
- claim_type: numeric (has a target number), directional (direction, no number) or \
qualitative (an action or event, e.g. launch, commissioning).
- target_low, target_high: numbers as stated; a single target sets both; null if none. Never \
compute or convert, except lakh/crore counts of units or tonnes in full (1.2 lakh -> 120000).
- unit: {_UNITS}. Rs crore -> inr_crore; % growth or margin -> percent; basis points -> bps; \
"2x" -> x.
- direction: increase, decrease, maintain or none.
- timeframe_raw: the timeframe words as said ("by FY27", "H2", "this year"), else null.
- target_fiscal_year (e.g. 2027), target_fiscal_quarter (1-4): resolved timeframe_raw; null \
if none or open-ended ("medium term").
- confidence: 0-1, how sure you are this is a genuine, specific management claim.
Read every sentence of every chunk: one chunk can hold many claims. Several targets in one \
sentence -> one claim each (same quote is fine). Reiterated targets still count. Never invent \
numbers, dates or quotes.
Write the JSON on a single line with no indentation or spaces between tokens."""


@dataclass(frozen=True)
class ExampleChunk:
    speaker: str
    section: str
    text: str
    question: str | None = None


@dataclass(frozen=True)
class Example:
    call: str
    chunks: list[ExampleChunk]
    # Each claim lists only its non-null, non-default fields; see _render_claim.
    claims: list[dict[str, object]]


# Three examples, chosen for what they teach: skip a past result next to a target;
# several claims from one sentence (and lakh -> full number); in batch form, a no-claim
# chunk next to a directional claim and a no-number commitment (net cash: null targets).
EXAMPLES: list[Example] = [
    Example(
        call="Q2FY26",
        chunks=[
            ExampleChunk(
                "CFO",
                "Q&A",
                "This quarter EBITDA margin was 18.2%. For FY27 we are targeting an EBITDA "
                "margin of 19% to 20%.",
                question="What is the margin outlook for next year?",
            )
        ],
        claims=[
            {
                "chunk_id": "C1",
                "quote": "For FY27 we are targeting an EBITDA margin of 19% to 20%.",
                "metric_raw": "EBITDA margin",
                "metric_normalized": "ebitda_margin",
                "claim_type": "numeric",
                "target_low": 19,
                "target_high": 20,
                "unit": "percent",
                "direction": "increase",
                "timeframe_raw": "FY27",
                "target_fiscal_year": 2027,
                "confidence": 0.95,
            }
        ],
    ),
    Example(
        call="Q2FY26",
        chunks=[
            ExampleChunk(
                "MD",
                "Prepared remarks",
                "We expect to commission the Dahej plant by Q3 of next year, taking capacity "
                "to 1.2 lakh tonnes.",
            )
        ],
        claims=[
            {
                "chunk_id": "C1",
                "quote": "We expect to commission the Dahej plant by Q3 of next year, taking "
                "capacity to 1.2 lakh tonnes.",
                "metric_raw": "Dahej plant commissioning",
                "metric_normalized": "plant_commissioning",
                "claim_type": "qualitative",
                "timeframe_raw": "by Q3 of next year",
                "target_fiscal_year": 2027,
                "target_fiscal_quarter": 3,
                "confidence": 0.9,
            },
            {
                "chunk_id": "C1",
                "quote": "We expect to commission the Dahej plant by Q3 of next year, taking "
                "capacity to 1.2 lakh tonnes.",
                "metric_raw": "capacity",
                "metric_normalized": "capacity",
                "claim_type": "numeric",
                "target_low": 120000,
                "target_high": 120000,
                "unit": "tonnes",
                "direction": "increase",
                "timeframe_raw": "by Q3 of next year",
                "target_fiscal_year": 2027,
                "target_fiscal_quarter": 3,
                "confidence": 0.85,
            },
        ],
    ),
    Example(
        call="Q3FY26",
        chunks=[
            ExampleChunk(
                "Chairman",
                "Prepared remarks",
                "Revenue grew 12% this quarter. The industry may grow 7% next year, and we "
                "are very confident about the future.",
            ),
            ExampleChunk(
                "CEO",
                "Prepared remarks",
                "We expect our volumes to grow faster than the industry going forward. "
                "Debt-wise, we will remain net cash.",
            ),
        ],
        claims=[
            {
                "chunk_id": "C2",
                "quote": "We expect our volumes to grow faster than the industry going forward.",
                "metric_raw": "volumes",
                "metric_normalized": "volume_growth",
                "claim_type": "directional",
                "direction": "increase",
                "timeframe_raw": "going forward",
                "confidence": 0.8,
            },
            {
                "chunk_id": "C2",
                "quote": "Debt-wise, we will remain net cash.",
                "metric_raw": "net cash position",
                "metric_normalized": "net_debt",
                "claim_type": "qualitative",
                "direction": "maintain",
                "confidence": 0.75,
            },
        ],
    ),
]


def _render_example(index: int, example: Example) -> str:
    lines = [f"Example {index} (CALL {example.call})"]
    questions = [c.question for c in example.chunks if c.question]
    for q, question in enumerate(questions, start=1):
        lines.append(f"Q{q}: {question}")
    q = 0
    for c, chunk in enumerate(example.chunks, start=1):
        answers = ""
        if chunk.question:
            q += 1
            answers = f", answers Q{q}"
        lines.append(f"[C{c}] {chunk.speaker}, {chunk.section}{answers}: {chunk.text}")
    lines.append(
        "Output: "
        + json.dumps({"claims": example.claims}, ensure_ascii=False, separators=(",", ":"))
    )
    return "\n".join(lines)


def examples_text() -> str:
    return "\n\n".join(_render_example(i, e) for i, e in enumerate(EXAMPLES, start=1))


SYSTEM_PROMPT = f"""{INSTRUCTIONS}

Examples (null fields and direction "none" left out to save space; always output every field):

{examples_text()}
"""


# ------------------------------------------------------------- user message


@dataclass(frozen=True)
class PromptChunk:
    chunk_id: str
    text: str
    speaker: str | None
    section: ChunkSection
    # Index into the request's questions, or None.
    question: int | None = None


@dataclass(frozen=True)
class PromptQuestion:
    text: str
    speaker: str | None


def call_line(company: str, call_fiscal_year: int, call_fiscal_quarter: int | None) -> str:
    anchor = CallAnchor(call_fiscal_year, call_fiscal_quarter)
    this_year = anchor.current_year
    next_q_year, next_q = anchor.next_quarter()
    return (
        f"CALL: {company}, {period_label(call_fiscal_year, call_fiscal_quarter)} results call. "
        f'"this year"/"current year"/"this fiscal" = {period_label(this_year)} ({this_year}); '
        f'"next year" = {period_label(this_year + 1)} ({this_year + 1}); '
        f'"next quarter" = {period_label(next_q_year, next_q)}.'
    )


def build_user_prompt(
    *,
    company: str,
    call_fiscal_year: int,
    call_fiscal_quarter: int | None,
    chunks: list[PromptChunk],
    questions: list[PromptQuestion],
) -> str:
    parts = [call_line(company, call_fiscal_year, call_fiscal_quarter)]
    for q, question in enumerate(questions, start=1):
        parts.append(
            f"Q{q} ({question.speaker or 'Analyst'}, analyst; context only):\n{question.text}"
        )
    for chunk in chunks:
        section = "Q&A" if chunk.section is ChunkSection.QNA else "Prepared remarks"
        answers = f", answers Q{chunk.question + 1}" if chunk.question is not None else ""
        parts.append(
            f"[{chunk.chunk_id}] {chunk.speaker or 'Management'}, {section}{answers}:\n{chunk.text}"
        )
    return "\n\n".join(parts)
