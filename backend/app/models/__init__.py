"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.actual import Actual
from app.models.base import Base
from app.models.chunk import EMBEDDING_DIM, Chunk
from app.models.claim import Claim
from app.models.claim_thread import ClaimThread
from app.models.company import Company
from app.models.document import Document, DocumentPage
from app.models.enums import (
    ChangeType,
    ChunkSection,
    ClaimType,
    ClaimUnit,
    Direction,
    DocType,
    DocumentStatus,
    ExtractionRunStatus,
    MetricNormalized,
    SpeakerRole,
    VerificationMethod,
    VerificationRunKind,
    VerificationRunStatus,
    VerificationStatus,
)
from app.models.extraction_run import ExtractionRun
from app.models.verification import Verification
from app.models.verification_run import VerificationRun

__all__ = [
    "EMBEDDING_DIM",
    "Actual",
    "Base",
    "ChangeType",
    "Chunk",
    "ChunkSection",
    "Claim",
    "ClaimThread",
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
    "VerificationMethod",
    "VerificationRun",
    "VerificationRunKind",
    "VerificationRunStatus",
    "VerificationStatus",
]
