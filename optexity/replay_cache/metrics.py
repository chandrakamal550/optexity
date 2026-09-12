"""Before-and-after numbers for an agentic run versus its cached replay.

Deliberately import-light, mirroring ``optexity.replay_cache.counters``: this
module imports nothing from ``optexity`` besides pydantic. It exists to let
``optexity.schema.memory`` hold a list of ``RunMetrics`` without dragging in
``optexity.replay_cache.gate`` (which imports ``optexity.replay_cache.emit``,
which imports ``optexity.inference.core.interaction.utils``, which imports
back into ``optexity.schema``) — that path would be a circular import. Keep
this module's import list exactly as small as it is now.

Also deliberately defensive about usage: StepMetadata carries no token fields
despite its docstring, and AgentHistoryList.usage is only assigned on normal
completion and KeyboardInterrupt — it stays None if the run raised.
"""

from pydantic import BaseModel


class RunMetrics(BaseModel):
    wall_clock_s: float = 0.0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    steps: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def from_history(history, wall_clock_s: float) -> RunMetrics:
    if history is None:
        return RunMetrics(wall_clock_s=wall_clock_s)
    steps = len(getattr(history, "history", None) or [])
    usage = getattr(history, "usage", None)
    return RunMetrics(
        wall_clock_s=wall_clock_s,
        llm_calls=steps,
        prompt_tokens=getattr(usage, "total_prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "total_completion_tokens", 0) or 0,
        steps=steps,
    )


def _ratio(before: float, after: float) -> str:
    if after <= 0:
        return "n/a"
    return f"{before / after:.1f}x"


def compare(agentic: RunMetrics, cached: RunMetrics) -> str:
    return (
        "replay cache comparison\n"
        f"  wall clock : {agentic.wall_clock_s:.2f}s -> {cached.wall_clock_s:.2f}s "
        f"({_ratio(agentic.wall_clock_s, cached.wall_clock_s)} faster)\n"
        f"  llm calls  : {agentic.llm_calls} -> {cached.llm_calls}\n"
        f"  tokens     : {agentic.total_tokens} -> {cached.total_tokens} "
        f"(saved {agentic.total_tokens - cached.total_tokens})\n"
        f"  steps      : {agentic.steps} -> {cached.steps}"
    )
