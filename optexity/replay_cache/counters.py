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
        """Describe what was actually measured: gated command steps.

        The gate runs on every command-based action, not only on steps that
        came out of a cache entry — there is no runtime cache-lookup path, so
        this code cannot tell the two apart. Calling these "cached steps" made
        a hand-written automation with no cache involvement report "replay
        cache: 4 hit (4 cached steps)", which is simply false. The wording
        below claims only what the counters can support. If a runtime lookup
        path is added later, scope the counters to cache-sourced actions and
        the wording can go back to talking about the cache.
        """
        return (
            f"replay gate: {self.hits} resolved on primary, "
            f"{self.chain_recoveries} recovered via candidate chain, "
            f"{self.escalations} escalated to LLM "
            f"({self.total} command steps gated)"
        )
