"""Verifies memory.replay_cache is actually populated by the real
command_based_action_with_retry code path, not just by calling
ReplayCounters.record() directly (that's covered in test_counters.py).

Also pins the retry loop's *timing* semantics. The gate was originally
inserted ahead of the pre-existing ``wait_for(state="visible")``, and
``Locator.count()`` does not auto-wait — so an element that entered the DOM a
few hundred milliseconds late made the gate return 0 instantly and every retry
burned with zero elapsed time. The waiting tests below fail against that
arrangement.
"""

import asyncio
import time

from optexity.inference.core.interaction.handle_command import (
    command_based_action_with_retry,
)
from optexity.schema.actions.interaction_action import ClickElementAction
from optexity.schema.memory import BrowserState, Memory


class FakeLocator:
    def __init__(self, n, visible=False):
        self._n = n
        self._visible = visible

    async def count(self):
        return self._n

    async def is_visible(self):
        return self._visible


class FakeBrowser:
    """Maps command string -> number of elements it matches.

    Deliberately reports the locator as not-visible: that keeps the test
    inside the well-guarded part of command_based_action_with_retry (the
    gate resolution and the counter record()) without needing to fake the
    rest of the click pipeline (screenshots, axtree capture, memory
    browser_states). What is under test here is that resolve_unique's
    outcome reaches memory.replay_cache, not the click itself.
    """

    def __init__(self, counts):
        self.counts = counts

    async def get_locator_from_command(self, command):
        if command not in self.counts:
            return None
        return FakeLocator(self.counts[command])


async def test_resolved_outcome_increments_hits_through_the_real_handle_command_path():
    browser = FakeBrowser({'locator("#a")': 1})
    memory = Memory(unique_child_arn="test")
    action = ClickElementAction(command='locator("#a")')

    last_error = await command_based_action_with_retry(
        action=action,
        browser=browser,
        memory=memory,
        task=None,
        max_tries=1,
        max_timeout_seconds_per_try=0.01,
    )

    assert memory.replay_cache.hits == 1
    assert memory.replay_cache.chain_recoveries == 0
    assert memory.replay_cache.escalations == 0
    # Not visible, so the action itself never runs, but that's orthogonal
    # to the thing under test: the gate outcome was still recorded.
    assert last_error == "error: locator not visible"


async def test_escalated_outcome_increments_escalations_through_the_real_handle_command_path():
    browser = FakeBrowser({})  # primary command matches nothing, no candidates
    memory = Memory(unique_child_arn="test")
    action = ClickElementAction(command='locator("#missing")')

    await command_based_action_with_retry(
        action=action,
        browser=browser,
        memory=memory,
        task=None,
        max_tries=1,
        max_timeout_seconds_per_try=0.01,
    )

    assert memory.replay_cache.hits == 0
    assert memory.replay_cache.chain_recoveries == 0
    assert memory.replay_cache.escalations == 1


class LateLocator:
    """A locator whose element only enters the DOM at `appears_at`.

    Models the two Playwright behaviours that matter here, and only those:
    ``count()`` does NOT auto-wait, ``wait_for()`` does. Everything else is
    the minimum needed for one click to complete.
    """

    def __init__(self, appears_at: float):
        self.appears_at = appears_at
        self.clicked = False
        self.wait_for_calls = 0

    def _present(self) -> bool:
        return time.monotonic() >= self.appears_at

    async def count(self):
        return 1 if self._present() else 0

    async def wait_for(self, state, timeout):
        self.wait_for_calls += 1
        deadline = time.monotonic() + timeout / 1000.0
        while not self._present():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timeout waiting for {state}")
            await asyncio.sleep(0.01)

    async def is_visible(self):
        return self._present()

    async def scroll_into_view_if_needed(self, timeout=None):
        return None

    async def bounding_box(self):
        return None

    async def click(self, **kwargs):
        self.clicked = True


class LateBrowser:
    """Returns the same LateLocator for every command."""

    def __init__(self, locator):
        self.locator = locator

    async def get_locator_from_command(self, command):
        return self.locator

    async def get_current_page(self):
        return None

    async def get_screenshot(self):
        return None

    async def get_current_page_url(self):
        return "https://example.com/"

    async def get_current_page_title(self):
        return "example"


def _memory_with_a_browser_state() -> Memory:
    memory = Memory(unique_child_arn="test")
    memory.browser_states.append(BrowserState(url="https://example.com/"))
    return memory


async def test_element_that_appears_late_is_waited_for_and_the_action_still_runs():
    """The regression the gate introduced: count() returns 0 for an element
    that is simply not in the DOM yet, so the gate declined and `continue`
    skipped both the wait_for and the retry backoff. All max_tries burned in
    ~0 seconds and the click never happened."""
    locator = LateLocator(appears_at=time.monotonic() + 0.3)
    browser = LateBrowser(locator)
    memory = _memory_with_a_browser_state()
    action = ClickElementAction(command='locator("#late")')

    started = time.monotonic()
    last_error = await command_based_action_with_retry(
        action=action,
        browser=browser,
        memory=memory,
        task=None,
        max_tries=3,
        max_timeout_seconds_per_try=1.0,
    )
    elapsed = time.monotonic() - started

    assert locator.wait_for_calls >= 1, "the pre-existing wait_for was skipped"
    assert elapsed >= 0.3, f"loop did not wait for the element (elapsed={elapsed:.2f}s)"
    assert locator.clicked, "the action never ran"
    assert last_error is None
    assert memory.replay_cache.hits == 1


async def test_gate_decline_keeps_the_retry_backoff():
    """When the element never shows up, the loop must still spend
    max_tries * max_timeout_seconds_per_try, exactly as every other failure
    branch does. A gate `continue` with no sleep makes the retry chain free
    and therefore useless."""
    # Far enough in the future that it never appears during this test.
    locator = LateLocator(appears_at=time.monotonic() + 3600)
    browser = LateBrowser(locator)
    memory = _memory_with_a_browser_state()
    action = ClickElementAction(command='locator("#never")')

    started = time.monotonic()
    last_error = await command_based_action_with_retry(
        action=action,
        browser=browser,
        memory=memory,
        task=None,
        max_tries=3,
        max_timeout_seconds_per_try=0.1,
    )
    elapsed = time.monotonic() - started

    assert not locator.clicked
    assert last_error is not None
    # 3 tries * 0.1s backoff, plus the one wait_for on try 0. Allow slack for
    # scheduler jitter but demand the loop is not free.
    assert elapsed >= 0.3, f"retry loop kept no backoff (elapsed={elapsed:.2f}s)"


async def test_gate_records_once_per_action_not_once_per_try():
    """A single click exhausting three tries is one escalation, not three:
    these counters are the instrument that makes 'the cached run worked'
    falsifiable, and per-try recording turns them into a retry counter."""
    locator = LateLocator(appears_at=time.monotonic() + 3600)
    browser = LateBrowser(locator)
    memory = _memory_with_a_browser_state()
    action = ClickElementAction(command='locator("#never")')

    await command_based_action_with_retry(
        action=action,
        browser=browser,
        memory=memory,
        task=None,
        max_tries=3,
        max_timeout_seconds_per_try=0.01,
    )

    assert memory.replay_cache.escalations == 1
    assert memory.replay_cache.total == 1
