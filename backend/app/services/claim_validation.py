"""Checks applied to every claim the LLM returns; the model's output is never trusted.

1. Quote: must appear in the chunk (whitespace, quotes and dashes normalised;
   fuzzy match >= ``QUOTE_MATCH_RATIO``, and a fuzzy match may not change any
   number), else the claim is rejected. The stored
   quote is the matching span of the source, so it is always verbatim.
2. Numbers: target_low/target_high must appear in the quote or chunk, allowing
   formatting variants ("18%" / "18 per cent", "1,200" / "1200", "1.2k"). If not,
   the targets are nulled, the claim is downgraded to directional and its
   confidence lowered.
3. Timeframe: resolved in code (``services/timeframe.py``) when a rule applies;
   otherwise the model's fiscal year/quarter is kept only if consistent with
   ``timeframe_raw``.
"""

import difflib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

from app.models import ClaimType, ClaimUnit, Direction, MetricNormalized
from app.services.claim_schema import ExtractedClaim
from app.services.timeframe import model_timeframe_is_consistent, resolve_timeframe

QUOTE_MATCH_RATIO = 0.9
DEDUPE_QUOTE_RATIO = 0.85
NUMBER_MISMATCH_CONFIDENCE_FACTOR = 0.6
# Window sizes tried around the quote's word count when fuzzy matching.
_WINDOW_SLACK_WORDS = 3

_CHAR_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2033": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)
_EDGE_PUNCT = "\"'()[]{},;:.!?\u2026"


def normalize_text(text: str) -> str:
    """Lower-case, NFKC, straight quotes, plain hyphens, single spaces."""
    text = unicodedata.normalize("NFKC", text).translate(_CHAR_MAP).lower()
    text = re.sub(r"\s*-\s*", "-", text)  # "15 - 18" == "15-18"
    return " ".join(text.split())


def _norm_token(token: str) -> str:
    return normalize_text(token).strip(_EDGE_PUNCT)


# -------------------------------------------------------------- quote check


@dataclass(frozen=True)
class QuoteMatch:
    ratio: float
    # The matching span of the source text, whitespace collapsed.
    source_span: str


def find_quote(quote: str, text: str) -> QuoteMatch | None:
    """Best match of ``quote`` in ``text`` (``None`` if below ``QUOTE_MATCH_RATIO``)."""
    if not quote.strip():
        return None
    norm_quote = normalize_text(quote).strip(_EDGE_PUNCT + " ")
    tokens = [(m.start(), m.end(), _norm_token(m.group())) for m in re.finditer(r"\S+", text)]
    tokens = [t for t in tokens if t[2]]
    quote_words = [w for w in (_norm_token(w) for w in norm_quote.split()) if w]
    if not tokens or not quote_words:
        return None

    target = " ".join(quote_words)
    best: tuple[float, int, int] = (0.0, 0, 0)
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(target)
    n = len(quote_words)
    sizes = range(max(1, n - _WINDOW_SLACK_WORDS), n + _WINDOW_SLACK_WORDS + 1)
    for size in sizes:
        for i in range(0, max(1, len(tokens) - size + 1)):
            window = tokens[i : i + size]
            candidate = " ".join(t[2] for t in window)
            if candidate == target:
                return QuoteMatch(1.0, _span(text, window[0][0], window[-1][1]))
            matcher.set_seq1(candidate)
            if matcher.real_quick_ratio() < max(best[0], QUOTE_MATCH_RATIO):
                continue
            if matcher.quick_ratio() < max(best[0], QUOTE_MATCH_RATIO):
                continue
            ratio = matcher.ratio()
            if ratio > best[0]:
                best = (ratio, window[0][0], window[-1][1])
    if best[0] < QUOTE_MATCH_RATIO:
        return None
    span = _span(text, best[1], best[2])
    # Fuzzy matching tolerates wording slips, never changed figures ("22%" vs "19%").
    span_numbers = numbers_in(span)
    if not all(number_in_source(n, span_numbers) for n in numbers_in(quote)):
        return None
    return QuoteMatch(round(best[0], 3), span)


def _span(text: str, start: int, end: int) -> str:
    return " ".join(text[start:end].split())


# ------------------------------------------------------------- number check

