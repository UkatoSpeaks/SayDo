"""What each normalized metric is: its label, how it aggregates over a year, and
which direction is good. Used by derived metrics, verification and titles."""

from enum import StrEnum

from app.models import ClaimUnit, MetricNormalized
from app.services.scope import scope_label

M = MetricNormalized

LABELS: dict[str, str] = {
    M.REVENUE: "Revenue",
    M.REVENUE_GROWTH: "Revenue growth",
    M.EBITDA: "EBITDA",
    M.EBITDA_MARGIN: "EBITDA margin",
    M.EBIT: "EBIT",
    M.EBIT_MARGIN: "EBIT margin",
    M.GROSS_MARGIN: "Gross margin",
    M.PAT: "PAT",
    M.PAT_MARGIN: "PAT margin",
    M.CAPEX: "Capex",
    M.CAPACITY: "Capacity",
    M.CAPACITY_UTILIZATION: "Capacity utilization",
    M.VOLUME_GROWTH: "Volume growth",
    M.DEBT: "Debt",
    M.NET_DEBT: "Net debt",
    M.ORDER_BOOK: "Order book",
    M.MARKET_SHARE: "Market share",
    M.STORE_COUNT: "Store count",
    M.PRODUCT_LAUNCH: "Product launch",
    M.PLANT_COMMISSIONING: "Plant commissioning",
    M.DIVIDEND_PAYOUT: "Dividend payout",
    M.COST_REDUCTION: "Cost reduction",
    M.OTHER: "Other",
}

# Summed over the four quarters of a year.
FLOW_METRICS = frozenset({M.REVENUE, M.EBITDA, M.EBIT, M.PAT, M.CAPEX})
# A level at a point in time: the year's value is the Q4 value.
POINT_IN_TIME_METRICS = frozenset({M.CAPACITY, M.DEBT, M.NET_DEBT, M.ORDER_BOOK, M.STORE_COUNT})
# Margin = numerator / denominator * 100, recomputed from the parts, never averaged.
MARGIN_PARTS: dict[str, tuple[str, str]] = {
    M.EBITDA_MARGIN: (M.EBITDA, M.REVENUE),
    M.EBIT_MARGIN: (M.EBIT, M.REVENUE),
    M.PAT_MARGIN: (M.PAT, M.REVENUE),
}
# Year-on-year growth of a base metric. volume_growth has no base metric in the enum,
# so it is never derived.
GROWTH_BASE: dict[str, str] = {M.REVENUE_GROWTH: M.REVENUE}
GROWTH_METRICS = frozenset({M.REVENUE_GROWTH, M.VOLUME_GROWTH})

# Promises about events: judged from later commentary, not numbers.
EVENT_METRICS = frozenset({M.PLANT_COMMISSIONING, M.PRODUCT_LAUNCH})

# Metrics stated as a rate; actuals must be percent or bps.
RATE_METRICS = frozenset(
    {
        M.REVENUE_GROWTH,
        M.EBITDA_MARGIN,
        M.EBIT_MARGIN,
        M.GROSS_MARGIN,
        M.PAT_MARGIN,
        M.CAPACITY_UTILIZATION,
        M.VOLUME_GROWTH,
        M.MARKET_SHARE,
        M.DIVIDEND_PAYOUT,
    }
)
# Metrics stated as money; actuals must be an INR or USD unit.
MONEY_METRICS = frozenset(
    {M.REVENUE, M.EBITDA, M.EBIT, M.PAT, M.CAPEX, M.DEBT, M.NET_DEBT, M.ORDER_BOOK}
)
RATE_UNITS = frozenset({ClaimUnit.PERCENT, ClaimUnit.BPS})
MONEY_UNITS = frozenset(
    {ClaimUnit.INR_CRORE, ClaimUnit.INR_LAKH, ClaimUnit.INR_BN, ClaimUnit.USD_MN}
)


class Polarity(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    # A plan (e.g. capex): landing outside the guided range either way is a miss.
    NEUTRAL = "neutral"


_LOWER_IS_BETTER = frozenset({M.DEBT, M.NET_DEBT})
_NEUTRAL = frozenset({M.CAPEX, M.OTHER})


def polarity(metric: str | None) -> Polarity:
    if metric in _LOWER_IS_BETTER:
        return Polarity.LOWER_IS_BETTER
    if metric is None or metric in _NEUTRAL:
        return Polarity.NEUTRAL
    return Polarity.HIGHER_IS_BETTER


def label(metric: str | None) -> str:
    return LABELS.get(metric or "", (metric or "Claim").replace("_", " ").capitalize())


def scoped_label(metric: str | None, scope: str | None) -> str:
    """ "Revenue growth", or "Services revenue growth" for a scoped claim."""
    base = label(metric)
    prefix = scope_label(scope)
    if prefix is None:
        return base
    # Keep acronyms ("EBIT margin"); lower-case words ("Revenue growth" -> "revenue growth").
    return f"{prefix} {base if base[:2].isupper() else base[:1].lower() + base[1:]}"


def allowed_units(metric: str) -> frozenset[ClaimUnit] | None:
    """Units an actual of ``metric`` may use (``None`` = any unit)."""
    if metric in RATE_METRICS:
        return RATE_UNITS
    if metric in MONEY_METRICS:
        return MONEY_UNITS
    return None
