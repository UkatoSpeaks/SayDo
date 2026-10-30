"""Turn parsed documents into chunks sized for embedding.

Transcripts: one chunk per speaker turn when it fits in ``CHUNK_MAX_TOKENS``;
longer turns are split at paragraph/sentence boundaries into balanced pieces
with ~``CHUNK_OVERLAP_TOKENS`` of overlap, all keeping the turn's speaker, role
and section. Short moderator turns ("Thank you. Please go ahead.") are dropped.

Other documents: the same sentence-aware splitter over the whole document, with
page breaks as extra split points. Speaker is null, role unknown, section other.

Token counts use tiktoken ``cl100k_base`` as an approximation.
"""

import itertools
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.models import ChunkSection, DocType, SpeakerRole
from app.services.tokens import count_tokens
from app.services.transcript_parser import PagedText, ParsedTranscript

CHUNK_MAX_TOKENS = 500
# Packing budget per piece; the gap to the max absorbs token-count drift when
# sentences are joined.
CHUNK_TARGET_TOKENS = 480
CHUNK_OVERLAP_TOKENS = 50
# Extra room per piece when balancing sizes, so sentence granularity doesn't
# push a few tokens into an extra, tiny last piece.
BALANCE_SLACK_TOKENS = 25
# Sentences longer than this are split at word boundaries.
MAX_UNIT_TOKENS = 400
MIN_MODERATOR_TOKENS = 20
MIN_UNLABELLED_TOKENS = 20

_SENTENCE_END_RE = re.compile(r"[.!?][\"'\u201d\u2019)\]]*\s+|\n[ \t]*\n\s*")
_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "rs",
        "no",
        "nos",
        "vs",
        "sr",
        "jr",
        "st",
        "co",
        "ltd",
        "inc",
        "approx",
        "etc",
        "e.g",
        "i.e",
        "fig",
        "prof",
        "shri",
        "smt",
        "yoy",
        "qoq",
        "bn",
        "mn",
        "cr",
    }
)
_LAST_WORD_RE = re.compile(r"([\w.]+)\.$")

SECTION_LABELS = {
    ChunkSection.PREPARED_REMARKS: "Prepared remarks",
    ChunkSection.QNA: "Q&A",
    ChunkSection.OTHER: "Other",
}


@dataclass(frozen=True)
class ChunkDraft:
    text: str
    page_start: int
    page_end: int
    speaker: str | None
    speaker_role: SpeakerRole
    section: ChunkSection
    token_count: int


# ------------------------------------------------------------------ splitting


def _is_abbreviation(text: str, period_index: int) -> bool:
    if text[period_index] != ".":
        return False
    match = _LAST_WORD_RE.search(text, 0, period_index + 1)
    if match is None:
        return False
    word = match[1].lower()
    # Single letters are initials ("R. Kumar"); digits before a period are decimals.
    return word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha())


def _sentence_units(
    text: str, start: int, end: int, extra_breaks: Iterable[int] = ()
) -> list[tuple[int, int]]:
    """Split ``text[start:end]`` into sentence/paragraph spans (whitespace trimmed)."""
    cuts = {start, end}
    for match in _SENTENCE_END_RE.finditer(text, start, end):
        if not _is_abbreviation(text, match.start()):
            cuts.add(match.end())
    cuts.update(b for b in extra_breaks if start < b < end)

    units = []
    bounds = sorted(cuts)
    for a, b in itertools.pairwise(bounds):
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a < b:
            units.append((a, b))
    return units


def _split_by_words(text: str, start: int, end: int, max_tokens: int) -> list[tuple[int, int]]:
    pieces = []
    piece_start, tokens = None, 0
    last_end = start
    for word in re.finditer(r"\S+", text[start:end]):
        w_start, w_end = start + word.start(), start + word.end()
        w_tokens = count_tokens(" " + word.group())
        if piece_start is not None and tokens + w_tokens > max_tokens:
            pieces.append((piece_start, last_end))
            piece_start, tokens = None, 0
        if piece_start is None:
            piece_start = w_start
        tokens += w_tokens
        last_end = w_end
    if piece_start is not None:
        pieces.append((piece_start, last_end))
    return pieces


def _units(text: str, start: int, end: int, extra_breaks: Iterable[int]) -> list[tuple[int, int]]:
    units = []
    for a, b in _sentence_units(text, start, end, extra_breaks):
        if count_tokens(text[a:b]) > MAX_UNIT_TOKENS:
            units.extend(_split_by_words(text, a, b, MAX_UNIT_TOKENS))
        else:
            units.append((a, b))
    return units


