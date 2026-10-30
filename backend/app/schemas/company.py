import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

_NSE_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
_BSE_CODE_RE = re.compile(r"^\d{6}$")


def _blank_to_none(value: object) -> object:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class CompanyCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    nse_symbol: str | None = Field(default=None, examples=["RELIANCE"])
    bse_code: str | None = Field(default=None, examples=["500325"])
    sector: Annotated[str, StringConstraints(max_length=128)] | None = None

    @field_validator("nse_symbol", mode="before")
    @classmethod
    def _normalize_nse(cls, value: object) -> object:
        value = _blank_to_none(value)
        if isinstance(value, str):
            value = value.upper()
            if not _NSE_SYMBOL_RE.fullmatch(value):
                raise ValueError("NSE symbol must be 1-20 chars of A-Z, 0-9, '&' or '-'")
        return value

    @field_validator("bse_code", mode="before")
    @classmethod
    def _normalize_bse(cls, value: object) -> object:
        value = _blank_to_none(value)
        if isinstance(value, int):
            value = str(value)
        if isinstance(value, str) and not _BSE_CODE_RE.fullmatch(value):
            raise ValueError("BSE code must be a 6-digit number")
        return value

    @field_validator("sector", mode="before")
    @classmethod
    def _normalize_sector(cls, value: object) -> object:
        return _blank_to_none(value)


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    nse_symbol: str | None
    bse_code: str | None
    sector: str | None
    created_at: datetime
    updated_at: datetime
