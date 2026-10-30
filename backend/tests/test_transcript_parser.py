from pathlib import Path

import pytest

from app.models import ChunkSection, SpeakerRole
from app.services.transcript_cleaner import clean_pages
from app.services.transcript_parser import (
    PagedText,
    ParsedTranscript,
    announces_qna,
    find_analyst_intros,
    match_name,
    parse_participants,
    parse_transcript,
)
from tests.concall_fixture import (
    ANALYSTS,
    COLON_SENTENCE,
    EXPECTED_PARTICIPANTS,
    LONG_ANSWER_FIRST,
    build_concall_pdf,
)
from tests.factories import extracted_pages


@pytest.fixture(scope="module")
def cleaned_pages(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    pages = extracted_pages(build_concall_pdf(), tmp_path_factory.mktemp("concall"))
    return clean_pages(pages).pages


@pytest.fixture(scope="module")
def parsed(cleaned_pages: list[str]) -> ParsedTranscript:
    return parse_transcript(cleaned_pages)


def _flat(text: str) -> str:
    return " ".join(text.split())


class TestPagedText:
    def test_offsets_map_to_pages(self) -> None:
        paged = PagedText.from_pages(["abc", "de", "", "fgh"])
        assert paged.text == "abc\nde\n\nfgh"
        assert [paged.page_at(i) for i in range(len(paged.text))] == [
            1,
            1,
            1,
            1,
            2,
            2,
            2,
            3,
            4,
            4,
            4,
        ]
        assert paged.page_range(2, 6) == (1, 2)
        assert paged.offset_of_page(4) == 8
        assert paged.offset_of_page(9) == len(paged.text)


class TestParticipants:
    def test_inline_list_with_commas(self) -> None:
        text = "Management: Mr. Rajesh Kumar – Managing Director, Mr. Amit Shah – CFO"
        assert parse_participants(text) == {"Rajesh Kumar": "Managing Director", "Amit Shah": "CFO"}

    def test_names_and_titles_spread_across_lines(self) -> None:
        text = (
            "Acme Ltd\nCOMPANY PARTICIPANTS:\nMR. RAJESH\nKUMAR — MANAGING DIRECTOR AND\n"
            "CHIEF EXECUTIVE OFFICER\nDR. A. K. IYER - CHIEF FINANCIAL OFFICER\n\n"
            "Moderator: Ladies and gentlemen, welcome."
        )
        assert parse_participants(text) == {
            "Rajesh Kumar": "Managing Director and Chief Executive Officer",
            "A. K. Iyer": "Chief Financial Officer",
        }

    def test_without_honorifics(self) -> None:
        text = "Management:\nRajesh Kumar – Managing Director\nAmit Shah – Chief Financial Officer"
        assert parse_participants(text) == {
            "Rajesh Kumar": "Managing Director",
            "Amit Shah": "Chief Financial Officer",
        }

    def test_title_with_comma_stays_together(self) -> None:
        text = "Management: Ms. Neha Rao – Executive Director, Finance; Mr. Ravi Iyer – CFO"
        assert parse_participants(text) == {
            "Neha Rao": "Executive Director, Finance",
            "Ravi Iyer": "CFO",
        }

    def test_no_list(self) -> None:
        assert parse_participants("Moderator: Welcome to the call.") == {}

    def test_fixture(self, parsed: ParsedTranscript) -> None:
        assert parsed.participants == EXPECTED_PARTICIPANTS


class TestNameMatching:
    @pytest.mark.parametrize(
        "label",
        ["Rajesh Kumar", "RAJESH KUMAR", "Mr. Rajesh Kumar", "R. Kumar", "Rajesh Kumaar"],
    )
    def test_matches(self, label: str) -> None:
        assert match_name(label, ["Rajesh Kumar", "Amit Shah"]) == "Rajesh Kumar"

    def test_matches_longer_name(self) -> None:
        assert match_name("Rajesh Kumar", ["Rajesh Kumar Sharma"]) == "Rajesh Kumar Sharma"

    @pytest.mark.parametrize("label", ["Rajesh Mehta", "Kumar", "The guidance"])
    def test_no_match(self, label: str) -> None:
        assert match_name(label, ["Rajesh Kumar", "Amit Shah"]) is None


class TestModeratorCues:
    def test_analyst_introductions(self) -> None:
        text = (
            "Thank you. The first question is from the line of Priya Shah from Kotak "
            "Institutional Equities. Please go ahead. Next we have a follow-up from Mr. Ravi "
            "Iyer of Axis Capital. The next question comes from Nikhil Jain with HDFC "
            "Securities. The next question is from the line of Arun Rao. Please go ahead."
        )
        assert find_analyst_intros(text) == {
            "Priya Shah": "Kotak Institutional Equities",
            "Ravi Iyer": "Axis Capital",
            "Nikhil Jain": "HDFC Securities",
            "Arun Rao": None,
        }

    @pytest.mark.parametrize(
        "text",
        [
            "We will now begin the question-and-answer session.",
            "We will now begin the Q&A.",
            "We shall now open the floor for Q & A.",
            "The first question is from the line of X from Y.",
            "We have the next question from Ravi.",
        ],
    )
    def test_qna_announcements(self, text: str) -> None:
        assert announces_qna(text)

    @pytest.mark.parametrize(
        "text",
        [
            "There will be an opportunity for you to ask questions after the presentation.",
            "After the presentation there will be a question-and-answer session.",
            "I now hand the conference over to Mr. Kumar.",
        ],
    )
    def test_not_qna_announcements(self, text: str) -> None:
        assert not announces_qna(text)


class TestSpeakerTurns:
    def test_speakers_and_roles(self, parsed: ParsedTranscript) -> None:
        roles = {t.speaker: t.role for t in parsed.turns}
        assert roles == {
            None: SpeakerRole.UNKNOWN,  # the cover page before the first speaker
            "Moderator": SpeakerRole.MODERATOR,
            "Vikram Rao": SpeakerRole.MANAGEMENT,
            "Rajesh Kumar": SpeakerRole.MANAGEMENT,
            "Anita Desai": SpeakerRole.MANAGEMENT,
            "Priya Shah": SpeakerRole.ANALYST,
            "Nikhil Jain": SpeakerRole.ANALYST,
            "Sanjay Mehta": SpeakerRole.ANALYST,  # speaks once, known from the introduction
        }
        assert parsed.analysts == ANALYSTS

    def test_turn_order(self, parsed: ParsedTranscript) -> None:
        speakers = [t.speaker for t in parsed.turns]
        assert speakers[:6] == [
            None,
            "Moderator",
            "Moderator",
            "Vikram Rao",
            "Rajesh Kumar",
            "Anita Desai",
        ]
        assert speakers[-2:] == ["Rajesh Kumar", "Moderator"]
        assert len(speakers) == 21

    def test_colon_sentence_is_not_a_speaker(self, parsed: ParsedTranscript) -> None:
        assert "Key highlights for the quarter" not in {t.speaker for t in parsed.turns}
        [turn] = [t for t in parsed.turns if COLON_SENTENCE in _flat(parsed.turn_text(t))]
        assert turn.speaker == "Rajesh Kumar"
        assert turn.section is ChunkSection.PREPARED_REMARKS

    def test_inline_label_text(self, parsed: ParsedTranscript) -> None:
        texts = [parsed.turn_text(t) for t in parsed.turns if t.speaker == "Priya Shah"]
        assert texts[1] == "Understood. That is helpful, thank you."

    def test_turn_text_excludes_label(self, parsed: ParsedTranscript) -> None:
        for turn in parsed.turns:
            text = parsed.turn_text(turn)
            assert text == text.strip()
            if turn.speaker:
                assert not text.startswith(f"{turn.speaker}:")

    def test_section_switches_at_moderator_qna_cue(self, parsed: ParsedTranscript) -> None:
        sections = [t.section for t in parsed.turns if t.speaker is not None]
        first_qna = sections.index(ChunkSection.QNA)
        assert set(sections[:first_qna]) == {ChunkSection.PREPARED_REMARKS}
        assert set(sections[first_qna:]) == {ChunkSection.QNA}
        cue = [t for t in parsed.turns if t.speaker is not None][first_qna]
        assert cue.role is SpeakerRole.MODERATOR
        assert "question-and-answer session" in parsed.turn_text(cue)
        # The CFO's "we can open the floor for questions" does not switch the section.
        cfo = next(t for t in parsed.turns if t.speaker == "Anita Desai")
        assert cfo.section is ChunkSection.PREPARED_REMARKS

    def test_long_answer_spans_pages(
        self, parsed: ParsedTranscript, cleaned_pages: list[str]
    ) -> None:
        [turn] = [t for t in parsed.turns if parsed.turn_text(t).startswith(LONG_ANSWER_FIRST[:40])]
        start_page, end_page = parsed.paged.page_range(turn.start, turn.end)
        assert end_page > start_page
        assert LONG_ANSWER_FIRST[:40] in cleaned_pages[start_page - 1]

    def test_unknown_role_for_unlisted_repeated_speaker(self) -> None:
        pages = [
            "Moderator:\nWelcome to the call. I hand over to the management.\n"
            "Suresh Nair:\nThank you. Revenue grew well.\n"
            "Moderator:\nThe first question is from the line of Ravi Iyer from Axis Capital.\n"
            "Ravi Iyer:\nWhat about margins?\nSuresh Nair:\nMargins improved."
        ]
        parsed = parse_transcript(pages)
        roles = [(t.speaker, t.role, t.section) for t in parsed.turns]
        assert roles == [
            ("Moderator", SpeakerRole.MODERATOR, ChunkSection.PREPARED_REMARKS),
            ("Suresh Nair", SpeakerRole.UNKNOWN, ChunkSection.PREPARED_REMARKS),
            ("Moderator", SpeakerRole.MODERATOR, ChunkSection.QNA),
            ("Ravi Iyer", SpeakerRole.ANALYST, ChunkSection.QNA),
            ("Suresh Nair", SpeakerRole.UNKNOWN, ChunkSection.QNA),
        ]

    def test_single_unknown_label_with_colon_is_text(self) -> None:
        pages = [
            "Moderator:\nWelcome.\nOur strategy is simple: grow profitably.\nModerator:\nThank you."
        ]
        parsed = parse_transcript(pages)
        assert [t.speaker for t in parsed.turns] == ["Moderator", "Moderator"]
        assert "Our strategy is simple: grow" in parsed.turn_text(parsed.turns[0])

    @pytest.mark.parametrize(
        "line",
        [
            "Revenue for Q2 FY26: Rs. 1,250 crore",  # digits
            "As we discussed in the last call with all of you: margins",  # > 6 words
            "https://example.com",
            "note: lowercase start",
        ],
    )
    def test_lines_that_are_never_labels(self, line: str) -> None:
        pages = [f"Moderator:\nHello.\n{line}\n{line}\nModerator:\nBye now."]
        parsed = parse_transcript(pages)
        assert [t.speaker for t in parsed.turns] == ["Moderator", "Moderator"]

    def test_no_labels_gives_single_unlabelled_turn(self, tmp_path: Path) -> None:
        parsed = parse_transcript(["Just some text.", "More text."])
        assert [(t.speaker, t.section) for t in parsed.turns] == [(None, ChunkSection.OTHER)]
