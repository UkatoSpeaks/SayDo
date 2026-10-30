import itertools
from datetime import date, datetime

import pytest

from app.services.fiscal import (
    FiscalPeriod,
    date_to_fiscal,
    fiscal_quarter_date_range,
    parse_period_label,
    period_label,
)


class TestDateToFiscal:
    @pytest.mark.parametrize(
        ("d", "expected"),
        [
            # FY27 = 1 Apr 2026 .. 31 Mar 2027
            (date(2026, 4, 1), (2027, 1)),
            (date(2026, 6, 30), (2027, 1)),
            (date(2026, 7, 1), (2027, 2)),
            (date(2026, 9, 30), (2027, 2)),
            (date(2026, 10, 1), (2027, 3)),
            (date(2026, 12, 31), (2027, 3)),
            (date(2027, 1, 1), (2027, 4)),
            (date(2027, 3, 31), (2027, 4)),
            # Jan-Mar belongs to the FY that started the previous calendar year.
            (date(2026, 1, 15), (2026, 4)),
            (date(2026, 3, 31), (2026, 4)),
            (date(2024, 2, 29), (2024, 4)),
            (date(2025, 11, 20), (2026, 3)),
        ],
    )
    def test_quarters_and_boundaries(self, d: date, expected: tuple[int, int]) -> None:
        assert date_to_fiscal(d) == expected

    def test_every_month_maps_to_expected_quarter(self) -> None:
        quarters = [date_to_fiscal(date(2025, m, 1)).fiscal_quarter for m in range(1, 13)]
        assert quarters == [4, 4, 4, 1, 1, 1, 2, 2, 2, 3, 3, 3]

    def test_new_year_does_not_change_fiscal_year(self) -> None:
        assert date_to_fiscal(date(2025, 12, 31)).fiscal_year == 2026
        assert date_to_fiscal(date(2026, 1, 1)).fiscal_year == 2026

    def test_march_to_april_rolls_fiscal_year(self) -> None:
        assert date_to_fiscal(date(2026, 3, 31)) == (2026, 4)
        assert date_to_fiscal(date(2026, 4, 1)) == (2027, 1)

    def test_accepts_datetime_and_returns_named_tuple(self) -> None:
        result = date_to_fiscal(datetime(2025, 8, 15, 18, 30))
        assert isinstance(result, FiscalPeriod)
        assert result.fiscal_year == 2026
        assert result.fiscal_quarter == 2


class TestPeriodLabel:
    @pytest.mark.parametrize(
        ("fy", "q", "expected"),
        [
            (2026, 2, "Q2FY26"),
            (2027, 1, "Q1FY27"),
            (2026, None, "FY26"),
            (2005, 4, "Q4FY05"),
            (2000, None, "FY00"),
        ],
    )
    def test_format(self, fy: int, q: int | None, expected: str) -> None:
        assert period_label(fy, q) == expected

    def test_quarter_defaults_to_none(self) -> None:
        assert period_label(2026) == "FY26"

    @pytest.mark.parametrize("q", [0, 5, -1])
    def test_rejects_bad_quarter(self, q: int) -> None:
        with pytest.raises(ValueError, match="fiscal_quarter"):
            period_label(2026, q)

    @pytest.mark.parametrize("fy", [26, 1999, 2100])
    def test_rejects_out_of_range_year(self, fy: int) -> None:
        with pytest.raises(ValueError, match="fiscal_year"):
            period_label(fy, 1)


class TestParsePeriodLabel:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Q2FY26", (2026, 2)),
            ("Q2 FY26", (2026, 2)),
            ("q2fy2026", (2026, 2)),
            ("FY26", (2026, None)),
            ("fy2026", (2026, None)),
            ("  Q1 FY27  ", (2027, 1)),
            ("Q4-FY25", (2025, 4)),
            ("Q3_FY24", (2024, 3)),
            ("FY'26", (2026, None)),
            ("Q2 FY 26", (2026, 2)),
            ("FY2025-26", (2026, None)),
            ("FY25-26", (2026, None)),
            ("Q3 FY2025-26", (2026, 3)),
            ("FY 2099-00", None),  # out of range year, checked separately below
        ],
    )
    def test_accepted_formats(self, label: str, expected: tuple[int, int | None] | None) -> None:
        if expected is None:
            with pytest.raises(ValueError):
                parse_period_label(label)
        else:
            assert parse_period_label(label) == expected

    @pytest.mark.parametrize(
        "label",
        [
            "",
            "Q5FY26",
            "Q0FY26",
            "Q2",
            "2026",
            "Q2FY",
            "Q2FY265",
            "H1FY26",
            "Q2FY26 extra",
            "FY2025-27",
            "FY25-27",
            "FY1999",
        ],
    )
    def test_rejects_invalid(self, label: str) -> None:
        with pytest.raises(ValueError):
            parse_period_label(label)

    @pytest.mark.parametrize(
        ("fy", "q"), [(2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, None), (2009, 3)]
    )
    def test_round_trip(self, fy: int, q: int | None) -> None:
        assert parse_period_label(period_label(fy, q)) == (fy, q)


class TestFiscalQuarterDateRange:
    @pytest.mark.parametrize(
        ("fy", "q", "start", "end"),
        [
            (2027, 1, date(2026, 4, 1), date(2026, 6, 30)),
            (2027, 2, date(2026, 7, 1), date(2026, 9, 30)),
            (2027, 3, date(2026, 10, 1), date(2026, 12, 31)),
            (2027, 4, date(2027, 1, 1), date(2027, 3, 31)),
            (2027, None, date(2026, 4, 1), date(2027, 3, 31)),
            (2026, 2, date(2025, 7, 1), date(2025, 9, 30)),
        ],
    )
    def test_ranges(self, fy: int, q: int | None, start: date, end: date) -> None:
        assert fiscal_quarter_date_range(fy, q) == (start, end)

    def test_quarters_are_contiguous_and_cover_the_year(self) -> None:
        ranges = [fiscal_quarter_date_range(2026, q) for q in (1, 2, 3, 4)]
        year_start, year_end = fiscal_quarter_date_range(2026)
        assert ranges[0][0] == year_start
        assert ranges[-1][1] == year_end
        for (_, prev_end), (next_start, _) in itertools.pairwise(ranges):
            assert (next_start - prev_end).days == 1

    def test_consistent_with_date_to_fiscal(self) -> None:
        for q in (1, 2, 3, 4):
            start, end = fiscal_quarter_date_range(2026, q)
            assert date_to_fiscal(start) == (2026, q)
            assert date_to_fiscal(end) == (2026, q)

    def test_rejects_bad_quarter(self) -> None:
        with pytest.raises(ValueError):
            fiscal_quarter_date_range(2026, 5)
