"""Resolve a spoken timeframe ("by FY27", "H2", "next year") to a fiscal period.

Results calls happen *after* the quarter they report on, so relative phrases are
anchored on the reported quarter:

    Q2FY26 call (held Oct-Nov 2025):
        "next quarter" -> Q3FY26 (the quarter after the reported one)
        "this year"    -> FY26   (the fiscal year the call is held in)
        "next year"    -> FY27
    Q4FY26 call (held Apr-May 2026, i.e. already in FY27):
        "next quarter" -> Q1FY27, "this year" -> FY27, "next year" -> FY28

Explicit calendar dates resolve to the quarter that contains them ("March 31, 2027" ->
Q4FY27; a day is never read as a year). Halves resolve to their last quarter (H1 -> Q2,
H2 -> Q4) and multi-period phrases ("next two years", "next 3 quarters", "next 18
months") to their last period; both set ``is_range``. "Medium term", "long term" and
similar resolve to no period.
``resolve_timeframe`` returns ``None`` when no rule applies.
"""

import re
from dataclasses import dataclass
from datetime import date

from app.services.fiscal import MAX_FISCAL_YEAR, MIN_FISCAL_YEAR, date_to_fiscal

_NUMBER_WORDS = {
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
    "a couple of": 2,
    "couple of": 2,
    "a few": None,
}
_ORDINAL_QUARTERS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "last": 4,
    "final": 4,
}
_MONTHS = {
    name: index
    for index, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}

# "FY27", "FY 27", "FY'27", "FY2027", "FY2026-27", "FY26-27", "fiscal 2027", "fiscal year 27",
# "financial year 2026-27".
_FY = (
    r"(?:fy|fiscal(?:\s+year)?|financial\s+year)\s*['\u2019]?\s*"
    r"(?P<{name}>\d{{4}}|\d{{2}})(?:\s*[-/\u2013]\s*(?P<{name}_end>\d{{4}}|\d{{2}}))?(?!\d)"
)
_REL_YEAR = (
    r"(?P<{name}>this|current|the\s+current|next|the\s+next|coming|the\s+coming)\s+"
    r"(?:fiscal\s+year|financial\s+year|fiscal|financial|year)"
)
_QUARTER = (
    r"(?:q(?P<qnum>[1-4])|(?P<qword>first|1st|second|2nd|third|3rd|fourth|4th|last|final)"
    r"\s+quarter)"
)
_HALF = r"(?:h(?P<hnum>[12])|(?P<hword>first|1st|second|2nd)\s+half)"
_CONNECTOR = r"[\s\-_']*(?:of|in|for)?\s*(?:the\s+)?"

