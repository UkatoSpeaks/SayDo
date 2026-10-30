"""Remove repeated page headers/footers and page numbers from extracted page text.

Indian concall transcripts repeat the company name, call title, date and a
"Page X of Y" line on every page. Left in, they split speaker turns mid-sentence
and pollute every chunk that crosses a page boundary.

Heuristics:
- Look at the first and last ``EDGE_LINES`` non-blank lines of each page.
- A line is boilerplate if (after lowercasing and replacing digits with ``#``)
  it shows up in that edge zone on at least half of the pages (min. 3 pages).
- Boilerplate and standalone page numbers are peeled off the top and bottom of
  each page until the first line that is neither. Lines in the body are never
  touched, except unambiguous "Page X of Y" lines.
- Lines that look like speaker labels ("Moderator:", "Rajesh Kumar:") are never
  treated as headers, even if many pages start with one.
"""

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

EDGE_LINES = 5
REPEAT_RATIO = 0.5
MIN_REPEAT_PAGES = 3

_PAGE_NUMBER_RE = re.compile(
    r"""^[\s\-\u2013\u2014(\[|]*
    (?:page\s*(?:no\.?|number)?\s*[|:.\-]?\s*)?
    \d{1,4}
    (?:\s*(?:of|/)\s*\d{1,4})?
    [\s\-\u2013\u2014)\]|]*$""",
    re.IGNORECASE | re.VERBOSE,
)
_EXPLICIT_PAGE_RE = re.compile(r"^\s*page\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")
_NON_WORD_RE = re.compile(r"[^\w#]+")


@dataclass(frozen=True)
class CleanedPages:
    pages: list[str]
    # Normalised keys of the lines detected as repeated headers/footers.
    boilerplate: frozenset[str] = field(default_factory=frozenset)


def _key(line: str) -> str:
    """Comparison key: lowercase, digits -> '#', punctuation and spacing collapsed."""
    text = _DIGITS_RE.sub("#", line.lower())
    return " ".join(_NON_WORD_RE.sub(" ", text).split())


def is_page_number(line: str) -> bool:
    return bool(_PAGE_NUMBER_RE.match(line.strip()))


def _looks_like_speaker_label(line: str) -> bool:
    stripped = line.strip()
    return stripped.endswith(":") or stripped.lower().startswith(("moderator", "operator"))


def _edge_indices(lines: list[str]) -> tuple[list[int], list[int]]:
    non_blank = [i for i, line in enumerate(lines) if line.strip()]
    return non_blank[:EDGE_LINES], non_blank[-EDGE_LINES:]


def detect_boilerplate(pages: Sequence[str]) -> frozenset[str]:
    """Return keys of lines repeated in the top/bottom zone of most pages."""
    if len(pages) < MIN_REPEAT_PAGES:
        return frozenset()

    counts: Counter[str] = Counter()
    for page in pages:
        lines = page.split("\n")
        top, bottom = _edge_indices(lines)
        keys = {
            _key(lines[i])
            for i in (*top, *bottom)
            if not _looks_like_speaker_label(lines[i]) and not is_page_number(lines[i])
        }
        counts.update(k for k in keys if k.strip("# "))

    threshold = max(MIN_REPEAT_PAGES, math.ceil(REPEAT_RATIO * len(pages)))
    return frozenset(key for key, n in counts.items() if n >= threshold)


def _is_removable_edge_line(line: str, boilerplate: frozenset[str]) -> bool:
    if _looks_like_speaker_label(line):
        return False
    return is_page_number(line) or _key(line) in boilerplate


def clean_page(text: str, boilerplate: frozenset[str]) -> str:
    lines = text.split("\n")
    keep = [True] * len(lines)

    for order in (range(len(lines)), range(len(lines) - 1, -1, -1)):
        inspected = 0
        for i in order:
            if not lines[i].strip():
                continue
            if inspected >= EDGE_LINES or not _is_removable_edge_line(lines[i], boilerplate):
                break
            keep[i] = False
            inspected += 1

    kept = [line for i, line in enumerate(lines) if keep[i] and not _EXPLICIT_PAGE_RE.match(line)]
    return "\n".join(kept).strip("\n")


def clean_pages(pages: Sequence[str]) -> CleanedPages:
    """Strip repeated headers/footers and page numbers; other text and line breaks stay."""
    boilerplate = detect_boilerplate(pages)
    return CleanedPages(pages=[clean_page(p, boilerplate) for p in pages], boilerplate=boilerplate)
