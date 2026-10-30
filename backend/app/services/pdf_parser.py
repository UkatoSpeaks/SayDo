"""Page-by-page PDF text extraction with PyMuPDF."""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pymupdf
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentPage, DocumentStatus

logger = logging.getLogger(__name__)

# A page with fewer non-whitespace characters than this is treated as "no text".
LOW_TEXT_CHAR_THRESHOLD = 25
# Warn that the PDF is probably scanned when at least this share of pages has no text.
SCANNED_PAGE_RATIO = 0.5

_CHAR_REPLACEMENTS = str.maketrans(
    {
        "\x00": "",
        "\N{SOFT HYPHEN}": "",
        "\N{ZERO WIDTH SPACE}": "",
        "\N{ZERO WIDTH NO-BREAK SPACE}": "",  # BOM
        "\t": " ",
        "\f": "\n",
        "\v": "\n",
        "\u2028": "\n",  # line separator
        "\u2029": "\n",  # paragraph separator
    }
)
_HORIZONTAL_SPACE_RE = re.compile(r"[^\S\n]+")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


class PdfParseError(Exception):
    pass


@dataclass(frozen=True)
class ExtractedPage:
    page_number: int
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def is_low_text(self) -> bool:
        return sum(not c.isspace() for c in self.text) < LOW_TEXT_CHAR_THRESHOLD


@dataclass(frozen=True)
class ExtractedPdf:
    pages: list[ExtractedPage]
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)


def normalize_text(raw: str) -> str:
    """Normalise whitespace while preserving line structure.

    Speaker names in transcripts sit on their own lines, so newlines are kept;
    runs of spaces collapse, lines are trimmed, and 3+ blank lines become one.
    """
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_CHAR_REPLACEMENTS)
    lines = (_HORIZONTAL_SPACE_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = "\n".join(lines)
    return _EXCESS_BLANK_LINES_RE.sub("\n\n", text).strip()


def _scanned_warning(pages: list[ExtractedPage]) -> str | None:
    low = [p.page_number for p in pages if p.is_low_text]
    if not pages or len(low) / len(pages) < SCANNED_PAGE_RATIO:
        return None
    shown = ", ".join(map(str, low[:20])) + (", ..." if len(low) > 20 else "")
    return (
        f"Likely scanned PDF: {len(low)} of {len(pages)} pages have almost no "
        f"extractable text (pages {shown}). OCR may be required."
    )


def extract_pdf(path: Path) -> ExtractedPdf:
    """Extract normalised text for every page. Blocking; run it in a worker thread."""
    try:
        # PyMuPDF ships only partial type hints.
        doc: Any = pymupdf.open(path, filetype="pdf")  # type: ignore[no-untyped-call]
    except Exception as exc:
        raise PdfParseError(f"Could not open PDF: {exc}") from exc

    with doc:
        if doc.needs_pass:
            raise PdfParseError("PDF is password-protected.")
        if doc.page_count == 0:
            raise PdfParseError("PDF has no pages.")
        try:
            pages = [
                ExtractedPage(page_number=index + 1, text=normalize_text(page.get_text("text")))
                for index, page in enumerate(doc)
            ]
        except Exception as exc:
            raise PdfParseError(f"Failed to extract text: {exc}") from exc

    warning = _scanned_warning(pages)
    return ExtractedPdf(pages=pages, warnings=[warning] if warning else [])


async def parse_document(session: AsyncSession, document: Document, pdf_path: Path) -> None:
    """Extract pages into ``document_pages`` and update the document's status.

    On failure the document is marked ``failed`` with an error message. Warnings
    (e.g. a likely scanned PDF) are recorded in ``error_message`` but the
    document is still marked ``parsed``. The caller owns the transaction.
    """
    try:
        extracted = await anyio.to_thread.run_sync(extract_pdf, pdf_path)
    except PdfParseError as exc:
        logger.warning("Parsing document %s failed: %s", document.id, exc)
        document.status = DocumentStatus.FAILED
        document.error_message = str(exc)
        await session.flush()
        return

    await session.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
    session.add_all(
        DocumentPage(
            document_id=document.id,
            page_number=page.page_number,
            text=page.text,
            char_count=page.char_count,
        )
        for page in extracted.pages
    )
    document.page_count = extracted.page_count
    document.status = DocumentStatus.PARSED
    document.error_message = "\n".join(extracted.warnings) or None
    await session.flush()
    logger.info("Parsed document %s: %d pages", document.id, extracted.page_count)
