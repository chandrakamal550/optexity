import asyncio
import logging
import time

from playwright.async_api import Locator

from optexity.exceptions import (
    AssertLocatorPresenceException,
    ExpectedDownloadFailedException,
)
from optexity.inference.core.interaction.handle_select_utils import (
    SelectOptionValue,
    smart_select,
)
from optexity.inference.core.interaction.utils import (
    LocatorExtraction,
    handle_download,
    highlight_element_and_screenshot,
)
from optexity.inference.infra.browser import Browser
from optexity.replay_cache.gate import resolve_unique
from optexity.schema.actions.interaction_action import (
    CheckAction,
    ClickElementAction,
    HoverAction,
    InputTextAction,
    SelectOptionAction,
    UncheckAction,
    UploadFileAction,
)
from optexity.schema.memory import BrowserState, Memory
from optexity.schema.task import Task

logger = logging.getLogger(__name__)


def _action_method(action) -> str:
    """The trailing Playwright call for an action, e.g. ``.click(button='left')`` —
    appended to the heuristically-derived locator so command steps record the same
    ``page.<locator><method>`` shape as the LLM-fallback path."""
    if isinstance(action, ClickElementAction):
        return (
            ".dblclick()"
            if action.double_click
            else f".click(button={action.button!r})"
        )
    if isinstance(action, InputTextAction):
        verb = "type" if action.fill_or_type == "type" else "fill"
        return f".{verb}({(action.input_text or '')!r})"
    if isinstance(action, SelectOptionAction):
        return f".select_option({action.select_values!r})"
    if isinstance(action, CheckAction):
        return ".check()"
    if isinstance(action, UncheckAction):
        return ".uncheck()"
    if isinstance(action, HoverAction):
        return ".hover()"
    if isinstance(action, UploadFileAction):
        return f".set_input_files({action.file_path!r})"
    return ""


async def command_based_action_with_retry(
    action: (
        ClickElementAction
        | InputTextAction
        | SelectOptionAction
        | CheckAction
        | UploadFileAction
        | UncheckAction
        | HoverAction
    ),
    browser: Browser,
    memory: Memory,
    task: Task,
    max_tries: int,
    max_timeout_seconds_per_try: float,
) -> str | None:
    """Run the retry loop, and record the replay gate's verdict exactly once.

    Once *per action*, not once per try: an action that exhausts three tries is
    one escalation, not three. Recording inside the loop turned these counters
    — the instrument that makes "the cached run worked" falsifiable — into a
    retry counter.
    """
    if action.command is None or action.skip_command:
        return

    # `outcome_box` carries the last gate outcome out of the loop, including
    # when the loop exits by raising.
    outcome_box: list = []
    try:
        return await _command_retry_loop(
            action,
            browser,
            memory,
            task,
            max_tries,
            max_timeout_seconds_per_try,
            outcome_box,
        )
    finally:
        if outcome_box:
            memory.replay_cache.record(outcome_box[-1])


