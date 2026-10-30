"""Versioned prompt for extracting management claims from one transcript chunk.

Bump ``PROMPT_VERSION`` whenever the wording or examples change; every claim and
extraction run stores the version that produced it.
"""

import json
from dataclasses import dataclass

from app.models import ChunkSection
from app.services.fiscal import period_label
from app.services.timeframe import CallAnchor

PROMPT_VERSION = "extract-v1"

_METRICS = (
    "revenue, revenue_growth, ebitda, ebitda_margin, gross_margin, pat, pat_margin, capex, "
    "capacity, capacity_utilization, volume_growth, debt, net_debt, order_book, market_share, "
    "store_count, product_launch, plant_commissioning, dividend_payout, cost_reduction, other"
)
_UNITS = "percent, inr_crore, inr_lakh, inr_bn, usd_mn, units, mw, tonnes, bps, x, count, or null"


@dataclass(frozen=True)
class FewShot:
    call: str
    speaker: str
    section: str
    text: str
    claims: list[dict[str, object]]
    context: str | None = None


def _claim(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "quote": "",
        "metric_raw": "",
        "metric_normalized": "other",
        "claim_type": "qualitative",
        "target_low": None,
        "target_high": None,
        "unit": None,
        "direction": "none",
        "timeframe_raw": None,
        "target_fiscal_year": None,
        "target_fiscal_quarter": None,
        "confidence": 0.9,
    }
    base.update(fields)
    return base


FEW_SHOTS: list[FewShot] = [
    # 1. Numeric margin target (with a past result in the same breath that must be skipped).
    FewShot(
        call="Q2FY26",
        speaker="CFO",
        section="Q&A",
        context="Analyst: What is the margin outlook for next year, given the 18.2% this quarter?",
        text="Sure. This quarter EBITDA margin was 18.2%, helped by lower steel prices. For "
        "FY27 we are targeting an EBITDA margin in the range of 19% to 20%, as the new "
        "capacity gets absorbed.",
        claims=[
            _claim(
                quote="For FY27 we are targeting an EBITDA margin in the range of 19% to 20%, as "
                "the new capacity gets absorbed.",
                metric_raw="EBITDA margin",
                metric_normalized="ebitda_margin",
                claim_type="numeric",
                target_low=19,
                target_high=20,
                unit="percent",
                direction="increase",
                timeframe_raw="FY27",
                target_fiscal_year=2027,
                confidence=0.95,
            )
        ],
    ),
    # 2. Capex amount and a commissioning date.
    FewShot(
        call="Q2FY26",
        speaker="Managing Director",
        section="Prepared remarks",
        text="On the expansion, the board has approved a capex of Rs. 450 crore for the new "
        "Dahej plant. Civil work is on track and we expect to commission the plant by Q3 of "
        "next year, which will take our capacity to 1.2 lakh tonnes per annum.",
        claims=[
            _claim(
                quote="the board has approved a capex of Rs. 450 crore for the new Dahej plant.",
                metric_raw="capex for the new Dahej plant",
                metric_normalized="capex",
                claim_type="numeric",
                target_low=450,
                target_high=450,
                unit="inr_crore",
                direction="none",
                confidence=0.85,
            ),
            _claim(
                quote="we expect to commission the plant by Q3 of next year, which will take our "
                "capacity to 1.2 lakh tonnes per annum.",
                metric_raw="Dahej plant commissioning",
                metric_normalized="plant_commissioning",
                claim_type="qualitative",
                direction="none",
                timeframe_raw="by Q3 of next year",
                target_fiscal_year=2027,
                target_fiscal_quarter=3,
                confidence=0.9,
            ),
            _claim(
                quote="we expect to commission the plant by Q3 of next year, which will take our "
                "capacity to 1.2 lakh tonnes per annum.",
                metric_raw="capacity",
                metric_normalized="capacity",
                claim_type="numeric",
                target_low=120000,
                target_high=120000,
                unit="tonnes",
                direction="increase",
                timeframe_raw="by Q3 of next year",
                target_fiscal_year=2027,
                target_fiscal_quarter=3,
                confidence=0.85,
            ),
        ],
    ),
    # 3. Directional statement, no number.
    FewShot(
        call="Q3FY26",
        speaker="CEO",
        section="Q&A",
        context="Analyst: The industry grew about 6% this quarter. How do you see your own "
        "volumes?",
        text="Look, the industry may grow 6% to 7% next year. We expect our volumes to grow "
        "faster than the industry going forward, given the new launches and our rural "
        "distribution push.",
        claims=[
            _claim(
                quote="We expect our volumes to grow faster than the industry going forward, "
                "given the new launches and our rural distribution push.",
                metric_raw="volumes",
                metric_normalized="volume_growth",
                claim_type="directional",
                direction="increase",
                timeframe_raw="going forward",
                confidence=0.8,
            )
        ],
    ),
    # 4. Reiterated guidance.
    FewShot(
        call="Q2FY26",
        speaker="CFO",
        section="Prepared remarks",
        text="We are reiterating our guidance of 12% to 14% revenue growth for this year. "
        "Debt-wise, we will remain net cash.",
        claims=[
            _claim(
                quote="We are reiterating our guidance of 12% to 14% revenue growth for this year.",
                metric_raw="revenue growth",
                metric_normalized="revenue_growth",
                claim_type="numeric",
                target_low=12,
                target_high=14,
                unit="percent",
                direction="increase",
                timeframe_raw="this year",
                target_fiscal_year=2026,
                confidence=0.95,
            ),
            _claim(
                quote="Debt-wise, we will remain net cash.",
                metric_raw="net cash position",
                metric_normalized="net_debt",
                claim_type="qualitative",
                direction="maintain",
                confidence=0.75,
            ),
        ],
    ),
    # 5. No claims: past results, generic optimism, macro view.
    FewShot(
        call="Q2FY26",
        speaker="Chairman",
        section="Prepared remarks",
        text="Revenue grew 12% this quarter to Rs. 2,340 crore and PAT was up 9%. The economy "
        "is expected to grow over 6.5% this year. We are very confident about the future and "
        "remain committed to creating value for all stakeholders.",
        claims=[],
    ),
]


