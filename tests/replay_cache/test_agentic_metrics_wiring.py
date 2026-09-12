"""Verifies memory.agentic_run_metrics is actually populated by the real
handle_agentic_task() code path (not just by calling from_history() directly,
that's covered in test_metrics.py), and that a raising metrics-capture step
can never prevent agent/browser-session cleanup.

This is the covering test for Task 8 fix round 1, finding 2: metrics code
had been inserted between `agent.run()` and `agent.stop()` /
`browser_session.stop()` / `.reset()` with nothing guarding it, so any
exception building RunMetrics or formatting the log line would have skipped
cleanup for every agentic task in the codebase. Nothing failed if the
`memory.agentic_run_metrics.append(...)` call were silently reverted either
-- exactly the "defined but never wired" defect class Amendment 1 (Task 8)
existed to prevent, already paid for once with ReplayCounters (Task 7).
"""

import os
from types import SimpleNamespace

import optexity.inference.core.interaction.handle_agentic_task as hat
from optexity.schema.actions.interaction_action import AgenticTask
from optexity.schema.memory import Memory


class FakeBrowserSession:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.reset_called = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def reset(self):
        self.reset_called = True


class FakeHistory:
    def __init__(self):
        self.usage = SimpleNamespace(
            total_prompt_tokens=10, total_completion_tokens=5, total_tokens=15
        )
        self.history = [1, 2]


class FakeAgent:
    """Stands in for browser_use.Agent. Records whether stop() was called so
    tests can assert cleanup happened regardless of what the metrics block
    did."""

    last_instance = None

    def __init__(self, **kwargs):
        self.browser_session = kwargs["browser_session"]
        self.stopped = False
        FakeAgent.last_instance = self

    async def run(self, max_steps):
        # What the recorder in the browser-use fork would see: it is inert
        # unless this env var names a path.
        self.trace_env_during_run = os.environ.get(hat.REPLAY_CACHE_TRACE_ENV)
        return FakeHistory()

    def stop(self):
        self.stopped = True


def _patch_agent_deps(monkeypatch):
    monkeypatch.setattr(hat, "build_agent_llm", lambda model: object())
    monkeypatch.setattr(hat, "normalize_model", lambda provider, name: object())
    monkeypatch.setattr(
        hat, "BrowserSession", lambda cdp_url, keep_alive: FakeBrowserSession()
    )
    monkeypatch.setattr(hat, "Agent", FakeAgent)


def _make_task(tmp_path):
    return SimpleNamespace(
        logs_directory=tmp_path,
        llm_provider="anthropic",
        llm_model_name="claude",
    )


def _make_browser():
    return SimpleNamespace(cdp_url="http://localhost:9222")


async def test_successful_run_records_metrics_and_still_cleans_up(monkeypatch, tmp_path):
    _patch_agent_deps(monkeypatch)
    memory = Memory(unique_child_arn="test")
    memory.automation_state.step_index = 0
    action = AgenticTask(task="do something", max_steps=5)

    history = await hat.handle_agentic_task(
        action, _make_task(tmp_path), memory, _make_browser()
    )

    assert history is not None
    assert len(memory.agentic_run_metrics) == 1
    m = memory.agentic_run_metrics[0]
    assert m.prompt_tokens == 10
    assert m.completion_tokens == 5
    assert m.steps == 2

    agent = FakeAgent.last_instance
    assert agent.stopped is True
    assert agent.browser_session.stopped is True
    assert agent.browser_session.reset_called is True


async def test_raising_metrics_capture_does_not_prevent_cleanup(monkeypatch, tmp_path):
    _patch_agent_deps(monkeypatch)

    def _boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(hat, "from_history", _boom)

    memory = Memory(unique_child_arn="test")
    memory.automation_state.step_index = 0
    action = AgenticTask(task="do something", max_steps=5)

    history = await hat.handle_agentic_task(
        action, _make_task(tmp_path), memory, _make_browser()
    )

    # The run itself still completes and returns history...
    assert history is not None
    # ...but nothing was recorded, since from_history() raised.
    assert memory.agentic_run_metrics == []

    # The whole point: cleanup ran anyway.
    agent = FakeAgent.last_instance
    assert agent.stopped is True
    assert agent.browser_session.stopped is True
    assert agent.browser_session.reset_called is True


async def test_recording_is_on_by_default(monkeypatch, tmp_path):
    """A trace is worthless if it has to be requested before the run that
    needed it, so the recorder defaults to on."""
    _patch_agent_deps(monkeypatch)
    monkeypatch.delenv(hat.REPLAY_CACHE_RECORD_ENV, raising=False)
    monkeypatch.delenv(hat.REPLAY_CACHE_TRACE_ENV, raising=False)
    memory = Memory(unique_child_arn="test")
    memory.automation_state.step_index = 0

    await hat.handle_agentic_task(
        AgenticTask(task="do something", max_steps=5),
        _make_task(tmp_path),
        memory,
        _make_browser(),
    )

    assert FakeAgent.last_instance.trace_env_during_run == str(
        hat.trace_path_for(_make_task(tmp_path), memory)
    )


async def test_recording_can_be_switched_off_without_a_deploy(monkeypatch, tmp_path):
    """Every agentic task in the codebase hashes the whole selector map once
    per step and appends JSONL. Defensible as a default, but it must be
    possible to turn off from configuration."""
    _patch_agent_deps(monkeypatch)
    monkeypatch.setenv(hat.REPLAY_CACHE_RECORD_ENV, "0")
    monkeypatch.setenv(hat.REPLAY_CACHE_TRACE_ENV, "/should/be/cleared.jsonl")
    memory = Memory(unique_child_arn="test")
    memory.automation_state.step_index = 0

    await hat.handle_agentic_task(
        AgenticTask(task="do something", max_steps=5),
        _make_task(tmp_path),
        memory,
        _make_browser(),
    )

    # Not merely "a different path": the recorder must see nothing at all.
    assert FakeAgent.last_instance.trace_env_during_run is None


def test_only_explicit_falsy_values_disable_recording(monkeypatch):
    for value in ("0", "false", "FALSE", " no ", "off"):
        monkeypatch.setenv(hat.REPLAY_CACHE_RECORD_ENV, value)
        assert hat.recording_enabled() is False, value
    for value in ("1", "true", "yes", "on", "", "anything-else"):
        monkeypatch.setenv(hat.REPLAY_CACHE_RECORD_ENV, value)
        assert hat.recording_enabled() is True, value
    monkeypatch.delenv(hat.REPLAY_CACHE_RECORD_ENV, raising=False)
    assert hat.recording_enabled() is True
