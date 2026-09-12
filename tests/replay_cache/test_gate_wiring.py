"""Verifies memory.replay_cache is actually populated by the real
command_based_action_with_retry code path, not just by calling
ReplayCounters.record() directly (that's covered in test_counters.py).
"""

from optexity.inference.core.interaction.handle_command import (
    command_based_action_with_retry,
)
from optexity.schema.actions.interaction_action import ClickElementAction
from optexity.schema.memory import Memory


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
