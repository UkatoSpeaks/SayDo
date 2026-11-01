"""Import reported actuals from a CSV file (entered by hand from results filings).

Columns: nse_symbol, metric_normalized, fiscal_year, fiscal_quarter (blank = full
year), value, unit, source_note, source_page, and optionally scope ("company" when the
column is missing or blank; "services" or "segment:<name>" for a part of the company).

Every row is validated and *all* problems are reported with their row number
(the header is row 1). If any row is invalid nothing is written, so a file is
never half-imported. Valid files are upserted on (company, metric, fiscal_year,
scope, fiscal_quarter).
"""

import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import Boolean, literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Actual, ClaimUnit, Company, MetricNormalized
from app.services.fiscal import MAX_FISCAL_YEAR, MIN_FISCAL_YEAR
from app.services.metrics import allowed_units
from app.services.scope import COMPANY, normalize_scope

COLUMNS = (
    "nse_symbol",
    "metric_normalized",
    "fiscal_year",
    "fiscal_quarter",
    "value",
    "unit",
    "source_note",
    "source_page",
)
# Optional columns: may be left out of the header entirely.
OPTIONAL_COLUMNS = ("scope",)
REQUIRED = ("nse_symbol", "metric_normalized", "fiscal_year", "value", "unit")
MAX_ROWS = 5000
MAX_NOTE_CHARS = 1000
_FY_RE = re.compile(r"^(?:FY\s*'?)?(\d{4}|\d{2})$", re.IGNORECASE)
_QUARTER_RE = re.compile(r"^Q?([1-4])$", re.IGNORECASE)


@dataclass(frozen=True)
class RowError:
    row: int
    field: str | None
    message: str


@dataclass(frozen=True)
class ParsedRow:
    row: int
    metric: str
    fiscal_year: int
    fiscal_quarter: int | None
    value: Decimal
    unit: str
    source_note: str | None
    source_page: int | None
    scope: str = COMPANY


