"""Turn a pruned trace into a cache entry and a deterministic automation.

Commands are emitted without `.first`. In Playwright's Python implementation
`.first` rewrites the selector to `>> nth=0`, which exempts the locator from
strict mode — exactly the check that stops an ambiguous locator acting on the
wrong element. Uniqueness is enforced by the replay gate instead.

Every pruned step the emitter cannot express reaches `CacheEntry.skipped` with
a reason. Dropping silently is how mid-run navigation vanished from the emitted
automation for two whole tasks without anything failing.
"""

import logging
import re
from datetime import datetime, timezone

from optexity.inference.core.interaction.utils import LocatorExtraction
from optexity.replay_cache.adapter import to_scorer_element
from optexity.replay_cache.key import origin_of
from optexity.replay_cache.prune import prune
from optexity.replay_cache.records import StepRecord
from optexity.replay_cache.store import CachedStep, CacheEntry, EmitDecision

logger = logging.getLogger(__name__)

# browser-use action name -> optexity interaction_action field name.
#
# `navigate` is here even though it targets no element: the pruner keeps
# navigation because it decides which page every later step runs against, and
# an automation that drops it replays the rest against the wrong page. It maps
# onto optexity's GoToUrlAction (`go_to_url`), whose only required field is
# `url` — available on the trace as `action.params["url"]`, with the step's own
# `url` as a fallback.
#
# `upload_file` is deliberately excluded. The trace records what was
# uploaded from the recording machine, but carries no file provenance a
# later replay on a different machine could resolve — there is no path on
# disk or URL to hand to UploadFileAction. Emitting a node for it would
# produce an automation that fails schema validation (UploadFileAction
# requires exactly one of file_path/file_url, and we have neither).
ACTION_TO_INTERACTION = {
    "click": "click_element",
    "input": "input_text",
    "select_dropdown": "select_option",
    "navigate": "go_to_url",
}

# The other navigation actions the pruner keeps, and why each is not emitted.
# Stated here rather than left to inference, and every occurrence is reported
# through CacheEntry.skipped so a workflow that used one is not quietly
# mis-replayed. Mapping them is the natural follow-up; see the final fix
# report for why it is not done blind in this change.
EXCLUSION_REASONS = {
    "upload_file": (
        "no file provenance a replay on another machine could resolve; "
        "UploadFileAction requires one of file_path/file_url and the trace has neither"
    ),
    "search": (
        "browser-use's search runs a search-engine query and lands on a URL that is "
        "not knowable until run time; optexity has no equivalent action, and emitting "
        "go_to_url with the recorded result URL would pin a result page that moves"
    ),
    "go_back": (
        "optexity's GoBackAction exists, but history depth at replay depends on every "
        "preceding emitted step, and this emitter drops steps (upload_file, search); "
        "a go_back replayed against a shorter history lands on the wrong page silently"
    ),
    "switch": (
        "browser-use addresses tabs by a 4-char tab_id assigned at run time; optexity's "
        "SwitchTabAction takes a positional tab_index. The two do not convert without "
        "a tab table the trace does not carry"
    ),
    "close": (
        "same tab-identity mismatch as switch: browser-use closes by run-time tab_id, "
        "optexity's close_current_tab closes whatever is focused"
    ),
}

_METHOD_FOR = {
    "click": ".click()",
    "input": ".fill()",
    "select_dropdown": ".select_option()",
}

_XPATH_INDEX = re.compile(r"\[(\d+)\]$")


def command_from_locator(locator_expression: str) -> str:
    """Strip the trailing method call and leading `page.` from a locator expression.

    Turns a `page.<locator>.<method>()` string (as produced by
    ``LocatorExtraction.locator_candidates``) into the bare `<locator>`
    command optexity expects on ``CachedStep.command`` / an automation node's
    ``command`` field.

    Public and shared: Task 7's replay gate imports this directly so the
    normalisation logic exists in exactly one place. Do not make this
    private again or fork a second copy of it.
    """
    for method in (".click()", ".fill()", ".select_option()", ".set_input_files()"):
        if locator_expression.endswith(method):
            locator_expression = locator_expression[: -len(method)]
            break
    return (
        locator_expression[len("page."):]
        if locator_expression.startswith("page.")
        else locator_expression
    )


def _sibling_ordinal(xpath: str) -> int | None:
    """The `[n]` on the xpath's last segment — position among same-tag siblings."""
    match = _XPATH_INDEX.search(xpath.rsplit("/", 1)[-1])
    return int(match.group(1)) if match else None


def prompt_instructions_for(step: StepRecord) -> str:
    """What the LLM index predictor gets when the gate declines this step.

    The earlier version was `"{action} the element previously identified as
    {ax_name or id or tag_name}"`, which on a form of unlabelled inputs
    degenerates to the identical string — "input the element previously
    identified as input" — for every field on the page. That cannot
    discriminate between four visually identical inputs, which is precisely
    the situation the fallback exists to handle. The xpath, the sibling
    ordinal and the value being entered are all already on the record and all
    three discriminate.
    """
    el = step.element
    if el is None:
        return step.action.type
    tag = el.tag_name or "element"
    label = (
        el.ax_name
        or el.attributes.get("id")
        or el.attributes.get("name")
        or el.attributes.get("placeholder")
    )
    head = f"{step.action.type} the <{tag}>"
    if label:
        head += f" labelled {label!r}"
    parts = [head]
    ordinal = _sibling_ordinal(el.xpath or "")
    if ordinal is not None:
        parts.append(f"#{ordinal} among its same-tag siblings")
    if el.xpath:
        parts.append(f"at xpath {el.xpath}")
    value = step.action.params.get("text")
    if value:
        parts.append(f"receiving {value!r}")
    return ", ".join(parts)