async def _command_retry_loop(
    action,
    browser: Browser,
    memory: Memory,
    task: Task,
    max_tries: int,
    max_timeout_seconds_per_try: float,
    outcome_box: list,
) -> str | None:

    last_error = None

    logger.debug(f"Executing command-based action: {action.__class__.__name__}")

    for try_index in range(max_tries):
        last_error = None
        try:
            # https://playwright.dev/docs/actionability
            #
            # The wait comes BEFORE the replay gate, deliberately, and must stay
            # there. Locator.count() does not auto-wait: an element that enters
            # the DOM a few hundred milliseconds after the action starts (a
            # post-click render, an SPA route change) makes the gate see 0
            # matches at t=0. Gating first therefore declined instantly, and the
            # `continue` below skipped both this wait_for and the retry backoff
            # — every try burned with zero elapsed time, for every automation,
            # cached or not. Resolve the primary command, wait on it exactly as
            # the pre-gate code did, and only then check uniqueness.
            primary_locator = await browser.get_locator_from_command(action.command)
            if primary_locator is not None and try_index == 0:
                try:
                    await primary_locator.wait_for(
                        state="visible", timeout=max_timeout_seconds_per_try * 1000
                    )
                except Exception:
                    # Timeout, strict-mode ambiguity, detached node: all of these
                    # are the gate's business to classify, not this wait's.
                    pass

            outcome = await resolve_unique(
                browser, action.command, getattr(action, "locator_candidates", None) or []
            )
            outcome_box.append(outcome)
            if not outcome.resolved:
                logger.warning(
                    f"Replay gate declined {action.__class__.__name__}: {outcome.reason}"
                )
                last_error = f"error: {outcome.reason}"
                # Same backoff every other failure branch takes. Without it the
                # retry chain costs nothing and therefore buys nothing.
                await asyncio.sleep(max_timeout_seconds_per_try)
                continue
            if outcome.candidate_rank > 0:
                logger.info(
                    f"Replay gate recovered via candidate rank {outcome.candidate_rank}: "
                    f"{outcome.command}"
                )
            locator = await browser.get_locator_from_command(outcome.command)
            if locator is None:
                last_error = "error: gate command did not resolve to a locator"
                await asyncio.sleep(max_timeout_seconds_per_try)
                continue
            is_visible = await locator.is_visible()

            if is_visible:
                await locator.scroll_into_view_if_needed(
                    timeout=max_timeout_seconds_per_try * 1000
                )
                await asyncio.sleep(0.05)

                try:
                    page = await browser.get_current_page()
                    bbox = await locator.bounding_box() if page else None
                    if page and bbox:
                        screenshot = await highlight_element_and_screenshot(
                            page, browser, bbox
                        )
                    else:
                        screenshot = await browser.get_screenshot()
                except Exception as e:
                    logger.error(f"Error in command_based_action_with_retry: {e}")
                    screenshot = await browser.get_screenshot()

                # Capture the axtree for this step's log too (command steps otherwise
                # have none). Done here, after the highlight overlay is removed and
                # before the action runs, so — exactly like the screenshot above — it is
                # a deterministic pre-action snapshot of this (latest) attempt. Skip the
                # redundant screenshot inside the summary to keep the added time to just
                # the DOM/AX serialization. Logging-only; never blocks control flow.
                axtree = None
                axtree_capture_start = time.perf_counter()
                try:
                    summary = await browser.get_browser_state_summary(
                        include_screenshot=False
                    )
                    axtree = summary.dom_state.llm_representation(
                        remove_empty_nodes=task.automation.remove_empty_nodes_in_axtree
                    )
                    logger.debug(
                        f"Command-step axtree capture took "
                        f"{(time.perf_counter() - axtree_capture_start) * 1000:.0f}ms "
                        f"({len(axtree) if axtree else 0} chars)"
                    )
                except Exception as e:
                    logger.debug(
                        f"Failed to capture axtree for command step after "
                        f"{(time.perf_counter() - axtree_capture_start) * 1000:.0f}ms: "
                        f"{type(e).__name__}: {e}"
                    )

                # Resolve the element this command targets and collect all candidate
                # locators for it via the heuristic (not just an echo of the command).
                # Pure logging: guarded so a failure here can never skip the action below.
                locator_candidates = None
                try:
                    locator_candidates = (
                        await LocatorExtraction.locator_from_playwright(
                            locator, _action_method(action), action.command
                        )
                    )
                    logger.debug(
                        f"Command-step locator candidates: {locator_candidates}"
                    )
                except Exception as e:
                    logger.debug(
                        f"Failed to record command-step locators: "
                        f"{type(e).__name__}: {e}"
                    )

                memory.browser_states[-1] = BrowserState(
                    url=await browser.get_current_page_url(),
                    screenshot=screenshot,
                    title=await browser.get_current_page_title(),
                    axtree=axtree,
                    locator_candidates=locator_candidates,
                )

                if isinstance(action, ClickElementAction):
                    await click_locator(
                        action,
                        locator,
                        browser,
                        memory,
                        task,
                        max_timeout_seconds_per_try,
                    )
                elif isinstance(action, InputTextAction):
                    await input_text_locator(
                        action, locator, browser, max_timeout_seconds_per_try
                    )
                elif isinstance(action, SelectOptionAction):
                    await select_option_locator(
                        action,
                        locator,
                        browser,
                        memory,
                        task,
                        max_timeout_seconds_per_try,
                    )
                elif isinstance(action, CheckAction):
                    await check_locator(
                        action, locator, max_timeout_seconds_per_try, browser
                    )
                elif isinstance(action, UncheckAction):
                    await uncheck_locator(
                        action, locator, max_timeout_seconds_per_try, browser
                    )
                elif isinstance(action, HoverAction):
                    await hover_locator(locator, max_timeout_seconds_per_try)
                elif isinstance(action, UploadFileAction):
                    await upload_file_locator(action, locator)
                logger.debug(
                    f"{action.__class__.__name__} successful on try {try_index + 1}"
                )
                return
            else:
                await asyncio.sleep(max_timeout_seconds_per_try)
                last_error = f"error: locator not visible"
        except ExpectedDownloadFailedException:
            # The action ran but expect_download=True produced no file. Do not
            # retry or downgrade to a string error; fail the task with the fixed
            # message.
            raise
        except Exception as e:
            last_error = f"error: {e}"
            await asyncio.sleep(max_timeout_seconds_per_try)

    if last_error is None:
        last_error = "error in executing command"
    logger.debug(
        f"{action.__class__.__name__} failed after {max_tries} tries: {last_error}"
    )

    if last_error and action.assert_locator_presence:
        logger.debug(
            f"Error in {action.__class__.__name__} with assert_locator_presence: {action.__class__.__name__}: {last_error}"
        )
        raise AssertLocatorPresenceException(
            message=f"Error in {action.__class__.__name__} with assert_locator_presence: {action.__class__.__name__}",
            original_error=last_error,
            command=action.command,
        )
    return last_error