@dataclass
class ImportReport:
    rows_total: int = 0
    created: int = 0
    updated: int = 0
    errors: list[RowError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _parse_year(raw: str) -> int | None:
    match = _FY_RE.match(raw.strip())
    if match is None:
        return None
    year = int(match.group(1))
    year = year + 2000 if year < 100 else year
    return year if MIN_FISCAL_YEAR <= year <= MAX_FISCAL_YEAR else None


def _parse_value(raw: str) -> Decimal | None:
    cleaned = raw.strip().replace(",", "").replace("_", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def parse_actuals_csv(content: bytes, company: Company) -> tuple[list[ParsedRow], list[RowError]]:
    """Parse and validate every row; returns the valid rows and all errors."""
    errors: list[RowError] = []
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [RowError(1, None, "File is not UTF-8 text.")]
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in COLUMNS if c not in header]
    unknown = [h for h in header if h and h not in COLUMNS and h not in OPTIONAL_COLUMNS]
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing column(s): {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown column(s): {', '.join(unknown)}")
        return [], [
            RowError(
                1,
                None,
                f"Header must be {','.join(COLUMNS)} (optional: {','.join(OPTIONAL_COLUMNS)}); "
                + "; ".join(parts),
            )
        ]
    reader.fieldnames = header

    rows: list[ParsedRow] = []
    seen: dict[tuple[str, str, int, int | None], int] = {}
    symbol = (company.nse_symbol or "").upper()
    for index, raw in enumerate(reader, start=2):
        if index - 1 > MAX_ROWS:
            errors.append(RowError(index, None, f"At most {MAX_ROWS} rows per file."))
            break
        if None in raw:  # more cells than columns
            errors.append(RowError(index, None, "Row has more cells than the header."))
            continue
        cells = {k: (v or "").strip() for k, v in raw.items()}
        for name in OPTIONAL_COLUMNS:
            cells.setdefault(name, "")
        if not any(cells.values()):
            continue  # blank line
        row_errors: list[RowError] = []
        for name in REQUIRED:
            if not cells[name]:
                row_errors.append(RowError(index, name, "Required."))

        if cells["nse_symbol"] and cells["nse_symbol"].upper() != symbol:
            row_errors.append(
                RowError(
                    index,
                    "nse_symbol",
                    f"{cells['nse_symbol']!r} does not match this company ({symbol or 'none'}).",
                )
            )

        metric: str | None = None
        if cells["metric_normalized"]:
            try:
                metric = MetricNormalized(cells["metric_normalized"].lower()).value
            except ValueError:
                row_errors.append(
                    RowError(
                        index,
                        "metric_normalized",
                        f"Unknown metric {cells['metric_normalized']!r}; use one of: "
                        + ", ".join(m.value for m in MetricNormalized),
                    )
                )

        year: int | None = None
        if cells["fiscal_year"]:
            year = _parse_year(cells["fiscal_year"])
            if year is None:
                row_errors.append(
                    RowError(
                        index,
                        "fiscal_year",
                        f"Invalid fiscal year {cells['fiscal_year']!r}; use e.g. 2026 or FY26.",
                    )
                )

        quarter: int | None = None
        if cells["fiscal_quarter"]:
            match = _QUARTER_RE.match(cells["fiscal_quarter"])
            if match is None:
                row_errors.append(
                    RowError(
                        index,
                        "fiscal_quarter",
                        f"Invalid quarter {cells['fiscal_quarter']!r}; use 1-4 or blank for the "
                        "full year.",
                    )
                )
            else:
                quarter = int(match.group(1))

        value: Decimal | None = None
        if cells["value"]:
            value = _parse_value(cells["value"])
            if value is None:
                row_errors.append(RowError(index, "value", f"Not a number: {cells['value']!r}."))

        unit: str | None = None
        if cells["unit"]:
            try:
                unit = ClaimUnit(cells["unit"].lower()).value
            except ValueError:
                row_errors.append(
                    RowError(
                        index,
                        "unit",
                        f"Unknown unit {cells['unit']!r}; use one of: "
                        + ", ".join(u.value for u in ClaimUnit),
                    )
                )
        if metric is not None and unit is not None:
            allowed = allowed_units(metric)
            if allowed is not None and ClaimUnit(unit) not in allowed:
                row_errors.append(
                    RowError(
                        index,
                        "unit",
                        f"{unit} is not a valid unit for {metric}; use "
                        + " or ".join(sorted(u.value for u in allowed))
                        + ".",
                    )
                )

        page: int | None = None
        if cells["source_page"]:
            if cells["source_page"].isdigit() and int(cells["source_page"]) >= 1:
                page = int(cells["source_page"])
            else:
                row_errors.append(
                    RowError(index, "source_page", "Must be a page number (1 or more).")
                )
        note = cells["source_note"] or None
        if note is not None and len(note) > MAX_NOTE_CHARS:
            row_errors.append(
                RowError(index, "source_note", f"At most {MAX_NOTE_CHARS} characters.")
            )

        scope = normalize_scope(cells["scope"])
        if metric is not None and year is not None and not row_errors:
            key = (metric, scope, year, quarter)
            if key in seen:
                row_errors.append(
                    RowError(
                        index,
                        None,
                        f"Duplicate of row {seen[key]} (same metric, scope and period).",
                    )
                )
            else:
                seen[key] = index

        if row_errors:
            errors.extend(row_errors)
            continue
        assert metric is not None and year is not None and value is not None and unit is not None
        rows.append(ParsedRow(index, metric, year, quarter, value, unit, note, page, scope))

    if not rows and not errors:
        errors.append(RowError(1, None, "The file has no data rows."))
    return rows, errors


async def import_actuals(session: AsyncSession, company: Company, content: bytes) -> ImportReport:
    """Validate ``content`` and upsert its rows (all or nothing). Commits on success."""
    rows, errors = parse_actuals_csv(content, company)
    report = ImportReport(rows_total=len(rows) + len({e.row for e in errors if e.row > 1}))
    if errors:
        report.errors = errors
        return report

    for row in rows:
        stmt = insert(Actual).values(
            id=uuid.uuid4(),
            company_id=company.id,
            metric_normalized=row.metric,
            scope=row.scope,
            fiscal_year=row.fiscal_year,
            fiscal_quarter=row.fiscal_quarter,
            value=row.value,
            unit=row.unit,
            source_note=row.source_note,
            source_page=row.source_page,
        )
        upsert = stmt.on_conflict_do_update(
            constraint="uq_actuals_company_metric_period",
            set_={
                "value": stmt.excluded.value,
                "unit": stmt.excluded.unit,
                "source_note": stmt.excluded.source_note,
                "source_page": stmt.excluded.source_page,
            },
        ).returning(literal_column("(xmax = 0)", Boolean))  # xmax is 0 only on a fresh insert
        inserted = (await session.execute(upsert)).scalar_one()
        if inserted:
            report.created += 1
        else:
            report.updated += 1
    await session.commit()
    return report
