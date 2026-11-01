"""Split a cleaned concall transcript into speaker turns.

Works on the cleaned pages joined into one string (``PagedText``) and keeps a
page-offset table, so any character range maps back to its page numbers.

Heuristics (see the README for the full list):
- Participants: a "Management:" / "Company Participants:" / "Corporate
  Participants" / "Conference Call Participants" block on pages 1-2, entries
  split on honorifics (Mr./Ms./Dr./...) or dashes, names may wrap lines. A block
  of bare lines alternating name / job title is paired line by line.
- Analysts: an "Analysts" block on pages 1-2 ends the management block; its
  "Name - Firm" entries or alternating name / firm lines seed the analyst list.
- Speaker label, two layouts:
  - ``<label>:`` at the start of a line, with at most 6 words, no digits,
    starting with a capital letter. It counts only if it is "Moderator"/
    "Operator", a known participant, a name the moderator introduced as asking
    a question, or a label that appears more than once.
  - A short line on its own, no colon (Infosys style), followed by the speech on
    the next lines. It counts only if it is "Moderator"/"Operator" or a known
    participant or analyst name, and never inside the participant lists.
- Roles: moderator > management (fuzzy name match, honorifics ignored) >
  analyst (introduced by the moderator) > unknown.
- Section: prepared_remarks until a moderator turn announces the Q&A; qna after.
"""

import bisect
import difflib
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from app.models import ChunkSection, SpeakerRole

MAX_LABEL_WORDS = 6
PARTICIPANT_PAGES = 2
MAX_PARTICIPANT_BLOCK_LINES = 30
FUZZY_NAME_RATIO = 0.85
MODERATOR_LABELS = frozenset({"moderator", "operator"})

_HONORIFIC = r"(?:mr|mrs|ms|miss|dr|shri|sri|smt|prof)"
_HONORIFIC_PREFIX_RE = re.compile(rf"^(?:{_HONORIFIC}\b\.?\s*)+", re.IGNORECASE)
_HONORIFIC_SPLIT_RE = re.compile(rf"(?=\b{_HONORIFIC}\b\.?\s)", re.IGNORECASE)
_DASH_RE = re.compile(r"\s*[\u2013\u2014]\s*|\s+-\s+")
# Inside an entry, ", Amit Shah - CFO" starts another participant without an honorific.
_ENTRY_SPLIT_RE = re.compile(r"[,;]\s*(?=[^,;\u2013\u2014]{2,60}?\s[-\u2013\u2014]\s)")
_SMALL_WORDS = frozenset({"and", "of", "the", "for", "&", "in"})
# Kept upper-case when title-casing an ALL CAPS job title.
_ACRONYMS = frozenset(
    {"ceo", "cfo", "coo", "cto", "cio", "cmd", "md", "ed", "ir", "hr", "vp", "svp", "evp"}
    | {"avp", "gm", "cs", "chro", "cso", "cxo", "r&d", "it", "esg"}
)

# "<label>:" at the start of a line; the label is letters plus a little punctuation.
_LABEL_RE = re.compile(r"^(?P<label>[^\W\d_][^:\n]{0,79}?)\s*:(?!//)[ \t]*(?P<rest>.*)$")
_LABEL_CHARS_RE = re.compile(r"^(?:[^\W\d_]|[ .'\u2019\-\u2013\u2014&,()])+$")
_LABEL_NAME_SPLIT_RE = re.compile(r"\s+[-\u2013\u2014]\s+|\s*[\u2013\u2014(,]\s*")

_PARTICIPANT_HEADING_RE = re.compile(
    r"^(?:(?:company|corporate)\s+participants?|conference\s+call\s+participants?"
    r"|management(?:\s+participants?|\s+team)?"
    r"|participants?\s+from\s+(?:the\s+)?management|key\s+management(?:\s+personnel)?"
    r"|speakers?)\s*[:\-\u2013]?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_ANALYST_HEADING_RE = re.compile(
    r"^(?:analysts?|analyst\s+participants?|participating\s+analysts?)\s*[:\-\u2013]?\s*"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)
# Words that mark a line as a job title, for name / title blocks without separators.
_TITLE_WORD_RE = re.compile(
    r"\b(?:officer|director|chairman|chairperson|chair|founder|co-founder|president|head"
    r"|controller|manager|secretary|treasurer|partner|executive|chief|lead|counsel|relations"
    r"|ceo|cfo|coo|cto|cio|md|vp|svp|evp|avp|gm|cs|ir|chro)\b",
    re.IGNORECASE,
)
_SENTENCE_END = tuple(".,;?!")
_ANY_HEADING_RE = re.compile(r"^[^\W\d_][^:]{0,40}:")
_PROSE_WORDS = 15

