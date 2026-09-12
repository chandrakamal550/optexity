"""Drop exploration noise from a recorded trace.

Conservative by construction: pruning a needed step breaks replay silently,
while keeping a redundant one costs milliseconds. The rules below generate a
candidate sequence, and whether that sequence was right is decided by running
the emitted automation, not by this module's confidence in its own heuristics.

That verification is currently manual: the emitter does NOT replay what it
produces, and there is no automatic widen-on-failure loop. Design §6.4
describes one — emit, run, restore the most recently dropped class on failure,
retry — and it is not implemented here. Anyone relying on "the pruner is
self-checking" would be wrong; a bad prune surfaces when a human runs the
cached automation and sees it fail.
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

# Actions that mutate page state before they can fail. A failed one may have left
# the page altered, so it is replayed rather than assumed to be a no-op. `input` clears
# the field before typing, so a mid-way failure leaves it blanked. `send_keys` may have
# dispatched part of its keystroke sequence. `upload_file` can attach before failing.
NON_IDEMPOTENT_ACTIONS = {"input", "select_dropdown", "send_keys", "upload_file"}

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
            # Deliberately decided before the `if not s.success` branch below, so
            # a scroll's success flag is never consulted: browser-use's multi-page
            # scroll swallows per-page failures and returns success anyway
            # (tools/service.py:738-770), so the flag cannot distinguish a full
            # scroll from a partial one. Only the selector map can.
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
