"""A per-run cap on LLM calls (``VERIFY_MAX_LLM_CALLS``), shared by linking and judging.

One "call" is one ``complete_json`` invocation; its one schema-repair retry (see
``services/llm.py``) is part of the same call. Tokens are added up for the run.
"""

from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

from app.services.llm import LLMError, LLMProvider, LLMUsage

T = TypeVar("T", bound=BaseModel)

BUDGET_EXHAUSTED = "llm budget exhausted"


class BudgetExhausted(Exception):
    pass


@dataclass
class LLMBudget:
    max_calls: int
    calls: int = 0
    usage: LLMUsage = field(default_factory=LLMUsage)

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls)

    async def complete_json(
        self, llm: LLMProvider, system: str, user: str, schema: type[T], model: str
    ) -> T:
        """Make one call, or raise ``BudgetExhausted`` without calling when none are left."""
        if self.remaining <= 0:
            raise BudgetExhausted(BUDGET_EXHAUSTED)
        self.calls += 1
        try:
            result, usage = await llm.complete_json(system, user, schema, model)
        except LLMError as exc:
            self.usage += exc.usage
            raise
        self.usage += usage
        return result
