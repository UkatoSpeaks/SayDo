from pathlib import Path

import pytest

from app.services.pdf_parser import PdfParseError, extract_pdf, normalize_text
from tests.factories import TRANSCRIPT_PAGES, make_pdf


class TestNormalizeText:
    def test_collapses_spaces_but_keeps_line_breaks(self) -> None:
        raw = "Moderator:  \n   Welcome   to\tthe call.\r\nRahul Mehta:\rThanks."
        assert normalize_text(raw) == "Moderator:\nWelcome to the call.\nRahul Mehta:\nThanks."

    def test_limits_blank_lines_to_one(self) -> None:
        assert normalize_text("a\n\n\n\n\nb\n \n \nc") == "a\n\nb\n\nc"

    def test_strips_invisible_characters_and_nul(self) -> None:
        raw = "Rev\N{SOFT HYPHEN}enue\x00 grew\N{ZERO WIDTH SPACE} 12%\N{NO-BREAK SPACE}YoY"
        assert normalize_text(raw) == "Revenue grew 12% YoY"

    def test_unicode_compatibility_forms(self) -> None:
        assert normalize_text("\N{LATIN SMALL LIGATURE FI}scal") == "fiscal"

    def test_empty(self) -> None:
        assert normalize_text("  \n\n \t ") == ""


class TestExtractPdf:
    def test_extracts_each_page_in_order(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(make_pdf(TRANSCRIPT_PAGES))

        result = extract_pdf(path)

        assert result.page_count == 3
        assert [p.page_number for p in result.pages] == [1, 2, 3]
        assert "Moderator:\nLadies and gentlemen" in result.pages[1].text
        assert "We aim to reach 20% EBITDA margin by Q4 FY27." in result.pages[2].text
        assert result.warnings == []

    def test_flags_likely_scanned_pdf(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.pdf"
        path.write_bytes(make_pdf(["", "", "Some real text on this page for sure.", ""]))

        result = extract_pdf(path)

        assert result.page_count == 4
        assert len(result.warnings) == 1
        assert "Likely scanned PDF: 3 of 4 pages" in result.warnings[0]
        assert "pages 1, 2, 4" in result.warnings[0]

    def test_occasional_blank_page_is_not_flagged(self, tmp_path: Path) -> None:
        path = tmp_path / "mostly-text.pdf"
        path.write_bytes(make_pdf([*TRANSCRIPT_PAGES, ""]))
        assert extract_pdf(path).warnings == []

    def test_corrupt_pdf_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.pdf"
        path.write_bytes(b"%PDF-1.7\nthis is not really a pdf")
        with pytest.raises(PdfParseError):
            extract_pdf(path)