async def click_locator(
    click_element_action: ClickElementAction,
    locator: Locator,
    browser: Browser,
    memory: Memory,
    task: Task,
    max_timeout_seconds_per_try: float,
):
    async def _actual_click():
        if click_element_action.mouse_click:
            page = await browser.get_current_page()
            if page is None:
                raise RuntimeError(
                    "click_locator(mouse_click=true): browser.get_current_page() returned None"
                )

            bbox = await locator.bounding_box()
            if bbox is None:
                # Fallback if Playwright can't compute the bounding-box.
                if click_element_action.double_click:
                    await locator.dblclick(
                        no_wait_after=True,
                        timeout=max_timeout_seconds_per_try * 1000,
                    )
                else:
                    await locator.click(
                        button=click_element_action.button,
                        no_wait_after=True,
                        timeout=max_timeout_seconds_per_try * 1000,
                    )
                return

            deviation = click_element_action.mouse_click_deviation or {}
            dx = float(deviation.get("x", 0))
            dy = float(deviation.get("y", 0))

            x = float(bbox["x"]) + dx
            y = float(bbox["y"]) + dy

            # TODO: Remove this later
            # Lightweight visual marker for debugging coordinate clicks.
            await page.evaluate(
                """([x, y]) => {
                    const el = document.createElement('div');
                    el.id = '__optexity_click_marker';
                    el.style.position = 'fixed';
                    el.style.left = `${x - 8}px`;
                    el.style.top = `${y - 8}px`;
                    el.style.width = '16px';
                    el.style.height = '16px';
                    el.style.border = '2px solid red';
                    el.style.borderRadius = '50%';
                    el.style.background = 'rgba(255,0,0,0.25)';
                    el.style.zIndex = '2147483647';
                    el.style.pointerEvents = 'none';
                    document.body.appendChild(el);
                    setTimeout(() => el.remove(), 800);
                }""",
                [x, y],
            )

            if click_element_action.double_click:
                await page.mouse.dblclick(
                    x,
                    y,
                    button=click_element_action.button,
                    timeout=max_timeout_seconds_per_try * 1000,
                )
            else:
                await page.mouse.click(x, y)
        if click_element_action.double_click:
            await locator.dblclick(
                no_wait_after=True,
                timeout=max_timeout_seconds_per_try * 1000,
                force=click_element_action.force,
            )
        else:
            await locator.click(
                button=click_element_action.button,
                no_wait_after=True,
                timeout=max_timeout_seconds_per_try * 1000,
                force=click_element_action.force,
            )

    if click_element_action.expect_download:
        await handle_download(
            _actual_click,
            memory,
            browser,
            task,
            click_element_action.download_filename,
            click_element_action.download_metadata,
        )
    else:
        await _actual_click()


