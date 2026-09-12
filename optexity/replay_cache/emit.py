"""Turn a pruned trace into a cache entry and a deterministic automation.

Commands are emitted without `.first`. In Playwright's Python implementation
`.first` rewrites the selector to `>> nth=0`, which exempts the locator from
strict mode — exactly the check that stops an ambiguous locator acting on the
wrong element. Uniqueness is enforced by the replay gate instead.
"""

from datetime import datetime, timezone

from optexity.inference.core.interaction.utils import LocatorExtraction
from optexity.replay_cache.adapter import to_scorer_element
from optexity.replay_cache.key import origin_of
from optexity.replay_cache.prune import prune
from optexity.replay_cache.records import StepRecord
from optexity.replay_cache.store import CachedStep, CacheEntry

# browser-use action name -> optexity interaction_action field name
#
# `upload_file` is deliberately excluded. The trace records what was
# uploaded from the recording machine, but carries no file provenance a
# later replay on a different machine could resolve — there is no path on
# disk or URL to hand to UploadFileAction. Emitting a node for it would
# produce an automation that fails schema validation (UploadFileAction
# requires exactly one of file_path/file_url, and we have neither).
# build_entry already skips any action type absent from this mapping, so
# leaving it out here is sufficient to drop upload steps from the cache
# entry; the step that triggered them is simply not replayable from cache.
ACTION_TO_INTERACTION = {
    "click": "click_element",
    "input": "input_text",
    "select_dropdown": "select_option",
}

_METHOD_FOR = {
    "click": ".click()",
    "input": ".fill()",
    "select_dropdown": ".select_option()",
}


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


def build_entry(
    key: str,
    endpoint_name: str,
    url: str,
    steps: list[StepRecord],
) -> CacheEntry:
    kept, _decisions = prune(steps)
    cached: list[CachedStep] = []

    for s in kept:
        if s.action.type not in ACTION_TO_INTERACTION or s.element is None:
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
                prompt_instructions=(
                    f"{s.action.type} the element previously identified as "
                    f"{s.element.ax_name or s.element.attributes.get('id') or s.element.tag_name}"
                ),
            )
        )

    return CacheEntry(
        key=key,
        endpoint_name=endpoint_name,
        origin=origin_of(url),
        created_at=datetime.now(timezone.utc).isoformat(),
        steps=cached,
    )


def to_automation(entry: CacheEntry, url: str, input_parameters: dict) -> dict:
    """Render a cache entry as an optexity Automation document."""
    nodes = []
    for s in entry.steps:
        field = ACTION_TO_INTERACTION[s.action_type]
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
            "input_parameters": input_parameters or {},
            "generated_parameters": {},
        },
        "nodes": nodes,
    }
