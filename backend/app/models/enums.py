"""Domain enums, mapped to Postgres enum types."""

from enum import StrEnum


class DocType(StrEnum):
    TRANSCRIPT = "transcript"
    RESULTS = "results"
    PRESENTATION = "presentation"
    ANNUAL_REPORT = "annual_report"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSED = "parsed"
    CHUNKED = "chunked"
    EMBEDDED = "embedded"
    EXTRACTED = "extracted"
    FAILED = "failed"


class SpeakerRole(StrEnum):
    MANAGEMENT = "management"
    ANALYST = "analyst"
    MODERATOR = "moderator"
    UNKNOWN = "unknown"


class ChunkSection(StrEnum):
    PREPARED_REMARKS = "prepared_remarks"
    QNA = "qna"
    OTHER = "other"


class ClaimType(StrEnum):
    NUMERIC = "numeric"
    DIRECTIONAL = "directional"
    QUALITATIVE = "qualitative"


class Direction(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    MAINTAIN = "maintain"
    NONE = "none"


class VerificationStatus(StrEnum):
    DELIVERED = "delivered"
    MISSED = "missed"
    IN_PROGRESS = "in_progress"
    DROPPED = "dropped"
    UNVERIFIABLE = "unverifiable"
