import itertools

import pytest

from app.models import ChunkSection, DocType, SpeakerRole
from app.services.chunker import (
    CHUNK_MAX_TOKENS,
    CHUNK_OVERLAP_TOKENS,
    MIN_MODERATOR_TOKENS,
    ChunkDraft,
    chunk_pages,
    chunk_transcript,
    embedding_text,
    split_span,
)
from app.services.tokens import count_tokens
from app.services.transcript_cleaner import clean_pages
from app.services.transcript_parser import parse_transcript
from tests.concall_fixture import (
    COLON_SENTENCE,
    FILLER_TURN,
    LONG_ANSWER_FIRST,
    LONG_ANSWER_LAST,
    build_concall_pdf,
)
from tests.factories import extracted_pages


def _flat(text: str) -> str:
    return " ".join(text.split())


def _sentences(n: int, prefix: str = "Sentence") -> str:
    return " ".join(
        f"{prefix} number {i} talks about revenue growth and margins in FY27." for i in range(n)
    )


@pytest.fixture(scope="module")
def cleaned_pages(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    pages = extracted_pages(build_concall_pdf(), tmp_path_factory.mktemp("concall"))
    return clean_pages(pages).pages


@pytest.fixture(scope="module")
def chunks(cleaned_pages: list[str]) -> list[ChunkDraft]:
    return chunk_transcript(parse_transcript(cleaned_pages))


class TestSplitSpan:
    def test_short_text_is_one_piece(self) -> None:
        text = "  Short answer.  "
        assert split_span(text, 0, len(text)) == [(2, 15)]

    def test_empty(self) -> None:
        assert split_span("   ", 0, 3) == []

    def test_long_text_split_with_overlap(self) -> None:
        text = _sentences(120)
        pieces = split_span(text, 0, len(text))
        tokens = [count_tokens(text[a:b]) for a, b in pieces]

        assert len(pieces) >= 3
        assert all(t <= CHUNK_MAX_TOKENS for t in tokens)
        assert all(t >= 300 for t in tokens)  # balanced, no tiny tail
        assert pieces[0][0] == 0 and pieces[-1][1] == len(text)
        for (_, prev_end), (next_start, _) in itertools.pairwise(pieces):
            assert next_start < prev_end  # overlap
            overlap = count_tokens(text[next_start:prev_end])
            assert CHUNK_OVERLAP_TOKENS // 2 <= overlap <= CHUNK_OVERLAP_TOKENS + 15
            # Pieces start and end on sentence boundaries.
            assert text[next_start:].startswith("Sentence number")
            assert text[prev_end - 1] == "."

    def test_abbreviations_are_not_sentence_breaks(self) -> None:
        text = " ".join(
            f"Mr. R. Kumar said capex of Rs. {i} crore is planned vs. last year." for i in range(80)
        )
        for a, _b in split_span(text, 0, len(text)):
            assert text[a:].startswith("Mr. R. Kumar")

    def test_paragraph_breaks_are_split_points(self) -> None:
        paragraph = "Revenue grew and margins expanded in every segment we operate in"
        text = "\n\n".join(f"{paragraph} {i}" for i in range(90))
        for a, _ in split_span(text, 0, len(text))[1:]:
            assert text[a:].startswith(paragraph)

    def test_giant_sentence_is_split_by_words(self) -> None:
        text = " ".join(f"word{i}" for i in range(1500))
        pieces = split_span(text, 0, len(text))
        assert len(pieces) >= 3
        assert all(count_tokens(text[a:b]) <= CHUNK_MAX_TOKENS for a, b in pieces)
        assert pieces[-1][1] == len(text)


class TestTranscriptChunks:
    def test_one_chunk_per_turn_with_metadata(self, chunks: list[ChunkDraft]) -> None:
        speakers = [c.speaker for c in chunks]
        assert speakers[:5] == [None, "Moderator", "Vikram Rao", "Rajesh Kumar", "Anita Desai"]
        [colon_chunk] = [c for c in chunks if COLON_SENTENCE in _flat(c.text)]
        assert colon_chunk.speaker == "Rajesh Kumar"
        assert colon_chunk.speaker_role is SpeakerRole.MANAGEMENT
        assert colon_chunk.section is ChunkSection.PREPARED_REMARKS
        for chunk in chunks:
            assert 0 < chunk.token_count <= CHUNK_MAX_TOKENS
            assert chunk.token_count == count_tokens(chunk.text)

    def test_long_turn_split_with_overlap(self, chunks: list[ChunkDraft]) -> None:
        start = next(i for i, c in enumerate(chunks) if _flat(c.text).startswith(LONG_ANSWER_FIRST))
        end = next(i for i, c in enumerate(chunks) if _flat(c.text).endswith(LONG_ANSWER_LAST))
        pieces = chunks[start : end + 1]

        assert len(pieces) >= 2
        assert all(400 <= p.token_count <= CHUNK_MAX_TOKENS for p in pieces)
        assert {(p.speaker, p.speaker_role, p.section) for p in pieces} == {
            ("Rajesh Kumar", SpeakerRole.MANAGEMENT, ChunkSection.QNA)
        }
        for prev, nxt in itertools.pairwise(pieces):
            head = _flat(nxt.text)[:120]
            assert head[:60] in _flat(prev.text)  # next piece repeats the tail of the previous

    def test_filler_moderator_turns_dropped(self, chunks: list[ChunkDraft]) -> None:
        texts = [_flat(c.text) for c in chunks]
        assert FILLER_TURN not in texts
        assert "MR. ROHAN GUPTA – SKYLINE CAPITAL MARKETS" not in texts
        moderator = [c for c in chunks if c.speaker_role is SpeakerRole.MODERATOR]
        assert moderator and all(c.token_count >= MIN_MODERATOR_TOKENS for c in moderator)
        # Short analyst turns are kept: only moderator filler is dropped.
        assert "Understood. That is helpful, thank you." in texts

    def test_cover_page_chunk(self, chunks: list[ChunkDraft]) -> None:
        cover = chunks[0]
        assert (cover.speaker, cover.speaker_role, cover.section) == (
            None,
            SpeakerRole.UNKNOWN,
            ChunkSection.OTHER,
        )
        assert cover.text.startswith("MANAGEMENT:")

    def test_page_ranges(self, chunks: list[ChunkDraft], cleaned_pages: list[str]) -> None:
        for chunk in chunks:
            lines = chunk.text.split("\n")
            assert lines[0] in cleaned_pages[chunk.page_start - 1]
            assert lines[-1] in cleaned_pages[chunk.page_end - 1]
            assert chunk.page_start <= chunk.page_end

        start = next(c for c in chunks if _flat(c.text).startswith(LONG_ANSWER_FIRST))
        end = next(c for c in chunks if _flat(c.text).endswith(LONG_ANSWER_LAST))
        first_page = next(i for i, p in enumerate(cleaned_pages, 1) if LONG_ANSWER_FIRST[:40] in p)
        last_page = next(i for i, p in enumerate(cleaned_pages, 1) if "range of 19% to 20%." in p)
        assert start.page_start == first_page
        assert end.page_end == last_page
        assert start.page_end > start.page_start  # this piece crosses a page break
        assert any(
            c.page_start == len(cleaned_pages) or c.page_end == len(cleaned_pages) for c in chunks
        )


class TestPageChunks:
    def test_fixed_size_chunks_track_pages(self) -> None:
        pages = [_sentences(30, prefix=f"Page{n}") for n in range(1, 6)]
        chunks = chunk_pages(pages)

        assert len(chunks) >= 3
        for chunk in chunks:
            assert chunk.speaker is None
            assert chunk.speaker_role is SpeakerRole.UNKNOWN
            assert chunk.section is ChunkSection.OTHER
            assert chunk.token_count <= CHUNK_MAX_TOKENS
            first_word = chunk.text.split()[0]
            last_word = chunk.text.split()[-2]
            assert first_word in pages[chunk.page_start - 1]
            assert last_word in pages[chunk.page_end - 1]
        assert chunks[0].page_start == 1
        assert chunks[-1].page_end == 5

    def test_short_document_is_one_chunk(self) -> None:
        [chunk] = chunk_pages(["Revenue Rs. 1,250 crore.", "EBITDA Rs. 242 crore."])
        assert (chunk.page_start, chunk.page_end) == (1, 2)

    def test_blank_document(self) -> None:
        assert chunk_pages(["", "  "]) == []


def test_embedding_text_prefix() -> None:
    text = embedding_text(
        "We expect 15% growth.",
        company="Acme Industries Limited",
        period_label="Q2FY26",
        doc_type=DocType.TRANSCRIPT,
        speaker="Rajesh Kumar",
        speaker_role=SpeakerRole.MANAGEMENT,
        section=ChunkSection.QNA,
    )
    assert text == (
        "[Acme Industries Limited | Q2FY26 | Rajesh Kumar (management) | Q&A]\n"
        "We expect 15% growth."
    )
    other = embedding_text(
        "Revenue table",
        company="Acme",
        period_label="FY26",
        doc_type=DocType.ANNUAL_REPORT,
        speaker=None,
        speaker_role=SpeakerRole.UNKNOWN,
        section=ChunkSection.OTHER,
    )
    assert other == "[Acme | FY26 | Annual report | Other]\nRevenue table"