def _overlap_start(text: str, piece_start: int, piece_end: int, unit_starts: list[int]) -> int:
    """Where the next piece starts: ~CHUNK_OVERLAP_TOKENS before ``piece_end``.

    Snaps forward to a sentence start when that still keeps at least half the overlap.
    """
    words = list(re.finditer(r"\S+", text[piece_start:piece_end]))
    candidate, tokens = piece_end, 0
    for word in reversed(words[1:]):  # never overlap the entire piece
        tokens += count_tokens(" " + word.group())
        candidate = piece_start + word.start()
        if tokens >= CHUNK_OVERLAP_TOKENS:
            break
    for unit_start in unit_starts:
        if candidate <= unit_start < piece_end:
            if count_tokens(text[unit_start:piece_end]) >= CHUNK_OVERLAP_TOKENS // 2:
                return unit_start
            break
    return candidate


def split_span(
    text: str, start: int, end: int, *, extra_breaks: Iterable[int] = ()
) -> list[tuple[int, int]]:
    """Split ``text[start:end]`` into pieces of at most ~CHUNK_MAX_TOKENS with overlap.

    Returns ``[(start, end)]`` (trimmed) when the span already fits.
    """
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return []
    total = count_tokens(text[start:end])
    if total <= CHUNK_MAX_TOKENS:
        return [(start, end)]

    units = _units(text, start, end, extra_breaks)
    # The joining space usually merges into the next token, so unit counts add up
    # to roughly the count of the joined text.
    unit_tokens = [count_tokens(text[a:b]) for a, b in units]
    # Spread content evenly instead of leaving a tiny last piece: with n pieces,
    # total = budget + (n - 1) * (budget - overlap). The slack absorbs pieces
    # ending a sentence short of the budget.
    step = CHUNK_TARGET_TOKENS - CHUNK_OVERLAP_TOKENS
    n_pieces = max(2, math.ceil((total - CHUNK_OVERLAP_TOKENS) / step))
    even = math.ceil((total + (n_pieces - 1) * CHUNK_OVERLAP_TOKENS) / n_pieces)
    budget = min(CHUNK_TARGET_TOKENS, even + BALANCE_SLACK_TOKENS)

    pieces: list[tuple[int, int]] = []
    i = 0
    piece_start, carried = units[0][0], 0
    while i < len(units):
        tokens, j = carried, i
        while j < len(units) and (j == i or tokens + unit_tokens[j] <= budget):
            tokens += unit_tokens[j]
            j += 1
        piece_end = units[j - 1][1]
        pieces.append((piece_start, piece_end))
        if j >= len(units):
            break
        unit_starts = [a for a, _ in units[i:j]]
        piece_start = _overlap_start(text, piece_start, piece_end, unit_starts)
        carried = count_tokens(text[piece_start:piece_end])
        i = j
    return pieces


# ------------------------------------------------------------------- chunking


def _draft(
    paged: PagedText,
    start: int,
    end: int,
    speaker: str | None,
    role: SpeakerRole,
    section: ChunkSection,
) -> ChunkDraft:
    text = paged.text[start:end]
    page_start, page_end = paged.page_range(start, end)
    return ChunkDraft(
        text=text,
        page_start=page_start,
        page_end=page_end,
        speaker=speaker,
        speaker_role=role,
        section=section,
        token_count=count_tokens(text),
    )


def chunk_transcript(parsed: ParsedTranscript) -> list[ChunkDraft]:
    paged = parsed.paged
    drafts: list[ChunkDraft] = []
    for turn in parsed.turns:
        tokens = count_tokens(parsed.turn_text(turn))
        if turn.role is SpeakerRole.MODERATOR and tokens < MIN_MODERATOR_TOKENS:
            continue  # "Thank you. Please go ahead."
        if turn.speaker is None and tokens < MIN_UNLABELLED_TOKENS:
            continue
        for start, end in split_span(paged.text, turn.start, turn.end):
            drafts.append(_draft(paged, start, end, turn.speaker, turn.role, turn.section))
    return drafts


def chunk_pages(pages: Sequence[str]) -> list[ChunkDraft]:
    """Fixed-size chunking for non-transcript documents (results, decks, reports)."""
    paged = PagedText.from_pages(pages)
    return [
        _draft(paged, start, end, None, SpeakerRole.UNKNOWN, ChunkSection.OTHER)
        for start, end in split_span(paged.text, 0, len(paged.text), extra_breaks=paged.page_starts)
    ]


def embedding_text(
    text: str,
    *,
    company: str,
    period_label: str,
    doc_type: DocType,
    speaker: str | None,
    speaker_role: SpeakerRole,
    section: ChunkSection,
) -> str:
    """Text sent to the embedding model: the chunk prefixed with its context."""
    who = (
        f"{speaker} ({speaker_role.value})"
        if speaker
        else doc_type.value.replace("_", " ").capitalize()
    )
    return f"[{company} | {period_label} | {who} | {SECTION_LABELS[section]}]\n{text}"
