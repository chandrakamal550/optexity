"""Drop exploration noise from a recorded trace.

Conservative by construction: pruning a needed step breaks replay silently,
while keeping a redundant one costs milliseconds. The rules below generate a
candidate sequence; the verification replay in the emitter decides whether it
was right.
"""

from pydantic import BaseModel

from optexity.replay_cache.records import StepRecord

# Actions that target a DOM element by index. Everything else is navigation or
# bookkeeping. Taken from browser-use's registered action set.
ELEMENT_ACTIONS = {"click", "input", "upload_file", "dropdown_options", "select_dropdown"}

# Bookkeeping: no browser side effect worth replaying.
BOOKKEEPING_ACTIONS = {
    "done", "wait", "screenshot", "extract", "find_text",
    "write_file", "replace_file", "read_file", "evaluate",
}

# Actions that change state before they can fail. A failed one may have left
# the page altered, so it is replayed rather than assumed to be a no-op.
NON_IDEMPOTENT_ACTIONS = {"check", "uncheck", "input", "select_dropdown"}

# Navigation always matters: it decides which page later steps run against.
NAVIGATION_ACTIONS = {"navigate", "search", "go_back", "switch", "close"}


class PruneDecision(BaseModel):
    step: int
    action_type: str
    kept: bool
    reason: str


def _next_element_hash(steps: list[StepRecord], after: int) -> int | None:
    """The element hash of the next element-targeting step after index `after`."""
    for s in steps[after + 1:]:
        if s.action.type in ELEMENT_ACTIONS and s.element and s.element.element_hash is not None:
            return s.element.element_hash
    return None


def prune(steps: list[StepRecord]) -> tuple[list[StepRecord], list[PruneDecision]]:
    kept: list[StepRecord] = []
    decisions: list[PruneDecision] = []

    def decide(step: StepRecord, keep: bool, reason: str):
        decisions.append(
            PruneDecision(step=step.step, action_type=step.action.type, kept=keep, reason=reason)
        )
        if keep:
            kept.append(step)

    for i, s in enumerate(steps):
        kind = s.action.type

        if kind in BOOKKEEPING_ACTIONS:
            decide(s, False, "bookkeeping action with no replayable browser effect")
            continue

        if kind in NAVIGATION_ACTIONS:
            decide(s, True, "navigation decides which page later steps run against")
            continue

        if kind == "scroll":
            target = _next_element_hash(steps, i)
            if target is not None and target in s.selector_map_hashes:
                decide(s, False, "next target already existed; replay scrolls into view itself")
            else:
                decide(s, True, "next target absent before this scroll; scroll may have created it")
            continue

        if not s.success:
            if kind in NON_IDEMPOTENT_ACTIONS:
                decide(s, True, "failed but not idempotent; may have changed state before failing")
            else:
                decide(s, False, "action failed and is idempotent")
            continue

        if kept:
            prev = kept[-1]
            same_kind = prev.action.type == kind
            prev_hash = prev.element.element_hash if prev.element else None
            this_hash = s.element.element_hash if s.element else None
            if same_kind and prev_hash is not None and prev_hash == this_hash:
                decide(s, False, "consecutive duplicate on the same element")
                continue

        decide(s, True, "kept")

    return kept, decisions
