from optexity.replay_cache.prune import prune
from optexity.replay_cache.records import ActionRecord, ElementSignals, StepRecord


def step(n, type_, *, index=None, hashes=None, success=True, el_hash=None, error=None):
    return StepRecord(
        step=n,
        url="https://example.com/",
        action=ActionRecord(type=type_, index=index),
        element=ElementSignals(tag_name="a", element_hash=el_hash) if el_hash else None,
        selector_map_hashes=hashes or [],
        success=success,
        error=error,
    )


def kept_types(steps):
    kept, _ = prune(steps)
    return [s.action.type for s in kept]


def test_done_is_always_dropped():
    assert kept_types([step(1, "click", el_hash=1), step(2, "done")]) == ["click"]


def test_redundant_scroll_is_dropped():
    """Target already existed before the scroll, so replay will scroll to it itself."""
    steps = [
        step(1, "scroll", hashes=[77]),
        step(2, "click", index=5, el_hash=77),
    ]
    assert kept_types(steps) == ["click"]


def test_load_bearing_scroll_is_kept():
    """Target did NOT exist before the scroll — the scroll created it."""
    steps = [
        step(1, "scroll", hashes=[10, 11]),
        step(2, "click", index=5, el_hash=77),
    ]
    assert kept_types(steps) == ["scroll", "click"]


def test_failed_send_keys_is_never_pruned():
    """send_keys may dispatch part of its keystroke sequence before failing."""
    steps = [step(1, "send_keys", index=3, el_hash=5, success=False, error="timeout")]
    assert kept_types(steps) == ["send_keys"]


def test_failed_input_is_never_pruned():
    """browser-use clears the field before typing, so a failure leaves it blank."""
    steps = [step(1, "input", index=3, el_hash=5, success=False, error="boom")]
    assert kept_types(steps) == ["input"]


def test_failed_select_dropdown_is_never_pruned():
    """select_dropdown mutates the dropdown state before it can fail."""
    steps = [step(1, "select_dropdown", index=3, el_hash=5, success=False, error="timeout")]
    assert kept_types(steps) == ["select_dropdown"]


def test_failed_click_is_dropped():
    steps = [step(1, "click", index=3, el_hash=5, success=False, error="boom"),
             step(2, "click", index=4, el_hash=6)]
    assert kept_types(steps) == ["click"]
    kept, _ = prune(steps)
    assert kept[0].step == 2


def test_consecutive_duplicate_on_same_element_is_dropped():
    steps = [step(1, "click", index=3, el_hash=42), step(2, "click", index=3, el_hash=42)]
    assert kept_types(steps) == ["click"]


def test_same_element_twice_non_consecutive_is_kept():
    steps = [
        step(1, "click", index=3, el_hash=42),
        step(2, "input", index=4, el_hash=43),
        step(3, "click", index=3, el_hash=42),
    ]
    assert kept_types(steps) == ["click", "input", "click"]


def test_navigation_is_kept():
    steps = [step(1, "navigate"), step(2, "click", index=3, el_hash=1)]
    assert kept_types(steps) == ["navigate", "click"]


def test_bookkeeping_actions_are_dropped():
    steps = [
        step(1, "screenshot"),
        step(2, "wait"),
        step(3, "click", index=3, el_hash=1),
    ]
    assert kept_types(steps) == ["click"]


def test_decisions_explain_every_step():
    steps = [step(1, "scroll", hashes=[77]), step(2, "click", index=5, el_hash=77)]
    _, decisions = prune(steps)
    assert len(decisions) == 2
    assert all(d.reason for d in decisions)
