"""What the LLM must return.

- ``extract-v1`` (one chunk per request): ``{"claims": [ExtractedClaim, ...]}``.
- ``extract-v2`` (a batch of chunks): ``{"claims": [BatchClaim, ...]}``, where each
  claim also names the ``chunk_id`` its quote comes from.

The schema is deliberately flat (every field required, nulls explicit), which is
what strict JSON-schema structured outputs need. ``llm_json_schema`` turns the
Pydantic model into that strict form.
"""

import copy
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import ClaimType, ClaimUnit, Direction, MetricNormalized

MAX_QUOTE_CHARS = 1500


class ExtractedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str = Field(
        min_length=1,
        max_length=MAX_QUOTE_CHARS,
        description="The exact words from the chunk (verbatim, one or two sentences).",
    )
    metric_raw: str = Field(min_length=1, description="The metric as management said it.")
    metric_normalized: MetricNormalized
    claim_type: ClaimType
    target_low: float | None = Field(description="Lower bound; a single target sets both.")
    target_high: float | None = Field(description="Upper bound; a single target sets both.")
    unit: ClaimUnit | None
    direction: Direction
    timeframe_raw: str | None = Field(description='As said, e.g. "by FY27", "H2", "this year".')
    target_fiscal_year: int | None = Field(description="Indian fiscal year, e.g. 2027 for FY27.")
    target_fiscal_quarter: int | None = Field(description="1-4, or null.")
    confidence: float = Field(ge=0, le=1)

    @field_validator("quote", "metric_raw", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("timeframe_raw", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("target_fiscal_year", mode="after")
    @classmethod
    def _expand_year(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if 0 <= value < 100:  # "27" -> 2027
            value += 2000
        if not 2000 <= value <= 2099:
            raise ValueError("target_fiscal_year must be a fiscal year such as 2027")
        return value

    @field_validator("target_fiscal_quarter", mode="after")
    @classmethod
    def _check_quarter(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 4:
            raise ValueError("target_fiscal_quarter must be 1-4 or null")
        return value

    @model_validator(mode="after")
    def _normalise_targets(self) -> "ExtractedClaim":
        if self.target_low is None and self.target_high is not None:
            self.target_low = self.target_high
        elif self.target_high is None and self.target_low is not None:
            self.target_high = self.target_low
        if (
            self.target_low is not None
            and self.target_high is not None
            and self.target_low > self.target_high
        ):
            self.target_low, self.target_high = self.target_high, self.target_low
        return self


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[ExtractedClaim]


class _ChunkRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=1, max_length=16)

    @field_validator("chunk_id", mode="before")
    @classmethod
    def _clean_id(cls, value: object) -> object:
        # Accept "[C2]" or " c2 " for "C2".
        return value.strip().strip("[]").strip().upper() if isinstance(value, str) else value


# _ChunkRef last in the bases, so chunk_id is the first field (and first in the schema):
# the model names the chunk before it writes the quote.
class BatchClaim(ExtractedClaim, _ChunkRef):
    model_config = ConfigDict(extra="forbid")


class BatchExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The v2 prompt explains every field, so the schema sent to the API skips descriptions.
    llm_schema_descriptions: ClassVar[bool] = False

    claims: list[BatchClaim]


# --------------------------------------------------------------- strict schema

# Keywords strict structured outputs don't accept; Pydantic still enforces them.
_DROPPED_KEYWORDS = frozenset(
    {
        "title",
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
    }
)


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _inline(copy.deepcopy(defs[name]), defs)
    out = {
        key: _inline(value, defs)
        for key, value in node.items()
        if key not in _DROPPED_KEYWORDS and key != "$defs"
    }
    if out.get("type") == "object" and "properties" in out:
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    # {"anyOf": [{"type": "number"}, {"type": "null"}]} -> {"type": ["number", "null"]}
    any_of = out.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
        (other,) = [item for item in any_of if item != {"type": "null"}]
        if isinstance(other.get("type"), str) and set(other) <= {"type", "enum", "description"}:
            merged = {k: v for k, v in out.items() if k != "anyOf"}
            merged["type"] = [other["type"], "null"]
            if "enum" in other:
                merged["enum"] = [*other["enum"], None]
            if "description" in other and "description" not in merged:
                merged["description"] = other["description"]
            out = merged
    return out


def _drop_descriptions(node: Any) -> Any:
    if isinstance(node, list):
        return [_drop_descriptions(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        key: _drop_descriptions(value)
        for key, value in node.items()
        # "description" as a property *name* stays; only the keyword goes.
        if key != "description" or isinstance(value, dict)
    }


def llm_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A strict-mode JSON schema: refs inlined, all fields required, no extras.

    Field descriptions are dropped when the model sets ``llm_schema_descriptions = False``
    (they cost prompt tokens on every request).
    """
    schema = model.model_json_schema()
    result: dict[str, Any] = _inline(schema, schema.get("$defs", {}))
    if not getattr(model, "llm_schema_descriptions", True):
        result = _drop_descriptions(result)
    return result