_NONE_RE = re.compile(
    r"\b(?:medium|mid|long|short|near)[\s-]*term\b|\bover\s+time\b|\bin\s+(?:the\s+)?future\b"
    r"|\beventually\b|\bin\s+due\s+course\b",
    re.IGNORECASE,
)
_QUARTER_FY_RE = re.compile(_QUARTER + _CONNECTOR + _FY.format(name="fy"), re.IGNORECASE)
_FY_QUARTER_RE = re.compile(
    _FY.format(name="fy") + r"[\s\-_']*" + r"q(?P<qnum>[1-4])\b", re.IGNORECASE
)
_HALF_FY_RE = re.compile(_HALF + _CONNECTOR + _FY.format(name="fy"), re.IGNORECASE)
_FY_HALF_RE = re.compile(_FY.format(name="fy") + r"[\s\-_']*h(?P<hnum>[12])\b", re.IGNORECASE)
_QUARTER_REL_RE = re.compile(_QUARTER + _CONNECTOR + _REL_YEAR.format(name="rel"), re.IGNORECASE)
_HALF_REL_RE = re.compile(_HALF + _CONNECTOR + _REL_YEAR.format(name="rel"), re.IGNORECASE)
_FY_RE = re.compile(r"\b" + _FY.format(name="fy"), re.IGNORECASE)
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MONTH_YEAR_RE = re.compile(
    r"\b(?P<month>" + _MONTH_NAMES + r")\.?,?\s*['\u2019]?"
    # Not a day of the month followed by the year ("March 31, 2027").
    r"(?P<year>\d{4}|\d{2})\b(?!(?:st|nd|rd|th)?\s*,?\s*\d{4})",
    re.IGNORECASE,
)
# Explicit calendar dates: "March 31, 2027", "31st March 2027", "31 Mar, 2027", "31/03/2027"
# (numeric dates are read day first, as written in India).
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_DATE_RES = (
    re.compile(
        r"\b(?P<month>" + _MONTH_NAMES + r")\.?\s+" + _DAY + r"\s*,?\s*(?P<year>\d{4})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b" + _DAY + r"\s+(?:of\s+)?(?P<month>" + _MONTH_NAMES + r")\.?\s*,?\s*"
        r"(?P<year>\d{4})\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?P<day>\d{1,2})[/.\-](?P<month>\d{1,2})[/.\-](?P<year>\d{4})\b"),
)
_NEXT_N_RE = re.compile(
    r"\b(?:next|coming|over\s+the\s+next|within\s+(?:the\s+next\s+)?|in\s+(?:the\s+next\s+)?)"
    r"\s*(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten|a\s+couple\s+of|couple\s+of)"
    r"(?:\s*(?:-|to|or)\s*(?P<n2>\d+|two|three|four|five|six|seven|eight|nine|ten))?"
    r"\s+(?P<unit>years?|quarters?|months?)\b",
    re.IGNORECASE,
)
_NEXT_QUARTER_RE = re.compile(
    r"\b(?:next|coming|the\s+next|the\s+coming|following)\s+quarter\b", re.IGNORECASE
)
_REL_YEAR_RE = re.compile(r"\b" + _REL_YEAR.format(name="rel") + r"\b", re.IGNORECASE)
_YEAR_END_RE = re.compile(
    r"\b(?:by\s+)?(?:the\s+)?(?:end\s+of\s+(?:the\s+)?|this\s+)?year[\s-]*end\b"
    r"|\bend\s+of\s+(?:the\s+)?(?:this\s+)?year\b",
    re.IGNORECASE,
)
_BARE_HALF_RE = re.compile(r"\b" + _HALF + r"\b", re.IGNORECASE)
_BARE_QUARTER_RE = re.compile(r"\b" + _QUARTER + r"\b", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedTimeframe:
    fiscal_year: int | None
    fiscal_quarter: int | None
    is_range: bool = False
    rule: str = ""


def _year(raw: str, end: str | None = None) -> int:
    year = 2000 + int(raw) if len(raw) == 2 else int(raw)
    if end is not None:
        # "FY2026-27" names the year it ends in.
        year += 1
    return year


def _in_range(year: int) -> bool:
    return MIN_FISCAL_YEAR <= year <= MAX_FISCAL_YEAR


def _quarter(match: re.Match[str]) -> int:
    if match["qnum"]:
        return int(match["qnum"])
    return _ORDINAL_QUARTERS[match["qword"].lower()]


def _half_end_quarter(match: re.Match[str]) -> int:
    half = int(match["hnum"]) if match["hnum"] else (1 if match["hword"][0] in "f1" else 2)
    return 2 if half == 1 else 4


def _ordinal(fiscal_year: int, quarter: int) -> int:
    return fiscal_year * 4 + quarter - 1


def _from_ordinal(ordinal: int) -> tuple[int, int]:
    return ordinal // 4, ordinal % 4 + 1


@dataclass(frozen=True)
class CallAnchor:
    """The reported period of the call and what "now" means on it."""

    fiscal_year: int
    fiscal_quarter: int | None

    @property
    def reported_ordinal(self) -> int:
        # A full-year document (no quarter) behaves like a Q4 call.
        return _ordinal(self.fiscal_year, self.fiscal_quarter or 4)

    @property
    def current_year(self) -> int:
        """The fiscal year the call is held in (the quarter after the reported one)."""
        return _from_ordinal(self.reported_ordinal + 1)[0]

    def relative_year(self, word: str) -> int:
        word = " ".join(word.lower().split())
        return self.current_year + (
            1 if word in {"next", "the next", "coming", "the coming"} else 0
        )

    def next_quarter(self, n: int = 1) -> tuple[int, int]:
        return _from_ordinal(self.reported_ordinal + n)

    def next_occurrence_of_quarter(self, quarter: int) -> tuple[int, int]:
        """The first ``Q<quarter>`` after the reported quarter ("by Q3" in a Q2 call)."""
        fiscal_year, reported_quarter = _from_ordinal(self.reported_ordinal)
        if quarter <= reported_quarter:
            fiscal_year += 1
        return fiscal_year, quarter


def _calendar_date(match: re.Match[str]) -> date | None:
    month_text = match["month"]
    month = int(month_text) if month_text.isdigit() else _MONTHS[month_text.lower()]
    try:
        return date(int(match["year"]), month, int(match["day"]))
    except ValueError:
        return None


def _count(raw: str) -> int | None:
    raw = " ".join(raw.lower().split())
    return int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)


def _checked(result: ResolvedTimeframe) -> ResolvedTimeframe | None:
    if result.fiscal_year is not None and not _in_range(result.fiscal_year):
        return None
    return result