def _navigation_step(s: StepRecord) -> tuple[CachedStep | None, EmitDecision]:
    # NavigateAction (browser-use tools/views.py) is `url: str, new_tab: bool`,
    # so params["url"] is the target the agent actually asked for. The step's
    # own `url` is the page the step *started* on, which for the first action
    # of a run is the same thing and otherwise is not — so it is only a
    # fallback for a params dict that lost the field.
    target = (s.action.params.get("url") or "").strip() or (s.url or "").strip()
    if not target:
        return None, EmitDecision(
            step=s.step,
            action_type=s.action.type,
            emitted=False,
            reason="navigate step carried no target URL",
        )
    return (
        CachedStep(
            action_type="navigate",
            command="",
            url=target,
            prompt_instructions=f"navigate to {target}",
        ),
        EmitDecision(
            step=s.step, action_type=s.action.type, emitted=True, reason="navigation"
        ),
    )


def build_entry(
    key: str,
    endpoint_name: str,
    url: str,
    steps: list[StepRecord],
) -> CacheEntry:
    kept, _decisions = prune(steps)
    cached: list[CachedStep] = []
    skipped: list[EmitDecision] = []

    def drop(s: StepRecord, reason: str):
        skipped.append(
            EmitDecision(
                step=s.step, action_type=s.action.type, emitted=False, reason=reason
            )
        )
        logger.warning(
            f"Replay-cache emitter skipped step {s.step} ({s.action.type}): {reason}"
        )

    for s in kept:
        if s.action.type == "navigate":
            step, decision = _navigation_step(s)
            if step is None:
                skipped.append(decision)
                logger.warning(
                    f"Replay-cache emitter skipped step {s.step} "
                    f"({s.action.type}): {decision.reason}"
                )
            else:
                cached.append(step)
            continue

        if s.action.type not in ACTION_TO_INTERACTION:
            drop(
                s,
                EXCLUSION_REASONS.get(
                    s.action.type,
                    "no optexity interaction action maps to this browser-use action",
                ),
            )
            continue

        if s.element is None:
            drop(s, "no element signals were captured for this step")
            continue

        method = _METHOD_FOR.get(s.action.type, ".click()")
        candidates = LocatorExtraction.locator_candidates(
            to_scorer_element(s.element), method
        )
        if not candidates:
            # A step with zero viable candidates carries nothing replayable.
            # Real traces show plenty of single-candidate steps (xpath-only,
            # when id/name are dynamic or absent) — that is normal output,
            # not an error; only an empty list is skipped.
            drop(s, "locator scorer produced no candidates for this element")
            continue
        cached.append(
            CachedStep(
                action_type=s.action.type,
                command=command_from_locator(candidates[0]["locator"]),
                candidates=candidates,
                # "text" is correct for both actions this reads from: browser-use's
                # InputTextAction and SelectDropdownOptionAction (tools/views.py)
                # both name their payload field `text` — verified directly against
                # the browser-use source, not assumed from input's shape.
                input_text=s.action.params.get("text"),
                prompt_instructions=prompt_instructions_for(s),
            )
        )

    return CacheEntry(
        key=key,
        endpoint_name=endpoint_name,
        origin=origin_of(url),
        created_at=datetime.now(timezone.utc).isoformat(),
        steps=cached,
        skipped=skipped,
    )


def _parameters_actually_used(nodes: list[dict], input_parameters: dict) -> dict:
    """Only the parameters the emitted nodes reference.

    Emitted steps carry literal values (spec §3: "Values in steps: literal, not
    parameterized"), so passing the originating task's `input_parameters`
    straight through decorates the automation with parameters it never reads —
    which is how both committed artifacts ended up declaring
    `{"stock_ticker": ["NVDA"]}` on a roboform form and a saucedemo login.
    Placeholders are `{name[index]}` (optexity/schema/automation.py:109), so a
    parameter is used iff `{name[` appears somewhere in the rendered nodes.
    """
    if not input_parameters:
        return {}
    rendered = repr(nodes)
    return {
        name: values
        for name, values in input_parameters.items()
        if f"{{{name}[" in rendered
    }


def to_automation(
    entry: CacheEntry, url: str, input_parameters: dict | None = None
) -> dict:
    """Render a cache entry as an optexity Automation document."""
    nodes = []
    for s in entry.steps:
        field = ACTION_TO_INTERACTION.get(s.action_type)
        if field is None:
            continue
        if s.action_type == "navigate":
            # GoToUrlAction is a plain BaseModel (url, new_tab) — no `command`
            # and no `prompt_instructions`, so this node's shape differs from
            # the element-targeting ones.
            nodes.append(
                {
                    "type": "action_node",
                    "interaction_action": {field: {"url": s.url}},
                }
            )
            continue
        action: dict = {
            "command": s.command,
            "prompt_instructions": s.prompt_instructions,
        }
        if s.action_type == "input":
            action["input_text"] = s.input_text or ""
        elif s.action_type == "select_dropdown":
            # SelectOptionAction.select_values is Optional[list[str]] and
            # defaults to None. A None reaches smart_select's
            # `for p in patterns:` with no guard, raising TypeError at replay
            # for any dropdown with more than two real options. Emit the
            # captured value explicitly so replay never falls through to that
            # (LLM-driven, non-deterministic) prediction path.
            action["select_values"] = [s.input_text] if s.input_text else []
        nodes.append(
            {
                "type": "action_node",
                "interaction_action": {field: action},
            }
        )

    return {
        "url": url,
        "parameters": {
            "input_parameters": _parameters_actually_used(nodes, input_parameters or {}),
            "generated_parameters": {},
        },
        "nodes": nodes,
    }