async def input_text_locator(
    input_text_action: InputTextAction,
    locator: Locator,
    browser: Browser,
    max_timeout_seconds_per_try: float,
):

    if input_text_action.fill_or_type == "fill":
        await locator.fill(
            input_text_action.input_text,
            no_wait_after=True,
            timeout=max_timeout_seconds_per_try * 1000,
        )
    elif input_text_action.fill_or_type == "type":
        await locator.type(
            input_text_action.input_text,
            no_wait_after=True,
            timeout=max_timeout_seconds_per_try * 1000,
        )
    else:
        page = await browser.get_current_page()
        if page is None:
            return
        for char in input_text_action.input_text:
            await page.keyboard.press(char)
            await asyncio.sleep(0.1)

    if input_text_action.press_enter:
        await locator.press("Enter")


async def check_locator(
    action: CheckAction,
    locator: Locator,
    max_timeout_seconds_per_try: float,
    browser: Browser,
):
    await locator.uncheck(
        no_wait_after=True, timeout=max_timeout_seconds_per_try * 1000
    )
    await asyncio.sleep(1)
    locator = await browser.get_locator_from_command(action.command)
    await locator.check(no_wait_after=True, timeout=max_timeout_seconds_per_try * 1000)


async def uncheck_locator(
    action: UncheckAction,
    locator: Locator,
    max_timeout_seconds_per_try: float,
    browser: Browser,
):
    await locator.check(no_wait_after=True, timeout=max_timeout_seconds_per_try * 1000)
    await asyncio.sleep(1)
    locator = await browser.get_locator_from_command(action.command)
    await locator.uncheck(
        no_wait_after=True, timeout=max_timeout_seconds_per_try * 1000
    )


async def hover_locator(
    locator: Locator,
    max_timeout_seconds_per_try: float,
):
    await locator.hover(no_wait_after=True, timeout=max_timeout_seconds_per_try * 1000)


async def upload_file_locator(upload_file_action: UploadFileAction, locator: Locator):
    await locator.set_input_files(upload_file_action.file_path)


async def select_option_locator(
    select_option_action: SelectOptionAction,
    locator: Locator,
    browser: Browser,
    memory: Memory,
    task: Task,
    max_timeout_seconds_per_try: float,
):
    async def _actual_select_option():
        options: list[dict[str, str]] = await locator.evaluate("""
        sel => Array.from(sel.options).map(o => ({
            value: o.value,
            label: o.label || o.textContent
        }))
    """)

        select_option_values = [
            SelectOptionValue(value=o["value"], label=o["label"]) for o in options
        ]

        matched_values = await smart_select(
            select_option_values, select_option_action.select_values, memory, task
        )

        logger.debug(
            f"Matched values for {select_option_action.command}: {matched_values}"
        )

        await locator.select_option(
            matched_values,
            no_wait_after=True,
            timeout=max_timeout_seconds_per_try * 1000,
        )

    if select_option_action.expect_download:
        await handle_download(
            _actual_select_option,
            memory,
            browser,
            task,
            select_option_action.download_filename,
            select_option_action.download_metadata,
        )
    else:
        await _actual_select_option()
