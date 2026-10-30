from pathlib import Path

import pytest

from app.services.transcript_cleaner import clean_pages, detect_boilerplate, is_page_number
from tests.concall_fixture import CALL_DATE, CALL_TITLE, COMPANY, build_concall_pdf
from tests.factories import extracted_pages

# Distinct bodies: lines that differ only in digits count as repeated boilerplate.
_BODIES = [
    "Revenue grew strongly this quarter.\nMargins held up well.",
    "Exports were the main driver.\nDomestic demand was steady.",
    "We commissioned a new plant.\nUtilisation is ramping up.",
    "Net debt came down again.\nWorking capital improved.",
    "Thank you all for joining.\nWe look forward to next quarter.",
]


def _page(n: int, total: int, body: str) -> str:
    return f"Acme Industries Limited\nQ2 FY26 Earnings Call\n{body}\nPage {n} of {total}"


class TestPageNumbers:
    @pytest.mark.parametrize(
        "line", ["3", " 12 ", "Page 3", "page 3 of 12", "- 4 -", "3/12", "(7)"]
    )
    def test_recognised(self, line: str) -> None:
        assert is_page_number(line)

    @pytest.mark.parametrize("line", ["Revenue grew 18%", "Q2 FY26", "Rs. 1,250 crore", "12345"])
    def test_not_page_numbers(self, line: str) -> None:
        assert not is_page_number(line)


class TestCleanPages:
    def test_removes_repeated_header_and_footer(self) -> None:
        pages = [_page(n, 4, _BODIES[n - 1]) for n in range(1, 5)]
        cleaned = clean_pages(pages)
        assert cleaned.pages == _BODIES[:4]
        assert "acme industries limited" in cleaned.boilerplate

    def test_header_with_varying_numbers_is_detected(self) -> None:
        # "Page X of Y" style lines differ only in their digits.
        pages = [f"Transcript - Page {n}\n{body}" for n, body in enumerate(_BODIES, start=1)]
        assert clean_pages(pages).pages == _BODIES

    def test_standalone_page_numbers_removed(self) -> None:
        pages = ["Intro text.\n1", "2\nMore text.", "Final words.\n\n3"]
        assert clean_pages(pages).pages == ["Intro text.", "More text.", "Final words."]

    def test_keeps_body_lines_and_line_breaks(self) -> None:
        body = (
            "Rajesh Kumar:\nRevenue grew.\n\nAcme Industries Limited\n15\n"
            "is growing.\nMore lines\nfollow here."
        )
        pages = [_page(n, 3, body if n == 2 else _BODIES[n]) for n in range(1, 4)]
        # The company name inside the body and a number mid-page stay.
        assert clean_pages(pages).pages[1] == body

    def test_speaker_label_at_top_of_every_page_is_kept(self) -> None:
        pages = [f"Moderator:\n{body}" for body in _BODIES]
        assert detect_boilerplate(pages) == frozenset()
        assert clean_pages(pages).pages == pages

    def test_line_on_minority_of_pages_is_kept(self) -> None:
        pages = ["Special note\nBody one.", "Body two.", "Body three.", "Body four."]
        assert clean_pages(pages).pages == pages

    def test_too_few_pages_for_detection(self) -> None:
        pages = ["Header\nOne.", "Header\nTwo."]
        assert clean_pages(pages).pages == pages


def test_fixture_headers_and_footers_removed(tmp_path: Path) -> None:
    pages = extracted_pages(build_concall_pdf(), tmp_path)
    assert len(pages) >= 5
    assert all(p.startswith(COMPANY) for p in pages)

    cleaned = clean_pages(pages).pages

    for text in cleaned:
        for boilerplate in (COMPANY, CALL_TITLE, CALL_DATE):
            assert boilerplate not in text.split("\n")
        assert "Page " not in text.split("\n")[-1]
    # The company name inside sentences is untouched.
    assert "Call of Acme Industries Limited" in " ".join(cleaned[0].split())
    assert cleaned[0].startswith("MANAGEMENT:")
