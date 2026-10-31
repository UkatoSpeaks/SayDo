"""Prompts and reply schemas for the two LLM steps of verification.

Both are small and only used where judgment on text is needed:
- ``link-v1``: is an ambiguous promise the same as one made on an earlier call?
- ``judge-v1``: did a qualitative/event promise happen, according to later calls?
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LINK_PROMPT_VERSION = "link-v1"
JUDGE_PROMPT_VERSION = "judge-v1"
SUMMARY_PROMPT_VERSION = "explain-v1"


def _clean_id(value: object) -> object:
    return value.strip().strip("[]").strip().upper() if isinstance(value, str) else value


# ------------------------------------------------------------------- linking

LINK_SYSTEM_PROMPT = """\
You match promises made by the management of one company on different earnings calls.
Each ITEM is a promise from a later call. Each candidate THREAD is a promise from earlier
calls. Decide, for every item, whether it is the SAME promise as one of the threads it
lists (same project, product, plant or initiative, possibly with a new name or a new
date, e.g. "Line 4" and "the Pune expansion" when the text shows they are the same
plant), or a different promise.

Rules:
- Only choose a thread id listed for that item; otherwise answer null.
- Different projects that share a category (two different plants) are NOT the same.
- When unsure, answer null.
Reply with one JSON object: {"decisions": [{"item": "N1", "same_as": "T2" or null}, ...]}
with one decision per item."""


class LinkDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1, max_length=16)
    same_as: str | None

    @field_validator("item", "same_as", mode="before")
    @classmethod
    def _ids(cls, value: object) -> object:
        return _clean_id(value)


class LinkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[LinkDecision]


# ------------------------------------------------------------------- judging

JUDGE_SYSTEM_PROMPT = """\
You check whether a promise made by company management on an earnings call was kept,
using only the EVIDENCE excerpts from later calls (management speaking). Do not use
outside knowledge.

Statuses:
- delivered: the evidence says it happened (e.g. the plant was commissioned).
- missed: the evidence says it did not happen by the target date, or was cancelled.
- in_progress: the evidence says it is still under way and the target date has not passed.
- unverifiable: the evidence does not say either way.

Reply with one JSON object:
{"status": "...", "reasoning": "at most two sentences",
 "evidence_ids": ["E1", ...], "quote": "verbatim words from one excerpt",
 "quote_evidence_id": "E1"}
The quote must be copied exactly from the excerpt named in quote_evidence_id, numbers
unchanged. For unverifiable, quote and quote_evidence_id may be empty strings."""


JudgeStatus = Literal["delivered", "missed", "in_progress", "unverifiable"]


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: JudgeStatus
    reasoning: str = Field(max_length=600)
    evidence_ids: list[str]
    quote: str = Field(max_length=1500)
    quote_evidence_id: str = Field(max_length=16)

    @field_validator("quote_evidence_id", mode="before")
    @classmethod
    def _quote_id(cls, value: object) -> object:
        return _clean_id(value)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _clean_ids(cls, value: object) -> object:
        if isinstance(value, list):
            return [_clean_id(v) for v in value]
        return value


def judge_retry_message(error: str) -> str:
    return (
        f"Your quote could not be found in the excerpt you cited ({error}). Copy the quote "
        "exactly, word for word and with the same numbers, from one EVIDENCE excerpt, and "
        "name that excerpt in quote_evidence_id. Reply with the full JSON object again."
    )


# ---------------------------------------------------------------- explaining

SUMMARY_SYSTEM_PROMPT = """\
Management missed, delayed or stopped mentioning a promise. Using only the EXCERPTS from
later calls, say in at most two sentences how management explained it. If the excerpts
do not explain it, say "No explanation given." Reply with one JSON object:
{"summary": "...", "quote": "verbatim words from one excerpt", "quote_excerpt_id": "X1"}
(quote and quote_excerpt_id may be empty strings when there is no explanation)."""


class SummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=600)
    quote: str = Field(max_length=1500)
    quote_excerpt_id: str = Field(max_length=16)

    @field_validator("quote_excerpt_id", mode="before")
    @classmethod
    def _quote_id(cls, value: object) -> object:
        return _clean_id(value)
