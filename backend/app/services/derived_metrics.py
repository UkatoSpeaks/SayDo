"""Values derived in code from reported actuals. Never stored, never presented as reported.

Rules (only when the derived value is not itself reported):

- Annual flow metrics (revenue, ebitda, ebit, pat, capex) = sum of the four quarters,
  only when all four are reported.
- Margins (ebitda_margin, ebit_margin, pat_margin) = numerator / denominator * 100 for
  the same period, using the reported or derived parts (an annual margin is therefore
  recomputed from summed EBITDA and revenue, never an average of quarterly margins).
- Annual point-in-time metrics (capacity, debt, net_debt, order_book, store_count) =
  the Q4 value.
- revenue_growth = year-on-year change of revenue when both periods are available.
  volume_growth and gross_margin have no base metric and are never derived.

Every derived value carries ``derived_from``, a sentence saying how it was computed.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from app.models import ClaimUnit
from app.services.fiscal import period_label
from app.services.metrics import (
    FLOW_METRICS,
    GROWTH_BASE,
    MARGIN_PARTS,
    POINT_IN_TIME_METRICS,
    label,
)
from app.services.scope import COMPANY
from app.services.units import EN_DASH, convert, format_value

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class ActualPoint:
    """A reported actual (one ``actuals`` row)."""

    metric: str
    fiscal_year: int
    fiscal_quarter: int | None
    value: Decimal
    unit: str
    id: uuid.UUID | None = None
    source_note: str | None = None
    source_page: int | None = None
    scope: str = COMPANY


@dataclass(frozen=True)
class ResolvedActual:
    metric: str
    fiscal_year: int
    fiscal_quarter: int | None
    value: Decimal
    unit: str
    derived: bool
    derived_from: str | None = None
    # The reported row (only when not derived).
    actual_id: uuid.UUID | None = None
    # Every reported row the value depends on.
    source_actual_ids: tuple[uuid.UUID, ...] = field(default=())
    source_note: str | None = None
    source_page: int | None = None

    @property
    def period_label(self) -> str:
        return period_label(self.fiscal_year, self.fiscal_quarter)

    def snapshot(self) -> dict[str, object]:
        """JSON-safe form stored with a verification."""
        return {
            "metric": self.metric,
            "period": self.period_label,
            "value": format(self.value.normalize(), "f"),
            "unit": self.unit,
            "derived": self.derived,
            "derived_from": self.derived_from,
            "actual_id": str(self.actual_id) if self.actual_id else None,
            "source_actual_ids": [str(i) for i in self.source_actual_ids],
            "source_note": self.source_note,
            "source_page": self.source_page,
        }


def _reported(point: ActualPoint) -> ResolvedActual:
    return ResolvedActual(
        metric=point.metric,
        fiscal_year=point.fiscal_year,
        fiscal_quarter=point.fiscal_quarter,
        value=point.value,
        unit=point.unit,
        derived=False,
        actual_id=point.id,
        source_actual_ids=(point.id,) if point.id else (),
        source_note=point.source_note,
        source_page=point.source_page,
    )


def _ids(*values: ResolvedActual) -> tuple[uuid.UUID, ...]:
    seen: dict[uuid.UUID, None] = {}
    for value in values:
        for actual_id in value.source_actual_ids:
            seen[actual_id] = None
    return tuple(seen)


class ActualsIndex:
    """Reported actuals of one company and one scope (``company`` unless asked), with
    on-demand derivation. ``in_scope`` gives the index of another scope."""

    def __init__(self, points: Iterable[ActualPoint], scope: str = COMPANY) -> None:
        self._all = list(points)
        self.scope = scope
        self._points: dict[tuple[str, int, int | None], ActualPoint] = {
            (p.metric, p.fiscal_year, p.fiscal_quarter): p for p in self._all if p.scope == scope
        }

    def scopes(self) -> list[str]:
        """Every scope with an actual, company first."""
        return sorted({COMPANY, *(p.scope for p in self._all)}, key=lambda s: (s != COMPANY, s))

    def in_scope(self, scope: str | None) -> "ActualsIndex":
        scope = scope or COMPANY
        return self if scope == self.scope else ActualsIndex(self._all, scope)

    def __len__(self) -> int:
        return len(self._points)

    def reported_values(self) -> list[ResolvedActual]:
        return [
            _reported(p) for _, p in sorted(self._points.items(), key=lambda kv: _sort_key(kv[0]))
        ]

    def reported(self, metric: str, fiscal_year: int, quarter: int | None) -> ResolvedActual | None:
        point = self._points.get((metric, fiscal_year, quarter))
        return _reported(point) if point is not None else None

    def resolve(self, metric: str, fiscal_year: int, quarter: int | None) -> ResolvedActual | None:
        """The reported value, else a derived one, else ``None`` (never assumed)."""
        return self.reported(metric, fiscal_year, quarter) or self.derive(
            metric, fiscal_year, quarter
        )

    def derive(self, metric: str, fiscal_year: int, quarter: int | None) -> ResolvedActual | None:
        """A derived value for the period, ignoring any reported value for it."""
        if metric in GROWTH_BASE:
            return self._growth(metric, fiscal_year, quarter)
        if metric in MARGIN_PARTS:
            return self._margin(metric, fiscal_year, quarter)
        if quarter is None and metric in FLOW_METRICS:
            return self._annual_sum(metric, fiscal_year)
        if quarter is None and metric in POINT_IN_TIME_METRICS:
            return self._annual_point(metric, fiscal_year)
        return None

    def derived_values(self) -> list[ResolvedActual]:
        """Every value that can be derived for the periods present and is not reported."""
        years = {p.fiscal_year for p in self._points.values()}
        candidates: set[tuple[str, int, int | None]] = set()
        derivable = FLOW_METRICS | POINT_IN_TIME_METRICS | set(MARGIN_PARTS) | set(GROWTH_BASE)
        for metric in derivable:
            for year in years:
                for quarter in (None, 1, 2, 3, 4):
                    candidates.add((metric, year, quarter))
        out = []
        for metric, year, quarter in sorted(candidates, key=_sort_key):
            if (metric, year, quarter) in self._points:
                continue
            value = self.derive(metric, year, quarter)
            if value is not None:
                out.append(value)
        return out

    # ------------------------------------------------------------- rules

    def _annual_sum(self, metric: str, fiscal_year: int) -> ResolvedActual | None:
        quarters = [self.reported(metric, fiscal_year, q) for q in (1, 2, 3, 4)]
        if any(q is None for q in quarters):
            return None
        parts = [q for q in quarters if q is not None]
        unit = parts[0].unit
        converted = [convert(p.value, p.unit, unit) for p in parts]
        if any(c is None for c in converted):
            return None
        total = sum((c for c in converted if c is not None), Decimal(0))
        terms = " + ".join(format_value(c, unit) for c in converted if c is not None)
        return ResolvedActual(
            metric=metric,
            fiscal_year=fiscal_year,
            fiscal_quarter=None,
            value=total,
            unit=unit,
            derived=True,
            derived_from=(
                f"Sum of Q1{EN_DASH}Q4 FY{fiscal_year % 100:02d} {label(metric)} "
                f"({terms}); no annual figure was entered"
            ),
            source_actual_ids=_ids(*parts),
        )

    def _annual_point(self, metric: str, fiscal_year: int) -> ResolvedActual | None:
        q4 = self.reported(metric, fiscal_year, 4)
        if q4 is None:
            return None
        return ResolvedActual(
            metric=metric,
            fiscal_year=fiscal_year,
            fiscal_quarter=None,
            value=q4.value,
            unit=q4.unit,
            derived=True,
            derived_from=(
                f"{label(metric)} is a point-in-time figure: the FY{fiscal_year % 100:02d} "
                f"value is the Q4 value ({format_value(q4.value, q4.unit)})"
            ),
            source_actual_ids=_ids(q4),
        )

    def _margin(self, metric: str, fiscal_year: int, quarter: int | None) -> ResolvedActual | None:
        numerator_metric, denominator_metric = MARGIN_PARTS[metric]
        numerator = self.resolve(numerator_metric, fiscal_year, quarter)
        denominator = self.resolve(denominator_metric, fiscal_year, quarter)
        if numerator is None or denominator is None:
            return None
        numerator_value = convert(numerator.value, numerator.unit, denominator.unit)
        if numerator_value is None or denominator.value == 0:
            return None
        value = numerator_value / denominator.value * HUNDRED
        period = period_label(fiscal_year, quarter)
        how = (
            f"{period} {label(numerator_metric)} {format_value(numerator.value, numerator.unit)}"
            f" / {label(denominator_metric)} "
            f"{format_value(denominator.value, denominator.unit)} * 100"
        )
        how += _parts_note(numerator, denominator)
        return ResolvedActual(
            metric=metric,
            fiscal_year=fiscal_year,
            fiscal_quarter=quarter,
            value=value,
            unit=ClaimUnit.PERCENT.value,
            derived=True,
            derived_from=how,
            source_actual_ids=_ids(numerator, denominator),
        )

    def _growth(self, metric: str, fiscal_year: int, quarter: int | None) -> ResolvedActual | None:
        base_metric = GROWTH_BASE[metric]
        current = self.resolve(base_metric, fiscal_year, quarter)
        previous = self.resolve(base_metric, fiscal_year - 1, quarter)
        if current is None or previous is None:
            return None
        previous_value = convert(previous.value, previous.unit, current.unit)
        if previous_value is None or previous_value == 0:
            return None
        value = (current.value / previous_value - 1) * HUNDRED
        how = (
            f"Year-on-year: {current.period_label} {label(base_metric)} "
            f"{format_value(current.value, current.unit)} vs {previous.period_label} "
            f"{format_value(previous.value, previous.unit)}"
        )
        how += _parts_note(current, previous)
        return ResolvedActual(
            metric=metric,
            fiscal_year=fiscal_year,
            fiscal_quarter=quarter,
            value=value,
            unit=ClaimUnit.PERCENT.value,
            derived=True,
            derived_from=how,
            source_actual_ids=_ids(current, previous),
        )


def _parts_note(*parts: ResolvedActual) -> str:
    """Short note on inputs that were themselves derived: " (EBITDA, Revenue: sum of Q1-Q4)"."""
    notes: dict[str, list[str]] = {}
    for part in parts:
        if not part.derived:
            continue
        if part.metric in FLOW_METRICS:
            how = f"sum of Q1{EN_DASH}Q4"
        elif part.metric in POINT_IN_TIME_METRICS:
            how = "Q4 value"
        else:
            how = "derived"
        names = notes.setdefault(how, [])
        if label(part.metric) not in names:
            names.append(label(part.metric))
    if not notes:
        return ""
    return " (" + "; ".join(f"{', '.join(n)}: {how}" for how, n in notes.items()) + ")"


def _sort_key(key: tuple[str, int, int | None]) -> tuple[str, int, int]:
    metric, year, quarter = key
    return metric, year, quarter or 5
