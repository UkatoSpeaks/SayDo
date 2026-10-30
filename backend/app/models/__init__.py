"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.actual import Actual
from app.models.base import Base
from app.models.chunk import EMBEDDING_DIM, Chunk
from app.models.claim import Claim
from app.models.company import Company
from app.models.document import Document, DocumentPage
from app.models.enums import (
    ChunkSection,
    ClaimType,
    ClaimUnit,
    Direction,
    DocType,
    DocumentStatus,
    ExtractionRunStatus,
    MetricNormalized,
    SpeakerRole,
    VerificationStatus,
)
from app.models.extraction_run import ExtractionRun
from app.models.verification import Verification

__all__ = [
    "EMBEDDING_DIM",
    "Actual",
    "Base",
    "Chunk",
    "ChunkSection",
    "Claim",
    "ClaimType",
    "ClaimUnit",
    "Company",
    "Direction",
    "DocType",
    "Document",
    "DocumentPage",
    "DocumentStatus",
    "ExtractionRun",
    "ExtractionRunStatus",
    "MetricNormalized",
    "SpeakerRole",
    "Verification",
    "VerificationStatus",
]
