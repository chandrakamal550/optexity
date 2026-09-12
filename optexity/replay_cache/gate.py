"""Uniqueness check for a cached locator, plus the fallback chain.

Playwright already refuses to act on an ambiguous bare locator: click, fill and
friends pass strict=True. But handle_command.py catches that in a blanket
`except Exception`, flattens it into a generic error string, and falls through
to the LLM index path — so a fully degraded cache still succeeds while paying
full token cost.

This gate makes the distinction explicit and countable: resolved on the primary
command, recovered from the candidate chain, or escalated.
"""

import logging

from pydantic import BaseModel

from optexity.replay_cache.counters import ReplayCounters
from optexity.replay_cache.emit import command_from_locator

logger = logging.getLogger(__name__)

__all__ = ["GateOutcome", "ReplayCounters", "resolve_unique"]


class GateOutcome(BaseModel):
    resolved: bool
    command: str | None
    match_count: int
    reason: str
    candidate_rank: int


async def _count(browser, command: str) -> int:
    try:
        locator = await browser.get_locator_from_command(command)
        if locator is None:
            return 0
        return await locator.count()
    except Exception as e:
        logger.debug(f"Replay gate could not resolve {command!r}: {type(e).__name__}: {e}")
        return -1


async def resolve_unique(browser, primary_command: str, candidates: list[dict]) -> GateOutcome:
    """First command in the chain that matches exactly one element."""
    chain = [primary_command] + [
        command_from_locator(c["locator"]) for c in (candidates or [])
    ]
    seen: set[str] = set()
    ordered = [c for c in chain if not (c in seen or seen.add(c))]

    reasons: list[str] = []
    for rank, command in enumerate(ordered):
        n = await _count(browser, command)
        if n == 1:
            reason = "resolved on primary" if rank == 0 else (
                f"recovered at rank {rank} after {'; '.join(reasons)}"
            )
            return GateOutcome(
                resolved=True, command=command, match_count=1,
                reason=reason, candidate_rank=rank,
            )
        if n == 0:
            reasons.append(f"{command} matched nothing")
        elif n < 0:
            reasons.append(f"{command} failed to resolve")
        else:
            reasons.append(f"{command} ambiguous ({n} matches)")

    return GateOutcome(
        resolved=False, command=None, match_count=0,
        reason=f"chain exhausted: {'; '.join(reasons)}",
        candidate_rank=-1,
    )
