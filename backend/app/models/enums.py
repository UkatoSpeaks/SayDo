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


class ExtractionRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PAUSED_QUOTA = "paused_quota"
    FAILED = "failed"


class MetricNormalized(StrEnum):
    REVENUE = "revenue"
    REVENUE_GROWTH = "revenue_growth"
    EBITDA = "ebitda"
    EBITDA_MARGIN = "ebitda_margin"
    GROSS_MARGIN = "gross_margin"
    PAT = "pat"
    PAT_MARGIN = "pat_margin"
    CAPEX = "capex"
    CAPACITY = "capacity"
    CAPACITY_UTILIZATION = "capacity_utilization"
    VOLUME_GROWTH = "volume_growth"
    DEBT = "debt"
    NET_DEBT = "net_debt"
    ORDER_BOOK = "order_book"
    MARKET_SHARE = "market_share"
    STORE_COUNT = "store_count"
    PRODUCT_LAUNCH = "product_launch"
    PLANT_COMMISSIONING = "plant_commissioning"
    DIVIDEND_PAYOUT = "dividend_payout"
    COST_REDUCTION = "cost_reduction"
    OTHER = "other"


class ClaimUnit(StrEnum):
    PERCENT = "percent"
    INR_CRORE = "inr_crore"
    INR_LAKH = "inr_lakh"
    INR_BN = "inr_bn"
    USD_MN = "usd_mn"
    UNITS = "units"
    MW = "mw"
    TONNES = "tonnes"
    BPS = "bps"
    X = "x"
    COUNT = "count"