_MULTIPLIERS = {
    "k": 1e3,
    "thousand": 1e3,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "mn": 1e6,
    "million": 1e6,
    "millions": 1e6,
    "cr": 1e7,
    "crore": 1e7,
    "crores": 1e7,
    "bn": 1e9,
    "billion": 1e9,
    "billions": 1e9,
}
_WORD_NUMBERS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "twenty-five": 25,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "hundred": 100,
    "double": 2,
    "doubling": 2,
    "doubled": 2,
    "twice": 2,
    "triple": 3,
    "tripling": 3,
    "tripled": 3,
    "half": 0.5,
}
# Not preceded by a letter/digit ("FY27" is a year, not a 27) or by "1." (mid-decimal).
_NUMBER_RE = re.compile(
    r"(?<![^\W\d_])(?<!\d)(?<!\d\.)"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*(?P<mult>k|thousand|lakhs?|lacs?|mn|millions?|cr|crores?|bn|billions?)\b)?",
    re.IGNORECASE,
)
_WORD_NUMBER_RE = re.compile(
    r"\b(" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)


def numbers_in(text: str) -> set[float]:
    """Every number in ``text``, plus scaled variants ("1.2k" -> 1.2 and 1200)."""
    found: set[float] = set()
    normalized = normalize_text(text)
    for match in _NUMBER_RE.finditer(normalized):
        value = float(match["num"].replace(",", ""))
        found.add(value)
        if match["mult"]:
            found.add(value * _MULTIPLIERS[match["mult"].lower()])
    for match in _WORD_NUMBER_RE.finditer(normalized):
        found.add(float(_WORD_NUMBERS[match.group(1).lower()]))
    return found


def number_in_source(value: float, source_numbers: set[float]) -> bool:
    return any(
        math.isclose(value, candidate, rel_tol=1e-9, abs_tol=1e-9)
        or math.isclose(abs(value), candidate, rel_tol=1e-9, abs_tol=1e-9)
        for candidate in source_numbers
    )


# ----------------------------------------------------------------- results


@dataclass
class ValidatedClaim:
    quote: str
    metric_raw: str
    metric_normalized: MetricNormalized
    claim_type: ClaimType
    target_low: Decimal | None
    target_high: Decimal | None
    unit: ClaimUnit | None
    direction: Direction
    timeframe_raw: str | None
    target_fiscal_year: int | None
    target_fiscal_quarter: int | None
    timeframe_is_range: bool
    confidence: float
    notes: list[str] = field(default_factory=list)


@dataclass
class ValidationOutcome:
    accepted: list[ValidatedClaim] = field(default_factory=list)
    rejected_hallucinated_quote: int = 0
    numbers_downgraded: int = 0

    @property
    def rejected(self) -> int:
        return self.rejected_hallucinated_quote


def _decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    # Via repr so 19.4 stays 19.4 rather than 19.39999...; 20.0 -> 20 (not 2E+1).
    number = Decimal(repr(value))
    integral = number.to_integral_value()
    return integral if number == integral else number.normalize()


def validate_claim(
    claim: ExtractedClaim,
    chunk_text: str,
    call_fiscal_year: int,
    call_fiscal_quarter: int | None,
) -> ValidatedClaim | None:
    """Return the cleaned claim, or ``None`` if the quote is not in the chunk."""
    match = find_quote(claim.quote, chunk_text)
    if match is None:
        return None
    notes: list[str] = []
    if match.ratio < 1.0:
        notes.append(f"quote fuzzy-matched to source (ratio {match.ratio:.2f})")

    claim_type = claim.claim_type
    confidence = claim.confidence
    low, high, unit = claim.target_low, claim.target_high, claim.unit

    # Numbers must come from the source: quote first, then the rest of the chunk.
    if low is not None or high is not None:
        source_numbers = numbers_in(match.source_span) | numbers_in(chunk_text)
        missing = [
            v for v in {low, high} if v is not None and not number_in_source(v, source_numbers)
        ]
        if missing:
            notes.append(
                "numbers not found in source ("
                + ", ".join(f"{v:g}" for v in sorted(missing))
                + "): targets removed, downgraded to directional"
            )
            low = high = None
            unit = None
            if claim_type is ClaimType.NUMERIC:
                claim_type = ClaimType.DIRECTIONAL
            confidence = round(confidence * NUMBER_MISMATCH_CONFIDENCE_FACTOR, 3)
        elif claim_type is not ClaimType.NUMERIC:
            notes.append(f"claim_type {claim_type.value} -> numeric (has a target)")
            claim_type = ClaimType.NUMERIC
    if claim_type is ClaimType.NUMERIC and low is None:
        notes.append("numeric claim without a target -> directional")
        claim_type = ClaimType.DIRECTIONAL

    # Timeframe: deterministic rules first, the model's guess only if consistent.
    is_range = False
    resolved = resolve_timeframe(claim.timeframe_raw, call_fiscal_year, call_fiscal_quarter)
    if resolved is not None:
        fiscal_year, fiscal_quarter, is_range = (
            resolved.fiscal_year,
            resolved.fiscal_quarter,
            resolved.is_range,
        )
        if (fiscal_year, fiscal_quarter) != (claim.target_fiscal_year, claim.target_fiscal_quarter):
            notes.append(
                f"timeframe resolved in code ({resolved.rule}): "
                f"{_period(claim.target_fiscal_year, claim.target_fiscal_quarter)} -> "
                f"{_period(fiscal_year, fiscal_quarter)}"
            )
    else:
        fiscal_year, fiscal_quarter = model_timeframe_is_consistent(
            claim.timeframe_raw,
            claim.target_fiscal_year,
            claim.target_fiscal_quarter,
            call_fiscal_year,
            call_fiscal_quarter,
        )
        if (fiscal_year, fiscal_quarter) != (claim.target_fiscal_year, claim.target_fiscal_quarter):
            notes.append(
                "model timeframe "
                f"{_period(claim.target_fiscal_year, claim.target_fiscal_quarter)} not supported "
                f"by {claim.timeframe_raw!r} -> {_period(fiscal_year, fiscal_quarter)}"
            )

    return ValidatedClaim(
        quote=match.source_span,
        metric_raw=claim.metric_raw,
        metric_normalized=claim.metric_normalized,
        claim_type=claim_type,
        target_low=_decimal(low),
        target_high=_decimal(high),
        unit=unit,
        direction=claim.direction,
        timeframe_raw=claim.timeframe_raw,
        target_fiscal_year=fiscal_year,
        target_fiscal_quarter=fiscal_quarter,
        timeframe_is_range=is_range,
        confidence=max(0.0, min(1.0, confidence)),
        notes=notes,
    )


