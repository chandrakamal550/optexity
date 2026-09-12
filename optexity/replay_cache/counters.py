"""Aggregate replay-gate outcomes across a run.

Deliberately import-light: this module imports nothing from ``optexity`` besides
pydantic. It exists to let ``optexity.schema.memory`` hold a ``ReplayCounters``
field without dragging in ``optexity.replay_cache.gate`` (which imports
``optexity.replay_cache.emit``, which imports
``optexity.inference.core.interaction.utils``, which imports back into
``optexity.schema``) — that path would be a circular import. Keep this module's
import list exactly as small as it is now; don't be tempted to add a type import
from ``gate`` "just for clarity".
"""

from pydantic import BaseModel


class ReplayCounters(BaseModel):
    """Without these, 'the cached run worked' is unfalsifiable."""

    hits: int = 0
    chain_recoveries: int = 0
    escalations: int = 0

    def record(self, outcome) -> None:
        if not outcome.resolved:
            self.escalations += 1
        elif outcome.candidate_rank == 0:
            self.hits += 1
        else:
            self.chain_recoveries += 1

    @property
    def total(self) -> int:
        return self.hits + self.chain_recoveries + self.escalations

    def summary(self) -> str:
        return (
            f"replay cache: {self.hits} hit, "
            f"{self.chain_recoveries} chain recovery, "
            f"{self.escalations} escalated to LLM "
            f"({self.total} cached steps)"
        )
