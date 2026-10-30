"""A synthetic, realistic Indian earnings-call transcript rendered to a multi-page PDF.

Layout per page: a 3-line header (company, call title, date), up to
``BODY_LINES_PER_PAGE`` lines of body text, and a "Page X of Y" footer, like
the transcripts companies file with NSE/BSE.

Content: participant list on page 1 (one name wraps onto the next line),
moderator intro, IR/CEO/CFO prepared remarks, a Q&A with three analysts
introduced by the moderator, one very long management answer that crosses a
page break, short moderator filler turns, and a sentence that starts a line and
contains a colon but is not a speaker label.
"""

import textwrap
from typing import Any

import pymupdf

COMPANY = "Acme Industries Limited"
CALL_TITLE = "Q2 FY26 Earnings Conference Call"
CALL_DATE = "October 28, 2025"
HEADER_LINES = (COMPANY, CALL_TITLE, CALL_DATE)
BODY_LINES_PER_PAGE = 28
WRAP_WIDTH = 92

PARTICIPANT_LINES = [
    "MANAGEMENT: MR. RAJESH KUMAR – MANAGING DIRECTOR AND CHIEF",
    "EXECUTIVE OFFICER",
    "MS. ANITA DESAI – CHIEF FINANCIAL OFFICER",
    "DR. VIKRAM",
    "RAO – HEAD, INVESTOR RELATIONS",
    "",
    "MODERATOR: MR. ROHAN GUPTA – SKYLINE CAPITAL MARKETS",
]

EXPECTED_PARTICIPANTS = {
    "Rajesh Kumar": "Managing Director and Chief Executive Officer",
    "Anita Desai": "Chief Financial Officer",
    "Vikram Rao": "Head, Investor Relations",
}
ANALYSTS = {
    "Priya Shah": "Kotak Institutional Equities",
    "Nikhil Jain": "HDFC Securities",
    "Sanjay Mehta": "Motilal Oswal Financial Services",
}

# Starts a line and contains a colon, but is not a speaker label.
COLON_SENTENCE = (
    "Key highlights for the quarter: revenue grew 18% year-on-year to Rs. 1,250 crore and "
    "EBITDA margin expanded by 120 basis points to 19.4%."
)
FILLER_TURN = "Thank you. Please go ahead."

_PLANTS = ["Pune", "Chennai", "Sanand", "Hosur", "Nagpur", "Vadodara", "Indore", "Coimbatore"]
_LONG_ANSWER_SENTENCES = [
    "Nikhil, let me take this question in some detail because capacity is central to our "
    "FY27 plan.",
    *(
        f"At our {plant} plant, utilisation was about {70 + 3 * i}% in the quarter, and we "
        f"expect the line {i + 1} expansion to add roughly {40 + 5 * i} thousand units of "
        f"annual capacity once commissioning is complete in Q{(i % 4) + 1} FY27."
        for i, plant in enumerate(_PLANTS)
    ),
    *(
        f"On the {area} side, we have committed capex of Rs. {120 + 15 * i} crore, which "
        f"will be funded entirely from internal accruals without any additional debt."
        for i, area in enumerate(
            ["machining", "paint shop", "assembly", "testing", "warehousing", "tooling"]
        )
    ),
    *(
        f"Our {segment} customers have given us firm schedules for the next "
        f"{2 + i} quarters, which gives us good visibility on volumes and pricing."
        for i, segment in enumerate(["export", "OEM", "replacement", "railway", "defence"])
    ),
    "We are also investing in automation, and we expect employee cost as a percentage of "
    "revenue to come down by about 50 basis points over the next two years.",
    "So, to summarise, we remain confident of delivering revenue growth of 15% to 18% in "
    "FY27 with EBITDA margins in the range of 19% to 20%.",
]
LONG_ANSWER = " ".join(_LONG_ANSWER_SENTENCES)
LONG_ANSWER_FIRST = _LONG_ANSWER_SENTENCES[0]
LONG_ANSWER_LAST = _LONG_ANSWER_SENTENCES[-1]