def resolve_timeframe(
    raw: str | None, call_fiscal_year: int, call_fiscal_quarter: int | None
) -> ResolvedTimeframe | None:
    """Deterministic resolution of ``raw``; ``None`` when no rule matches."""
    if raw is None or not raw.strip():
        return None
    text = " ".join(raw.replace("\u2019", "'").split())
    anchor = CallAnchor(call_fiscal_year, call_fiscal_quarter)

    for pattern in (_QUARTER_FY_RE, _FY_QUARTER_RE):
        if match := pattern.search(text):
            year = _year(match["fy"], match["fy_end"])
            return _checked(ResolvedTimeframe(year, _quarter(match), rule="quarter+fy"))
    for pattern in (_HALF_FY_RE, _FY_HALF_RE):
        if match := pattern.search(text):
            year = _year(match["fy"], match["fy_end"])
            return _checked(ResolvedTimeframe(year, _half_end_quarter(match), True, "half+fy"))
    if match := _QUARTER_REL_RE.search(text):
        year = anchor.relative_year(match["rel"])
        return _checked(ResolvedTimeframe(year, _quarter(match), rule="quarter+relative"))
    if match := _HALF_REL_RE.search(text):
        year = anchor.relative_year(match["rel"])
        return _checked(ResolvedTimeframe(year, _half_end_quarter(match), True, "half+relative"))
    if match := _FY_RE.search(text):
        return _checked(ResolvedTimeframe(_year(match["fy"], match["fy_end"]), None, rule="fy"))
    for pattern in _DATE_RES:
        if (match := pattern.search(text)) and (day := _calendar_date(match)) is not None:
            # Indian FY: Apr-Dec of year Y is in FY(Y+1), Jan-Mar of year Y in FY(Y).
            period = date_to_fiscal(day)
            return _checked(
                ResolvedTimeframe(period.fiscal_year, period.fiscal_quarter, rule="date")
            )
    if match := _MONTH_YEAR_RE.search(text):
        year = _year(match["year"])
        if _in_range(year):
            period = date_to_fiscal(date(year, _MONTHS[match["month"].lower()], 1))
            return ResolvedTimeframe(period.fiscal_year, period.fiscal_quarter, rule="month+year")
    if match := _NEXT_N_RE.search(text):
        n = _count(match["n2"] or match["n"])
        if n is not None and n >= 1:
            if match["unit"].lower().startswith("year"):
                if n == 1 and not match["n2"]:
                    return ResolvedTimeframe(anchor.current_year + 1, None, rule="next year")
                return _checked(
                    ResolvedTimeframe(anchor.current_year + n, None, True, "next n years")
                )
            if match["unit"].lower().startswith("month"):
                n = -(-n // 3)  # 18 months -> 6 quarters
            year, quarter = anchor.next_quarter(n)
            return _checked(ResolvedTimeframe(year, quarter, n > 1, "next n quarters"))
    if _NEXT_QUARTER_RE.search(text):
        year, quarter = anchor.next_quarter()
        return ResolvedTimeframe(year, quarter, rule="next quarter")
    if match := _REL_YEAR_RE.search(text):
        return ResolvedTimeframe(anchor.relative_year(match["rel"]), None, rule="relative year")
    if _YEAR_END_RE.search(text):
        return ResolvedTimeframe(anchor.current_year, None, rule="year end")
    if match := _BARE_HALF_RE.search(text):
        # "in H2" on its own: the half of the fiscal year the call is held in.
        return ResolvedTimeframe(anchor.current_year, _half_end_quarter(match), True, "bare half")
    if match := _BARE_QUARTER_RE.search(text):
        year, quarter = anchor.next_occurrence_of_quarter(_quarter(match))
        return ResolvedTimeframe(year, quarter, rule="bare quarter")
    if _NONE_RE.search(text):
        return ResolvedTimeframe(None, None, rule="open-ended")
    return None


# "2027", "FY27", "CY27", "'27"
_ANY_YEAR_RE = re.compile(r"(?<!\d)20(\d{2})(?!\d)|(?:\b(?:fy|cy)\s*|')(\d{2})(?!\d)", re.I)
_QUARTER_HINT_RE = re.compile(
    r"\bq[1-4]\b|quarter|\bh[12]\b|half|" + "|".join(sorted(_MONTHS, key=len, reverse=True)),
    re.IGNORECASE,
)
MAX_YEARS_AHEAD = 10


def model_timeframe_is_consistent(
    raw: str | None,
    fiscal_year: int | None,
    fiscal_quarter: int | None,
    call_fiscal_year: int,
    call_fiscal_quarter: int | None,
) -> tuple[int | None, int | None]:
    """Keep the model's period only where ``raw`` supports it; otherwise null it.

    Used when ``resolve_timeframe`` has no rule for ``raw`` (e.g. "by 2027",
    "post monsoon"). The year must not be before the call or implausibly far out,
    and when ``raw`` contains year numbers the model's year must be within one of
    them (calendar vs fiscal naming). A quarter needs a quarter/half/month word.
    """
    if raw is None or not raw.strip() or fiscal_year is None:
        return None, None
    anchor = CallAnchor(call_fiscal_year, call_fiscal_quarter)
    if not call_fiscal_year <= fiscal_year <= anchor.current_year + MAX_YEARS_AHEAD:
        return None, None
    years = {2000 + int(a or b) for a, b in _ANY_YEAR_RE.findall(raw)}
    if years and not any(abs(fiscal_year - year) <= 1 for year in years):
        return None, None
    quarter = fiscal_quarter if _QUARTER_HINT_RE.search(raw) else None
    return fiscal_year, quarter