_NAME = r"[A-Z][\w.'\u2019-]*(?:\s+[A-Z][\w.'\u2019-]*){0,4}?"
_ANALYST_INTRO_RE = re.compile(
    r"\b(?:question|questions|follow[-\s]?up)\s+(?:is\s+|comes\s+|will\s+be\s+)?from\s+"
    r"(?:the\s+line\s+of\s+)?"
    rf"(?P<name>(?:(?:Mr|Ms|Mrs|Dr)\.?\s+)?{_NAME})"
    r"(?:\s+(?:from|of|with)\s+(?P<firm>[A-Z][^.?!;]*?))?"
    r"(?=\s*[.,;?!]|\s+(?i:please|go\s+ahead)\b|\s*$)"
)
_QNA_START_RE = re.compile(
    r"\b(?:now|begin|start|commence|open)\w*\b[^.?!]{0,60}?"
    r"\b(?:question[-\s]+and[-\s]+answer|Q\s*&\s*A|Q\s+and\s+A)\b"
    r"|\b(?:first|next)\s+question\s+(?:is\s+|comes\s+)?from\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PagedText:
    """Pages joined with newlines, plus the offset where each page starts."""

    text: str
    page_starts: tuple[int, ...]

    @classmethod
    def from_pages(cls, pages: Sequence[str]) -> "PagedText":
        starts: list[int] = []
        offset = 0
        for page in pages:
            starts.append(offset)
            offset += len(page) + 1  # the joining newline belongs to the page before it
        return cls(text="\n".join(pages), page_starts=tuple(starts))

    def page_at(self, offset: int) -> int:
        """1-based page number of the character at ``offset``."""
        return max(1, bisect.bisect_right(self.page_starts, offset))

    def page_range(self, start: int, end: int) -> tuple[int, int]:
        """Pages covered by ``text[start:end]`` (``end`` exclusive)."""
        return self.page_at(start), self.page_at(max(start, end - 1))

    def offset_of_page(self, page_number: int) -> int:
        """Offset where page ``page_number`` (1-based) starts, or len(text) past the end."""
        if page_number > len(self.page_starts):
            return len(self.text)
        return self.page_starts[page_number - 1]


@dataclass(frozen=True)
class SpeakerTurn:
    speaker: str | None
    role: SpeakerRole
    section: ChunkSection
    # Body (without the speaker label) as offsets into PagedText.text; end is exclusive.
    start: int
    end: int


@dataclass(frozen=True)
class ParsedTranscript:
    paged: PagedText
    turns: list[SpeakerTurn]
    participants: dict[str, str] = field(default_factory=dict)  # name -> title
    analysts: dict[str, str | None] = field(default_factory=dict)  # name -> firm

    def turn_text(self, turn: SpeakerTurn) -> str:
        return self.paged.text[turn.start : turn.end]


# --------------------------------------------------------------------------- names


def _smart_title(text: str, *, keep_acronyms: bool = False) -> str:
    """Title-case text that is ALL CAPS; leave mixed-case text alone."""
    if not text.isupper():
        return text
    words = []
    for i, word in enumerate(text.lower().split()):
        if keep_acronyms and word.strip(",;.()") in _ACRONYMS:
            words.append(word.upper())
        elif i and word in _SMALL_WORDS:
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def strip_honorifics(name: str) -> str:
    return _HONORIFIC_PREFIX_RE.sub("", name.strip()).strip()


def normalize_name(name: str) -> str:
    """Comparison key: honorifics removed, lowercase, punctuation dropped."""
    base = strip_honorifics(name).lower()
    return " ".join(re.sub(r"[^\w\s]", " ", base).split())


def _tokens_match(small: list[str], large: list[str]) -> bool:
    """Every token in ``small`` matches a distinct token in ``large`` (initials allowed)."""
    remaining = list(large)
    for token in small:
        for i, candidate in enumerate(remaining):
            if token == candidate or (len(token) == 1 and candidate.startswith(token)):
                del remaining[i]
                break
        else:
            return False
    return True


def match_name(name: str, candidates: Iterable[str]) -> str | None:
    """Return the candidate that ``name`` refers to, ignoring honorifics and case.

    Matches exact keys, token subsets of two or more tokens ("Rajesh Kumar" vs
    "Rajesh Kumar Sharma", "R. Kumar" vs "Rajesh Kumar"), and near-identical
    spellings (difflib ratio >= 0.85).
    """
    key = normalize_name(name)
    if not key:
        return None
    pool = {normalize_name(c): c for c in candidates}
    if key in pool:
        return pool[key]

    tokens = key.split()
    subset_hits = []
    for cand_key, cand in pool.items():
        cand_tokens = cand_key.split()
        small, large = sorted((tokens, cand_tokens), key=len)
        if len(small) >= 2 and _tokens_match(small, large):
            subset_hits.append(cand)
    if len(subset_hits) == 1:
        return subset_hits[0]

    best, best_ratio = None, 0.0
    for cand_key, cand in pool.items():
        ratio = difflib.SequenceMatcher(None, key, cand_key).ratio()
        if ratio > best_ratio:
            best, best_ratio = cand, ratio
    return best if best_ratio >= FUZZY_NAME_RATIO else None


# --------------------------------------------------------------------- participants


@dataclass(frozen=True)
class _Block:
    lines: list[str]
    start: int  # offset of the heading line
    end: int  # offset just past the block's last line


def _ends_block(stripped: str) -> bool:
    return bool(
        _ANY_HEADING_RE.match(stripped)
        or len(stripped.split()) > _PROSE_WORDS
        or _ANALYST_HEADING_RE.match(stripped)
        or _PARTICIPANT_HEADING_RE.match(stripped)
        or normalize_name(stripped) in MODERATOR_LABELS
    )


def _blocks(text: str, heading_re: re.Pattern[str]) -> list[_Block]:
    """Every participant-list block under a heading matching ``heading_re``."""
    found: list[_Block] = []
    lines = list(_iter_lines(text))
    for i, (offset, line) in enumerate(lines):
        heading = heading_re.match(line.strip())
        if heading is None:
            continue
        block = [heading["rest"].strip()] if heading["rest"].strip() else []
        end = offset + len(line)
        for following_offset, following in lines[i + 1 :]:
            stripped = following.strip()
            if not stripped:
                continue
            if _ends_block(stripped) or len(block) >= MAX_PARTICIPANT_BLOCK_LINES:
                break
            block.append(stripped)
            end = following_offset + len(following)
        if block:
            found.append(_Block(block, offset, end))
    return found


def _participant_block(text: str) -> list[str] | None:
    """Non-blank lines of the management participant block."""
    blocks = _blocks(text, _PARTICIPANT_HEADING_RE)
    return blocks[0].lines if blocks else None


def participant_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of the participant and analyst lists in ``text``."""
    return [
        (b.start, b.end)
        for heading_re in (_PARTICIPANT_HEADING_RE, _ANALYST_HEADING_RE)
        for b in _blocks(text, heading_re)
    ]


def _is_title_line(line: str) -> bool:
    return bool(_TITLE_WORD_RE.search(line))


def _looks_like_name(line: str) -> bool:
    words = strip_honorifics(line).split()
    return (
        1 <= len(words) <= 5
        and not any(ch.isdigit() for ch in line)
        and bool(_LABEL_CHARS_RE.match(line))
        and line[:1].isupper()
    )


def _pair_name_title_lines(lines: list[str]) -> list[tuple[str, str]] | None:
    """Bare lines alternating name / job title ("Salil Parekh", "Chief Executive
    Officer"); ``None`` if the block does not look like that."""
    if len(lines) < 2 or _HONORIFIC_SPLIT_RE.search(" ".join(lines)):
        return None
    if any(_DASH_RE.search(line) and not _is_title_line(line) for line in lines):
        return None  # "Name - Title" entries: the dash format
    pairs: list[tuple[str, str]] = []
    name: str | None = None
    for line in lines:
        if _is_title_line(line):
            if name is None:
                if not pairs:
                    return None
                # A title that wrapped onto a second line.
                previous_name, previous_title = pairs[-1]
                pairs[-1] = (previous_name, f"{previous_title} {line}")
                continue
            pairs.append((name, line))
            name = None
        elif _looks_like_name(line) and name is None:
            name = line
        else:
            return None
    if name is not None:
        pairs.append((name, ""))
    titled = sum(1 for _, title in pairs if title)
    return pairs if pairs and titled * 2 >= len(pairs) else None


def _split_by_lines(lines: list[str]) -> list[str]:
    """Entries when there are no honorifics.

    A line with a dash starts a new entry once the current entry has its dash;
    other lines continue the current entry (a wrapped name or title).
    """
    entries: list[str] = []
    for line in lines:
        if entries and not (_DASH_RE.search(line) and _DASH_RE.search(entries[-1])):
            entries[-1] = f"{entries[-1]} {line}"
        else:
            entries.append(line)
    return entries


def _split_entries(lines: list[str]) -> list[str]:
    joined = " ".join(lines)
    if _HONORIFIC_SPLIT_RE.search(joined):
        parts = _HONORIFIC_SPLIT_RE.split(joined)
    else:
        parts = _split_by_lines(lines)
    entries: list[str] = []
    for part in parts:
        entries.extend(p.strip(" ,;") for p in _ENTRY_SPLIT_RE.split(part))
    return [e for e in entries if e]


def parse_participants(text: str) -> dict[str, str]:
    """Parse the management list into ``{name: title}``.

    ``text`` should be the first page or two of the transcript.
    """
    block = _participant_block(text)
    if block is None:
        return {}

    participants: dict[str, str] = {}
    paired = _pair_name_title_lines(block)
    if paired is not None:
        for name, title in paired:
            name = strip_honorifics(name).strip(" ,;.")
            participants[_smart_title(name)] = _smart_title(title, keep_acronyms=True)
        return participants
    for entry in _split_entries(block):
        pieces = _DASH_RE.split(strip_honorifics(entry), maxsplit=1)
        if len(pieces) == 1:
            pieces = entry.split(",", 1)
        name = strip_honorifics(pieces[0]).strip(" ,;.")
        title = pieces[1].strip(" ,;.") if len(pieces) > 1 else ""
        words = name.split()
        if not 1 <= len(words) <= 5 or any(ch.isdigit() for ch in name):
            continue
        participants[_smart_title(name)] = _smart_title(title, keep_acronyms=True)
    return participants


def parse_analyst_list(text: str) -> dict[str, str | None]:
    """Analysts listed under an "Analysts" heading on the first pages: ``{name: firm}``.

    Entries are "Name - Firm" or alternating name / firm lines.
    """
    analysts: dict[str, str | None] = {}
    for block in _blocks(text, _ANALYST_HEADING_RE):
        lines = block.lines
        if any(_DASH_RE.search(line) for line in lines):
            for line in lines:
                pieces = _DASH_RE.split(strip_honorifics(line), maxsplit=1)
                name = pieces[0].strip(" ,;.")
                if _looks_like_name(name):
                    firm = pieces[1].strip(" ,;.") if len(pieces) > 1 else None
                    analysts.setdefault(_smart_title(name), firm or None)
            continue
        for i in range(0, len(lines), 2):
            name = strip_honorifics(lines[i]).strip(" ,;.")
            if _looks_like_name(name):
                firm = lines[i + 1].strip(" ,;.") if i + 1 < len(lines) else None
                analysts.setdefault(_smart_title(name), firm or None)
    return analysts


# ------------------------------------------------------------------- speaker labels


@dataclass(frozen=True)
class _LabelLine:
    line_start: int
    body_start: int
    label: str  # cleaned name part of the label
    # A name alone on its line, without a colon (Infosys style).
    standalone: bool = False


def _iter_lines(text: str) -> Iterator[tuple[int, str]]:
    offset = 0
    for line in text.split("\n"):
        yield offset, line
        offset += len(line) + 1


def _label_name(raw_label: str) -> str:
    """Name part of a label: "Rajesh Kumar - CFO" -> "Rajesh Kumar"."""
    return _LABEL_NAME_SPLIT_RE.split(raw_label.strip(), maxsplit=1)[0].strip(" .")


def _standalone_label(line: str) -> str | None:
    """The name, if ``line`` could be a speaker name standing alone on its line."""
    stripped = line.strip()
    if (
        not stripped
        or ":" in stripped
        or stripped.endswith(_SENTENCE_END)
        or len(stripped.split()) > MAX_LABEL_WORDS
        or not stripped[0].isupper()
        or not _LABEL_CHARS_RE.match(stripped)
    ):
        return None
    return _label_name(stripped) or None


def _candidate_labels(text: str, skip_spans: Sequence[tuple[int, int]] = ()) -> list[_LabelLine]:
    found = []
    lines = list(_iter_lines(text))
    for i, (offset, line) in enumerate(lines):
        match = _LABEL_RE.match(line)
        if match is None:
            name = _standalone_label(line)
            if name is None or any(start <= offset < end for start, end in skip_spans):
                continue
            # The speech must follow on the next non-blank line.
            following = next((rest for _, rest in lines[i + 1 :] if rest.strip()), None)
            if following is None or _LABEL_RE.match(following):
                continue
            found.append(
                _LabelLine(
                    line_start=offset,
                    body_start=offset + len(line) + 1,
                    label=name,
                    standalone=True,
                )
            )
            continue
        raw = match["label"]
        if (
            len(raw.split()) > MAX_LABEL_WORDS
            or not raw[0].isupper()
            or not _LABEL_CHARS_RE.match(raw)
        ):
            continue
        name = _label_name(raw)
        if not name:
            continue
        rest_start = offset + match.start("rest")
        body_start = rest_start if match["rest"].strip() else offset + len(line) + 1
        found.append(_LabelLine(line_start=offset, body_start=body_start, label=name))
    return found


def _is_moderator(label: str) -> bool:
    return normalize_name(label) in MODERATOR_LABELS


def _accepted_labels(candidates: list[_LabelLine], known_names: Iterable[str]) -> list[_LabelLine]:
    known = list(known_names)
    counts = Counter(normalize_name(c.label) for c in candidates if not c.standalone)
    # A bare line counts only as a known name: "Revenue Outlook" on its own line is a
    # heading, not a speaker, however often it repeats.
    return [
        c
        for c in candidates
        if _is_moderator(c.label)
        or (not c.standalone and counts[normalize_name(c.label)] > 1)
        or match_name(c.label, known) is not None
    ]


def _segments(text: str, labels: list[_LabelLine]) -> list[tuple[_LabelLine | None, int, int]]:
    """(label, body_start, body_end) for each turn, plus leading text before the first label."""
    segments: list[tuple[_LabelLine | None, int, int]] = []
    first_start = labels[0].line_start if labels else len(text)
    if text[:first_start].strip():
        segments.append((None, 0, first_start))
    for i, label in enumerate(labels):
        end = labels[i + 1].line_start if i + 1 < len(labels) else len(text)
        segments.append((label, min(label.body_start, end), end))
    return segments


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def find_analyst_intros(moderator_text: str) -> dict[str, str | None]:
    """Names (and firms) the moderator introduces as asking a question."""
    flat = " ".join(moderator_text.split())
    analysts: dict[str, str | None] = {}
    for match in _ANALYST_INTRO_RE.finditer(flat):
        name = _smart_title(strip_honorifics(match["name"]).strip(" ."))
        if name and normalize_name(name) not in MODERATOR_LABELS:
            firm = match["firm"].strip() if match["firm"] else None
            analysts.setdefault(name, firm)
    return analysts


def announces_qna(moderator_text: str) -> bool:
    return bool(_QNA_START_RE.search(" ".join(moderator_text.split())))


def parse_transcript(pages: Sequence[str]) -> ParsedTranscript:
    """Parse cleaned page texts into speaker turns with roles and sections."""
    paged = PagedText.from_pages(pages)
    text = paged.text
    front = text[: paged.offset_of_page(PARTICIPANT_PAGES + 1)]
    participants = parse_participants(front)
    listed_analysts = {
        name: firm
        for name, firm in parse_analyst_list(front).items()
        if match_name(name, participants) is None
    }

    candidates = _candidate_labels(text, participant_spans(front))
    # Pass 1 finds moderator turns, whose introductions name the analysts.
    labels = _accepted_labels(candidates, participants)
    analysts: dict[str, str | None] = dict(listed_analysts)
    for label, start, end in _segments(text, labels):
        if label is not None and _is_moderator(label.label):
            for name, firm in find_analyst_intros(text[start:end]).items():
                analysts.setdefault(name, firm)
    # Pass 2 also accepts analysts who speak only once.
    labels = _accepted_labels(candidates, [*participants, *analysts])

    turns: list[SpeakerTurn] = []
    section = ChunkSection.PREPARED_REMARKS
    for label, raw_start, raw_end in _segments(text, labels):
        start, end = _trim(text, raw_start, raw_end)
        if label is None:
            if start < end:
                turns.append(SpeakerTurn(None, SpeakerRole.UNKNOWN, ChunkSection.OTHER, start, end))
            continue

        speaker, role = _resolve_speaker(label.label, participants, analysts)
        if role is SpeakerRole.MODERATOR and announces_qna(text[start:end]):
            section = ChunkSection.QNA
        elif role is SpeakerRole.ANALYST:
            # An analyst speaking means the Q&A has started, even if we missed the cue.
            section = ChunkSection.QNA
        turns.append(SpeakerTurn(speaker, role, section, start, end))

    return ParsedTranscript(paged=paged, turns=turns, participants=participants, analysts=analysts)


def _resolve_speaker(
    label: str, participants: dict[str, str], analysts: dict[str, str | None]
) -> tuple[str, SpeakerRole]:
    if _is_moderator(label):
        return "Moderator", SpeakerRole.MODERATOR
    if (name := match_name(label, participants)) is not None:
        return name, SpeakerRole.MANAGEMENT
    if (name := match_name(label, analysts)) is not None:
        return name, SpeakerRole.ANALYST
    return _smart_title(strip_honorifics(label)), SpeakerRole.UNKNOWN