def _period(fiscal_year: int | None, fiscal_quarter: int | None) -> str:
    if fiscal_year is None:
        return "none"
    fy = f"FY{fiscal_year % 100:02d}"
    return f"Q{fiscal_quarter}{fy}" if fiscal_quarter else fy


def validate_claims(
    claims: list[ExtractedClaim],
    chunk_text: str,
    call_fiscal_year: int,
    call_fiscal_quarter: int | None,
) -> ValidationOutcome:
    outcome = ValidationOutcome()
    for claim in claims:
        validated = validate_claim(claim, chunk_text, call_fiscal_year, call_fiscal_quarter)
        if validated is None:
            outcome.rejected_hallucinated_quote += 1
            continue
        if any(note.startswith("numbers not found") for note in validated.notes):
            outcome.numbers_downgraded += 1
        outcome.accepted.append(validated)
    return outcome


# ------------------------------------------------------------------ dedupe


@dataclass(frozen=True)
class DedupeKey:
    """The fields two claims are compared on."""

    metric_normalized: str | None
    target_fiscal_year: int | None
    target_fiscal_quarter: int | None
    target_low: Decimal | None
    target_high: Decimal | None
    unit: str | None
    direction: str
    quote: str


def quote_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def _ranges_overlap(a: DedupeKey, b: DedupeKey) -> bool:
    if a.target_low is None and b.target_low is None:
        return a.direction == b.direction
    if (
        a.target_low is None
        or b.target_low is None
        or a.target_high is None
        or b.target_high is None
    ):
        return False
    return a.unit == b.unit and a.target_low <= b.target_high and b.target_low <= a.target_high


def _conflicts(a: DedupeKey, b: DedupeKey) -> bool:
    """Both state a target (or a period) and they differ: separate claims, however alike
    the wording ("line 1 adds 40k units in Q1" vs "line 2 adds 45k units in Q2")."""
    if a.target_low is not None and b.target_low is not None and not _ranges_overlap(a, b):
        return True
    period_a = (a.target_fiscal_year, a.target_fiscal_quarter)
    period_b = (b.target_fiscal_year, b.target_fiscal_quarter)
    return (
        a.target_fiscal_year is not None
        and b.target_fiscal_year is not None
        and (period_a != period_b)
    )


def is_duplicate(a: DedupeKey, b: DedupeKey) -> bool:
    """Same metric, and either the same period with overlapping targets, or a near-identical
    quote whose targets and period don't contradict each other."""
    if a.metric_normalized != b.metric_normalized:
        return False
    same_period = (a.target_fiscal_year, a.target_fiscal_quarter) == (
        b.target_fiscal_year,
        b.target_fiscal_quarter,
    )
    if same_period and _ranges_overlap(a, b):
        return True
    if _conflicts(a, b):
        return False
    return quote_similarity(a.quote, b.quote) >= DEDUPE_QUOTE_RATIO