# (speaker label, paragraphs). The label is printed on its own line, as most
# transcripts do; an entry with inline=True puts the text after the colon.
TURNS: list[tuple[str, list[str], bool]] = [
    (
        "Moderator",
        [
            "Ladies and gentlemen, good day and welcome to the Q2 FY26 Earnings Conference "
            "Call of Acme Industries Limited, hosted by Skyline Capital Markets. As a reminder, "
            "all participant lines will be in the listen-only mode and there will be an "
            "opportunity for you to ask questions after the presentation concludes. Should you "
            "need assistance during the conference call, please signal an operator by pressing "
            "star then zero on your touchtone phone. Please note that this conference is being "
            "recorded. I now hand the conference over to Dr. Vikram Rao. Thank you and over to "
            "you, sir."
        ],
        False,
    ),
    (
        "Vikram Rao",
        [
            "Thank you. Good evening, everyone, and welcome to our earnings call. Before we "
            "begin, please note that some of the statements made today may be forward-looking "
            "and are subject to risks and uncertainties. With that, I hand over to our "
            "Managing Director, Mr. Rajesh Kumar."
        ],
        False,
    ),
    (
        "Rajesh Kumar",
        [
            "Thank you, Vikram, and good evening to all of you. I am pleased to report another "
            "quarter of strong execution across our businesses.",
            COLON_SENTENCE,
            "Demand from our export customers remained healthy, and our order book stands at "
            "Rs. 4,800 crore, which is about 1.6 times our trailing twelve-month revenue. We "
            "commissioned the new forging line at our Pune plant during the quarter and it is "
            "ramping up as planned. For FY27, we expect revenue growth of 15% to 18%, driven "
            "by new capacity and a richer product mix.",
            "I will now request our CFO, Ms. Anita Desai, to take you through the financials.",
        ],
        False,
    ),
    (
        "Anita Desai",
        [
            "Thank you, Rajesh. Good evening, everyone. Revenue for the quarter was Rs. 1,250 "
            "crore, up 18% year-on-year. Gross margin improved by 90 basis points on the back "
            "of softer raw material prices and better realisations. EBITDA was Rs. 242 crore "
            "and the EBITDA margin was 19.4%. Profit after tax grew 22% to Rs. 141 crore.",
            "Our net debt reduced to Rs. 310 crore from Rs. 420 crore at the end of March, and "
            "we target to become net debt free by the end of FY27. Working capital days "
            "improved to 68 from 74 a year ago. With this, we can open the floor for questions.",
        ],
        False,
    ),
    (
        "Moderator",
        [
            "Thank you very much. We will now begin the question-and-answer session. Anyone who "
            "wishes to ask a question may press star and one on their touchtone telephone. The "
            "first question is from the line of Priya Shah from Kotak Institutional Equities. "
            "Please go ahead."
        ],
        False,
    ),
    (
        "Priya Shah",
        [
            "Thank you for the opportunity. My question is on margins. What is the EBITDA "
            "margin guidance for FY27, and how much of the improvement this quarter is "
            "sustainable?"
        ],
        False,
    ),
    (
        "Anita Desai",
        [
            "Thanks, Priya. We are guiding for an EBITDA margin of 19% to 20% for FY27. About "
            "two-thirds of the improvement this quarter came from operating leverage and mix, "
            "which we believe is sustainable, and the balance from lower commodity prices."
        ],
        False,
    ),
    ("Priya Shah", ["Understood. That is helpful, thank you."], True),
    (
        "Moderator",
        [
            "Thank you. The next question is from the line of Nikhil Jain from HDFC "
            "Securities. Please go ahead."
        ],
        False,
    ),
    (
        "Nikhil Jain",
        [
            "Good evening, and congratulations on a good quarter. Could you talk about the "
            "capacity expansion plans plant by plant, the capex required, and how that "
            "supports the FY27 growth guidance?"
        ],
        False,
    ),
    ("Rajesh Kumar", [LONG_ANSWER], False),
    ("Nikhil Jain", ["Thank you, that is very helpful. I will join back the queue."], False),
    ("Moderator", [FILLER_TURN], False),
    (
        "Moderator",
        [
            "The next question is from the line of Sanjay Mehta from Motilal Oswal Financial "
            "Services. Please go ahead."
        ],
        False,
    ),
    (
        "Sanjay Mehta",
        [
            "Hi, thanks for taking my question. What is the outlook for the working capital "
            "cycle and the dividend payout policy going forward?"
        ],
        False,
    ),
    (
        "Anita Desai",
        [
            "Sanjay, we expect working capital days to stay in the range of 65 to 70. On "
            "dividends, the board has approved a policy of paying out at least 25% of "
            "standalone profit after tax."
        ],
        False,
    ),
    (
        "Moderator",
        [
            "Thank you. As there are no further questions, I would now like to hand the "
            "conference back to the management for closing comments."
        ],
        False,
    ),
    (
        "Rajesh Kumar",
        [
            "Thank you, everyone, for joining us today. We remain committed to delivering on "
            "our FY27 targets and look forward to speaking with you next quarter."
        ],
        False,
    ),
    (
        "Moderator",
        [
            "Thank you. On behalf of Skyline Capital Markets, that concludes this conference. "
            "Thank you for joining us and you may now disconnect your lines."
        ],
        False,
    ),
]


def body_lines() -> list[str]:
    lines = [*PARTICIPANT_LINES, ""]
    for label, paragraphs, inline in TURNS:
        if inline:
            text = f"{label}: {' '.join(paragraphs)}"
            lines.extend(textwrap.wrap(text, WRAP_WIDTH))
        else:
            lines.append(f"{label}:")
            for i, paragraph in enumerate(paragraphs):
                if i:
                    lines.append("")
                lines.extend(textwrap.wrap(paragraph, WRAP_WIDTH))
        lines.append("")
    return lines


def page_layout() -> list[list[str]]:
    """Lines per page, including header and footer."""
    body = body_lines()
    chunks = [body[i : i + BODY_LINES_PER_PAGE] for i in range(0, len(body), BODY_LINES_PER_PAGE)]
    total = len(chunks)
    return [
        [*HEADER_LINES, "", *chunk, "", f"Page {n} of {total}"]
        for n, chunk in enumerate(chunks, start=1)
    ]


def build_concall_pdf() -> bytes:
    doc: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    # TextWriter embeds the font, so en dashes and curly quotes survive extraction.
    font = pymupdf.Font("helv")  # type: ignore[no-untyped-call]
    for lines in page_layout():
        page = doc.new_page()
        writer = pymupdf.TextWriter(page.rect)  # type: ignore[no-untyped-call]
        y = 48.0
        for line in lines:
            if line:
                writer.append((60, y), line, font=font, fontsize=9)  # type: ignore[no-untyped-call]
            y += 15
        writer.write_text(page)  # type: ignore[no-untyped-call]
    data: bytes = doc.tobytes()
    doc.close()
    return data
