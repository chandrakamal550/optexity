import logging
import os
import time
from pathlib import Path

from browser_use import Agent, BrowserSession, Tools

from optexity.inference.infra.browser import Browser
from optexity.inference.models import normalize_model
from optexity.inference.models.chat_litellm import build_agent_llm
from optexity.replay_cache.metrics import from_history
from optexity.schema.actions.interaction_action import (
    AgenticTask,
    CloseOverlayPopupAction,
)
from optexity.schema.memory import Memory
from optexity.schema.task import Task

logger = logging.getLogger(__name__)

REPLAY_CACHE_TRACE_ENV = "OPTEXITY_REPLAY_CACHE_TRACE"

# Off-switch for the recorder itself. Recording costs one hash of the whole
# selector map per step plus a JSONL append, on every agentic task in the
# codebase — including tasks that will never build a cache entry. On by
# default (a trace is worthless if it has to be requested before the run that
# needed it), but it must be possible to turn off without a deploy.
REPLAY_CACHE_RECORD_ENV = "OPTEXITY_REPLAY_CACHE_RECORD"
_FALSY = {"0", "false", "no", "off"}


def recording_enabled() -> bool:
    """True unless OPTEXITY_REPLAY_CACHE_RECORD is explicitly set to a falsy value."""
    try:
        return os.environ.get(REPLAY_CACHE_RECORD_ENV, "").strip().lower() not in _FALSY
    except Exception:
        # Matches the recorder's own posture: a failure to read configuration
        # must never be able to break an agentic task.
        return True


def trace_path_for(task: Task, memory: Memory) -> Path:
    """Where this agentic step's raw trace is written."""
    return (
        task.logs_directory
        / f"step_{memory.automation_state.step_index}"
        / "replay_cache_trace.jsonl"
    )


async def handle_agentic_task(
    agentic_task_action: AgenticTask | CloseOverlayPopupAction,
    task: Task,
    memory: Memory,
    browser: Browser,
):

    if agentic_task_action.backend == "browser_use":

        if isinstance(agentic_task_action, CloseOverlayPopupAction):
            tools = Tools(
                exclude_actions=[
                    "search",
                    "navigate",
                    "go_back",
                    "upload_file",
                    "scroll",
                    "find_text",
                    "send_keys",
                    "evaluate",
                    "switch",
                    "close",
                    "extract",
                    "dropdown_options",
                    "select_dropdown",
                    "write_file",
                    "read_file",
                    "replace_file",
                ]
            )
        else:
            tools = Tools()
        llm = build_agent_llm(normalize_model(task.llm_provider, task.llm_model_name))
        browser_session = BrowserSession(
            cdp_url=browser.cdp_url, keep_alive=agentic_task_action.keep_alive
        )

        step_directory = (
            task.logs_directory / f"step_{str(memory.automation_state.step_index)}"
        )
        step_directory.mkdir(parents=True, exist_ok=True)

        trace_path = trace_path_for(task, memory)
        recording = recording_enabled()
        if recording:
            os.environ[REPLAY_CACHE_TRACE_ENV] = str(trace_path)
            logger.debug(f"Replay-cache trace for this step: {trace_path}")
        else:
            # The recorder is inert unless the env var names a path, so
            # clearing it is the whole off-switch.
            os.environ.pop(REPLAY_CACHE_TRACE_ENV, None)
            logger.debug(
                f"Replay-cache recording disabled by {REPLAY_CACHE_RECORD_ENV}"
            )

        try:
            agent = Agent(
                task=agentic_task_action.task,
                llm=llm,
                browser_session=browser_session,
                use_vision=agentic_task_action.use_vision,
                tools=tools,
                calculate_cost=True,
                save_conversation_path=step_directory,
            )
            logger.debug(f"Starting browser session for agentic task {browser.cdp_url} ")
            await agent.browser_session.start()
            logger.debug(f"Finally running agentic task on browser_use {browser.cdp_url} ")
            run_started_at = time.monotonic()
            history = await agent.run(max_steps=agentic_task_action.max_steps)
            wall_clock_s = time.monotonic() - run_started_at
            logger.debug(f"Agentic task completed on browser_use {browser.cdp_url} ")

            # Diagnostics only: must never be able to skip the cleanup below.
            # A pydantic error building RunMetrics or a formatting error in
            # the log line would otherwise leave agent.stop() /
            # browser_session.stop() / .reset() unreached for every agentic
            # task in the codebase, not only replay-cache ones.
            try:
                run_metrics = from_history(history, wall_clock_s)
                memory.agentic_run_metrics.append(run_metrics)
                logger.info(
                    f"Agentic run metrics: wall_clock={run_metrics.wall_clock_s:.2f}s "
                    f"llm_calls={run_metrics.llm_calls} "
                    f"prompt_tokens={run_metrics.prompt_tokens} "
                    f"completion_tokens={run_metrics.completion_tokens} "
                    f"steps={run_metrics.steps}"
                )
            except Exception as e:
                logger.debug(
                    f"Replay-cache run-metrics capture skipped: {type(e).__name__}: {e}"
                )

            agent.stop()
            if agent.browser_session:
                await agent.browser_session.stop()
                await agent.browser_session.reset()
        finally:
            os.environ.pop(REPLAY_CACHE_TRACE_ENV, None)

        try:
            if trace_path.exists():
                with trace_path.open() as fh:
                    record_count = sum(1 for _ in fh)
                logger.info(
                    f"Replay-cache trace written: {trace_path} "
                    f"({record_count} step records)"
                )
        except Exception as e:
            logger.debug(
                f"Replay-cache trace log skipped: {type(e).__name__}: {e}"
            )

        return history

    elif agentic_task_action.backend == "browserbase":
        raise NotImplementedError("Browserbase is not supported yet")

    return None