def _render_example(index: int, shot: FewShot) -> str:
    lines = [f"### Example {index} (call: {shot.call}; {shot.speaker}, {shot.section})"]
    if shot.context:
        lines.append(f"CONTEXT (analyst question, do not extract from it):\n{shot.context}")
    lines.append(f"CHUNK:\n{shot.text}")
    lines.append("OUTPUT:\n" + json.dumps({"claims": shot.claims}, ensure_ascii=False))
    return "\n".join(lines)


def build_system_prompt(
    company: str, call_fiscal_year: int, call_fiscal_quarter: int | None
) -> str:
    anchor = CallAnchor(call_fiscal_year, call_fiscal_quarter)
    label = period_label(call_fiscal_year, call_fiscal_quarter)
    this_year = anchor.current_year
    next_q_year, next_q = anchor.next_quarter()
    examples = "\n\n".join(_render_example(i, shot) for i, shot in enumerate(FEW_SHOTS, start=1))
    return f"""You extract management's forward-looking claims from an Indian listed company's \
earnings call transcript, one chunk at a time.

## This call
- Company: {company}
- Call: {label} results call
- Indian fiscal years run April to March and are named after the year they end in \
(FY27 = Apr 2026 - Mar 2027). Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar.
- A results call is held AFTER the quarter it reports on, so on this call:
  "this year" / "current year" / "this fiscal" = {period_label(this_year)} \
(target_fiscal_year {this_year}); "next year" = {period_label(this_year + 1)} \
({this_year + 1}); "next quarter" = {period_label(next_q_year, next_q)}.

## What is a claim
A specific forward-looking statement made by management about the company's OWN future \
performance or actions: guidance, targets, plans, commitments and promises that can later \
be checked against what actually happened (a number, a date, a direction or a concrete action).

NOT claims (return nothing for these):
- Reported past or current results: "revenue grew 12% this quarter", "margin was 18.2%".
- Anything said by an analyst or the moderator, including the CONTEXT block.
- Industry, sector or macro forecasts not tied to the company: "the industry should grow 7%".
- Generic optimism with nothing checkable: "we are very confident about the future".
- Vague references to targets without stating them: "we remain committed to our FY27 \
targets" (extract the target itself where it is actually stated).
- Safe-harbour / forward-looking-statement disclaimers.

## Fields (one object per claim)
- quote: copy the EXACT words from the CHUNK, verbatim, one or two sentences. Never \
paraphrase, never stitch sentences together, never quote the CONTEXT.
- metric_raw: the metric as management said it.
- metric_normalized: one of {_METRICS}.
- claim_type: numeric (has a target number), directional (a direction without a number), \
or qualitative (an action or event, e.g. a launch or commissioning).
- target_low / target_high: numbers exactly as stated in the chunk; a single target sets \
both; null when no number is stated. Never compute, convert or invent numbers, except that \
"lakh"/"crore" quantities of units or tonnes may be written out in full (1.2 lakh -> 120000).
- unit: one of {_UNITS}. Rupee amounts in crore -> inr_crore. Percent growth or margin -> \
percent. Basis points -> bps. Multiples ("2x") -> x.
- direction: increase, decrease, maintain or none.
- timeframe_raw: the timeframe words as said ("by FY27", "next two years", "H2", "this \
year"); null if none is stated.
- target_fiscal_year (e.g. 2027) and target_fiscal_quarter (1-4): your best resolution of \
timeframe_raw using the rules above; null if not stated or open-ended ("medium term").
- confidence: 0-1, how sure you are that this is a genuine, specific management claim.

## Rules
- If one sentence contains several distinct targets (e.g. revenue growth AND margin), emit \
one claim per target; they may share the same quote.
- If management repeats or reiterates a target, still extract it.
- Return {{"claims": []}} when there are no claims. That is the right answer for most \
small-talk, thank-you and past-results chunks.
- Never invent numbers, dates or quotes.

## Examples
{examples}
"""


def build_user_prompt(
    *,
    chunk_text: str,
    speaker: str | None,
    section: ChunkSection,
    context_text: str | None = None,
    context_speaker: str | None = None,
) -> str:
    section_label = "Q&A" if section is ChunkSection.QNA else "Prepared remarks"
    parts = []
    if context_text:
        who = context_speaker or "Analyst"
        parts.append(
            f"CONTEXT (question from {who}, analyst; for understanding only, do not extract "
            f"claims from it):\n{context_text}"
        )
    parts.append(f"CHUNK ({speaker or 'Management'}, management, {section_label}):\n{chunk_text}")
    parts.append('Return {"claims": [...]} for the CHUNK only.')
    return "\n\n".join(parts)
