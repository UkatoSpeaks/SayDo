import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import ChunkSection, SpeakerRole
from app.services.fiscal import parse_period_label

MAX_SEARCH_TOP_K = 30


class ChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    page_start: int
    page_end: int
    speaker: str | None
    speaker_role: SpeakerRole
    section: ChunkSection
    text: str
    token_count: int
    has_embedding: bool


class ChunkListResponse(BaseModel):
    document_id: uuid.UUID
    total: int
    limit: int
    offset: int
    chunks: list[ChunkRead]


class SearchRequest(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    top_k: int = Field(default=8, ge=1, le=MAX_SEARCH_TOP_K)
    section: ChunkSection | None = None
    speaker_role: SpeakerRole | None = None
    from_period: str | None = Field(default=None, examples=["Q1FY25"])
    to_period: str | None = Field(default=None, examples=["Q4FY26"])

    @model_validator(mode="after")
    def _check_periods(self) -> "SearchRequest":
        for name in ("from_period", "to_period"):
            label = getattr(self, name)
            if label is not None:
                try:
                    parse_period_label(label)
                except ValueError as exc:
                    raise ValueError(f"{name}: {exc}") from exc
        if self.period_range[0] > self.period_range[1]:
            raise ValueError("from_period must not be after to_period")
        return self

    @property
    def period_range(self) -> tuple[int, int]:
        """Inclusive range of quarter ordinals (fiscal_year * 4 + quarter - 1).

        A full-year label covers Q1-Q4 of that year; a missing bound is open.
        """
        low, high = 0, 2**31 - 1
        if self.from_period is not None:
            fy, q = parse_period_label(self.from_period)
            low = fy * 4 + (q or 1) - 1
        if self.to_period is not None:
            fy, q = parse_period_label(self.to_period)
            high = fy * 4 + (q or 4) - 1
        return low, high


class SearchResult(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    score: float = Field(description="Cosine similarity (1 = identical direction)")
    text: str
    speaker: str | None
    speaker_role: SpeakerRole
    section: ChunkSection
    period_label: str
    page_start: int
    page_end: int


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
