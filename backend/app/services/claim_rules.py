"""Deterministic corrections to a claim's metric and type. The model's labels are not
trusted where the quote says otherwise.

1. EBIT is not EBITDA. A claim whose metric (as said) names EBIT or "operating
   margin" (operating profit after depreciation, i.e. EBIT) but not EBITDA is
   ``ebit_margin`` (or ``ebit`` for the amount), whatever the model chose among
   ebitda / ebitda_margin / other.
2. Impacts are not targets. A figure in basis points described as an impact, a
   headwind/tailwind, a charge, a one-off or a contribution ("50 bps impact from the
   labour code", "62 bps inorganic contribution") is claim type ``impact``: it sizes an
   effect on the metric and is never verified as the metric's target.
3. Company level (stored claims only): when a company states its margins as EBIT and
   never as EBITDA, a margin claim the model filed as ``ebitda_margin`` without naming
   either ("margin guidance of 17.5% to 18.5%") is ``ebit_margin`` too.
4. A revenue sub-line is not total revenue. A revenue / revenue-growth claim whose
   metric (as said) starts with a sub-line ("third-party revenue", "product revenue",
   "segment revenue", "software revenue") is filed under that scope
   (``segment:<name>``), or as metric ``other`` when the line has no segment name
   (third-party, segment, vertical). "Services revenue growth" gets scope ``services``.
5. Past results are not promises (``is_past_result``, extraction only). A claim whose
   resolved target period is the call's own quarter or earlier is dropped unless its
   quote is clearly forward-looking ("will", "expect", "guidance", "by FY27"...). A
   numeric/directional claim with no period at all is dropped when its quote is in the
   past tense ("grew", "was") and has no forward-looking word.

Rules 1, 2 and 4 run on every extracted claim; rules 1-4 run on a company's stored claims
before linking (``reclassify_company_claims``), so claims from older prompt versions get
the same treatment.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Claim, ClaimType, ClaimUnit, MetricNormalized
from app.services.periods import end_ordinal
from app.services.scope import COMPANY, SERVICES, normalize_scope

logger = logging.getLogger(__name__)

M = MetricNormalized

_EBIT_RE = re.compile(r"\bEBIT\b|\boperating\s+(?:profit\s+)?margins?\b", re.IGNORECASE)
_EBITDA_RE = re.compile(r"\bEBITDA\b", re.IGNORECASE)
_MARGIN_RE = re.compile(r"\bmargins?\b", re.IGNORECASE)
_IMPACT_RE = re.compile(
    r"\b(?:impact\w*|headwinds?|tailwinds?|charges?|one[-\s]?(?:time|off)|exceptional"
    r"|contribut\w*|drag)\b",
    re.IGNORECASE,
)
# Metrics a mislabelled EBIT claim may arrive as.
_EBIT_CANDIDATES = frozenset({M.EBITDA, M.EBITDA_MARGIN, M.OTHER})


# A negative percent "due to" / "because of" something is the size of an effect.
_CAUSE_RE = re.compile(r"\b(?:due\s+to|because\s+of|owing\s+to|on\s+account\s+of)\b", re.I)
# Rule 4: revenue from acquisitions is not the company's total revenue.
_ACQUIRED_RE = re.compile(r"\b(?:acquisitions?|acquired|inorganic|contribution)\b", re.I)
# Rule 4: a sub-line named at the start of the metric as said ("third-party revenue",
# "our product revenue growth", "HCLTech services revenue").
_SUBLINE_RE = re.compile(
    r"^(?:the\s+|our\s+|total\s+)?(?:[a-z]+\s*tech\s+|it\s+)?"
    r"(?P<name>third[\s-]party|services|products?|software|platforms?|segments?|verticals?"
    r"|digital|cloud|engineering|ai|gen[\s-]?ai)"
    r"\s+(?:revenues?|sales|growth|business|top[\s-]?line)\b",
    re.IGNORECASE,
)
# Sub-lines with no segment name of their own: filed as metric "other".
_UNNAMED_SUBLINES = frozenset({"third party", "segment", "segments", "vertical", "verticals"})
_REVENUE_METRICS = frozenset({M.REVENUE, M.REVENUE_GROWTH})

# Rule 5.
_FORWARD_RE = re.compile(
    r"\b(?:will|\w+'ll|shall|going\s+to|expect\w*|anticipat\w*|guid(?:e|es|ing|ance)"
    r"|outlook|going\s+forward|intend\w*|aim(?:s|ing)?\s+to|plan(?:s|ning)?\s+to"
    r"|forecast\w*|should|look(?:s|ing)?\s+to)\b"
    r"|\bby\s+(?:the\s+)?(?:end\s+of\s+)?(?:the\s+)?(?:fy|q[1-4]|h[12]|fiscal|next|this\s+year"
    r"|year[\s-]*end|20\d\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
    re.IGNORECASE,
)
_PAST_RE = re.compile(
    r"\b(?:was|were|grew|increased|decreased|declined|rose|fell|stood|came\s+in|delivered"
    r"|reported|achieved|ended|clocked|posted|recorded|registered|closed|had|did|saw"
    r"|has\s+been|have\s+been|has\s+grown|have\s+grown|up|down)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Reclassified:
    metric: str | None
    claim_type: ClaimType
    notes: tuple[str, ...] = ()
    scope: str = COMPANY


def _names_ebit(text: str) -> bool:
    return bool(_EBIT_RE.search(text)) and not _EBITDA_RE.search(text)


def ebit_metric(metric: str | None, metric_raw: str, quote: str) -> str | None:
    """``ebit_margin`` / ``ebit`` when the claim is about EBIT but labelled otherwise."""
    if metric not in _EBIT_CANDIDATES:
        return None
    if _names_ebit(metric_raw):
        said = metric_raw
    elif not _EBITDA_RE.search(metric_raw) and metric != M.OTHER and _names_ebit(quote):
        said = quote
    else:
        return None
    if _MARGIN_RE.search(said) or metric == M.EBITDA_MARGIN:
        return M.EBIT_MARGIN.value
    return M.EBIT.value


def is_impact(
    claim_type: ClaimType,
    unit: str | None,
    metric_raw: str,
    quote: str,
    target_low: Decimal | float | None = None,
) -> bool:
    """A figure that sizes an effect rather than setting a target: basis points described
    as an impact/headwind/contribution, or a negative percent put down to a cause ("a half
    a percent reduction due to two clients")."""
    if claim_type not in (ClaimType.NUMERIC, ClaimType.DIRECTIONAL):
        return False
    text = f"{metric_raw} {quote}"
    if unit == ClaimUnit.BPS:
        return bool(_IMPACT_RE.search(text))
    return (
        unit == ClaimUnit.PERCENT
        and target_low is not None
        and target_low < 0
        and bool(_IMPACT_RE.search(text) or _CAUSE_RE.search(text))
    )


def revenue_line(
    metric: str | None, metric_raw: str, scope: str
) -> tuple[str | None, str, str | None]:
    """Rule 4: (metric, scope, note) for a revenue claim about a sub-line; unchanged
    (note ``None``) otherwise."""
    if metric not in _REVENUE_METRICS or scope != COMPANY:
        return metric, scope, None
    if _ACQUIRED_RE.search(metric_raw):
        return (
            M.OTHER.value,
            scope,
            f"metric {metric} -> other ({metric_raw!r} is acquired revenue, not total revenue)",
        )
    match = _SUBLINE_RE.match(" ".join(metric_raw.split()))
    if match is None:
        return metric, scope, None
    name = " ".join(match["name"].lower().replace("-", " ").split())
    if name == "services":
        return metric, SERVICES, f"scope company -> services ({metric_raw!r} is services)"
    if name in _UNNAMED_SUBLINES:
        return (
            M.OTHER.value,
            scope,
            f"metric {metric} -> other ({metric_raw!r} is a revenue sub-line, not total revenue)",
        )
    new_scope = normalize_scope(f"segment:{name}")
    return (
        metric,
        new_scope,
        f"scope company -> {new_scope} ({metric_raw!r} is a revenue sub-line, not total revenue)",
    )


def is_past_result(
    quote: str,
    claim_type: ClaimType,
    target_fiscal_year: int | None,
    target_fiscal_quarter: int | None,
    call_fiscal_year: int,
    call_fiscal_quarter: int | None,
) -> str | None:
    """Rule 5: why the claim is a reported result rather than a promise, or ``None``."""
    if _FORWARD_RE.search(quote.replace("\u2019", "'")):
        return None
    call_end = end_ordinal(call_fiscal_year, call_fiscal_quarter or 4)
    if target_fiscal_year is not None:
        target_end = end_ordinal(target_fiscal_year, target_fiscal_quarter)
        if target_end <= call_end:
            return (
                "target period is the call's own quarter or earlier and the quote is not "
                "forward-looking"
            )
        return None
    if claim_type in (ClaimType.NUMERIC, ClaimType.DIRECTIONAL) and _PAST_RE.search(quote):
        return "no target period and the quote reports a result in the past tense"
    return None


def reclassify(
    metric: str | None,
    metric_raw: str,
    quote: str,
    unit: str | None,
    claim_type: ClaimType,
    scope: str = COMPANY,
    target_low: Decimal | float | None = None,
) -> Reclassified:
    """Rules 1, 2 and 4 for one claim."""
    notes: list[str] = []
    ebit = ebit_metric(metric, metric_raw, quote)
    if ebit is not None:
        notes.append(f"metric {metric} -> {ebit} (the claim names EBIT, not EBITDA)")
        metric = ebit
    if is_impact(claim_type, unit, metric_raw, quote, target_low):
        notes.append(
            f"claim_type {claim_type.value} -> impact (the size of an effect, not a target)"
        )
        claim_type = ClaimType.IMPACT
    metric, scope, note = revenue_line(metric, metric_raw, scope)
    if note is not None:
        notes.append(note)
    return Reclassified(metric, claim_type, tuple(notes), scope)


@dataclass(frozen=True)
class ClaimChange:
    claim_id: uuid.UUID
    quote: str
    before: tuple[str | None, str, str]
    after: tuple[str | None, str, str]
    reason: str


def _states_ebit_not_ebitda(claims: list[Claim]) -> bool:
    texts = [f"{c.metric_raw} {c.quote}" for c in claims]
    return any(_EBIT_RE.search(t) for t in texts) and not any(_EBITDA_RE.search(t) for t in texts)


async def reclassify_company_claims(
    session: AsyncSession, company_id: uuid.UUID
) -> list[ClaimChange]:
    """Apply rules 1-3 to the company's stored claims. Flushes; the caller commits."""
    claims = list(await session.scalars(select(Claim).where(Claim.company_id == company_id)))
    ebit_company = _states_ebit_not_ebitda(claims)
    changes: list[ClaimChange] = []
    for claim in claims:
        before = (claim.metric_normalized, claim.claim_type.value, claim.scope)
        result = reclassify(
            claim.metric_normalized,
            claim.metric_raw,
            claim.quote,
            claim.unit,
            claim.claim_type,
            claim.scope or COMPANY,
            claim.target_low,
        )
        notes = list(result.notes)
        metric = result.metric
        if (
            ebit_company
            and metric == M.EBITDA_MARGIN
            and not _EBITDA_RE.search(f"{claim.metric_raw} {claim.quote}")
        ):
            metric = M.EBIT_MARGIN.value
            notes.append(
                "metric ebitda_margin -> ebit_margin (the company states its margins as EBIT)"
            )
        if not notes:
            continue
        claim.metric_normalized = metric
        claim.claim_type = result.claim_type
        claim.scope = result.scope
        reason = "; ".join(notes)
        existing = claim.validation_notes or ""
        if reason not in existing:
            claim.validation_notes = f"{existing}; {reason}" if existing else reason
        changes.append(
            ClaimChange(
                claim.id,
                claim.quote,
                before,
                (metric, result.claim_type.value, result.scope),
                reason,
            )
        )
    if changes:
        await session.flush()
        logger.info("Company %s: reclassified %d claim(s)", company_id, len(changes))
    return changes
