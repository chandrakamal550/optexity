import ast

from optexity.replay_cache.emit import build_entry, to_automation
from optexity.replay_cache.records import ActionRecord, ElementSignals, StepRecord

SEARCH_INPUT = StepRecord(
    step=1,
    url="https://www.roboform.com/filling-test-all-fields",
    action=ActionRecord(type="input", index=6, params={"text": "myname"}),
    element=ElementSignals(
        tag_name="input",
        attributes={"id": "firstname", "name": "firstname"},
        xpath="/html/body/form/input[2]",
        element_hash=101,
    ),
)

LINK_CLICK = StepRecord(
    step=2,
    url="https://www.roboform.com/filling-test-all-fields",
    action=ActionRecord(type="click", index=2421),
    element=ElementSignals(
        tag_name="a",
        attributes={"href": "/stocks/nvda/"},
        xpath="/html/body/div/a",
        ax_role="link",
        ax_name="NVDA NVIDIA Corporation Stock",
        text="NVDA NVIDIA Corporation Stock",
        element_hash=102,
    ),
)


UPLOAD_STEP = StepRecord(
    step=3,
    url="https://www.roboform.com/filling-test-all-fields",
    action=ActionRecord(type="upload_file", index=9, params={"path": "/tmp/resume.pdf"}),
    element=ElementSignals(
        tag_name="input",
        attributes={"id": "resume", "name": "resume", "type": "file"},
        xpath="/html/body/form/input[9]",
        element_hash=103,
    ),
)


SELECT_DROPDOWN = StepRecord(
    step=4,
    url="https://www.roboform.com/filling-test-all-fields",
    action=ActionRecord(type="select_dropdown", index=12, params={"text": "California"}),
    element=ElementSignals(
        tag_name="select",
        attributes={"id": "state", "name": "state"},
        xpath="/html/body/form/select[1]",
        element_hash=104,
    ),
)


def test_upload_file_steps_are_not_emitted_but_siblings_still_are():
    """upload_file carries no file provenance a later replay could resolve, so a
    node for it would produce an automation that fails UploadFileAction's
    required-file-field validation. It is dropped; steps around it are not."""
    entry = build_entry(
        "k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT, UPLOAD_STEP, LINK_CLICK]
    )
    assert all(s.action_type != "upload_file" for s in entry.steps)
    assert [s.action_type for s in entry.steps] == ["input", "click"]


def test_entry_uses_the_highest_scoring_candidate_as_the_command():
    entry = build_entry("k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT])
    assert entry.steps[0].command == 'locator("#firstname")'


def test_entry_keeps_the_full_fallback_chain():
    entry = build_entry("k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT])
    scores = [c["score"] for c in entry.steps[0].candidates]
    assert len(scores) > 1
    assert scores == sorted(scores, reverse=True)


def test_command_never_contains_first():
    """.first rewrites the selector to >> nth=0 and exempts it from strict mode."""
    entry = build_entry("k1", "ep1", LINK_CLICK.url, [SEARCH_INPUT, LINK_CLICK])
    for s in entry.steps:
        assert ".first" not in s.command
        for c in s.candidates:
            assert ".first" not in c["locator"]


def test_input_text_is_carried_through():
    entry = build_entry("k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT])
    assert entry.steps[0].input_text == "myname"


def test_automation_validates_against_the_real_schema():
    from optexity.schema.automation import Automation

    entry = build_entry("k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT, LINK_CLICK])
    doc = to_automation(entry, SEARCH_INPUT.url, {})
    Automation.model_validate(doc)          # raises if the shape is wrong
    assert doc["url"] == SEARCH_INPUT.url
    assert len(doc["nodes"]) == 2


def test_select_dropdown_value_round_trips_through_to_automation():
    """build_entry must capture the chosen option, and to_automation must not
    drop it: SelectOptionAction.select_values defaults to None, and a None
    reaches smart_select's `for p in patterns:` with no guard -> TypeError at
    replay for any dropdown with more than two real options."""
    from optexity.schema.automation import Automation

    entry = build_entry("k1", "ep1", SELECT_DROPDOWN.url, [SELECT_DROPDOWN])
    assert entry.steps[0].input_text == "California"

    doc = to_automation(entry, SELECT_DROPDOWN.url, {})
    Automation.model_validate(doc)
    select_option = doc["nodes"][0]["interaction_action"]["select_option"]
    assert select_option["select_values"] == ["California"]


def _called_names(expr: str) -> set[str]:
    """Names/attributes that are actually invoked (the `func` of some ast.Call)
    anywhere in `expr` -- as opposed to merely appearing as text inside a
    string constant, which cannot execute."""
    tree = ast.parse(expr, mode="eval")
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return called


def test_adversarial_page_text_stays_inert():
    """Locator commands are eval()'d via `eval(f"page.{command}")`, and are
    built from page-controlled strings (ax_name/text). A hostile value must
    surface only as a string constant in the parsed expression -- never as a
    name or attribute that is actually called.

    Quote-counting cannot show this: a hand-built *unescaped* version of this
    same payload has the same (even) quote parity as the correctly escaped
    one, so `command.count('"') % 2 == 0` cannot tell safe from unsafe. The
    negative control below proves this test would have caught that failure.
    """
    hostile = StepRecord(
        step=1,
        url="https://example.com/",
        action=ActionRecord(type="click", index=1),
        element=ElementSignals(
            tag_name="a",
            attributes={},
            xpath="/html/body/a",
            ax_role="link",
            ax_name='") or __import__("os").system("touch /tmp/pwned") or ("',
            text='") or __import__("os").system("touch /tmp/pwned") or ("',
            element_hash=1,
        ),
    )
    entry = build_entry("k1", "ep1", hostile.url, [hostile])
    command = entry.steps[0].command
    expr = f"page.{command}"

    # Positive: the real, escaped command. It must parse, and the payload
    # markers must never be invoked.
    tree = ast.parse(expr, mode="eval")
    called = _called_names(expr)
    assert "__import__" not in called
    assert "system" not in called
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in ("__import__", "system")
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("__import__", "system")
    # And the only call actually made is the expected locator chain: exactly
    # one call, targeting get_by_role -- a payload that opened a second call
    # (e.g. by escaping the string) would add to this set.
    assert called == {"get_by_role"}

    # Negative control: a hand-built *unescaped* version of the identical
    # payload. Same even quote-parity as the safe command above (the check
    # Finding 2 rejected), but it genuinely calls __import__(...).system(...)
    # when eval()'d. If this test could not tell the two apart, it would be
    # useless -- this half proves it can.
    unescaped = (
        'get_by_role("link", name="") '
        'or __import__("os").system("touch /tmp/pwned") or ("")'
    )
    assert unescaped.count('"') % 2 == command.count('"') % 2 == 0
    unescaped_called = _called_names(f"page.{unescaped}")
    assert "__import__" in unescaped_called
    assert "system" in unescaped_called


NAVIGATE_STEP = StepRecord(
    step=5,
    url="https://www.saucedemo.com/",
    action=ActionRecord(
        type="navigate", index=None, params={"url": "https://www.saucedemo.com/cart.html"}
    ),
)

SEARCH_STEP = StepRecord(
    step=6,
    url="https://www.saucedemo.com/",
    action=ActionRecord(type="search", index=None, params={"query": "sauce labs"}),
)

GO_BACK_STEP = StepRecord(
    step=7, url="https://www.saucedemo.com/cart.html",
    action=ActionRecord(type="go_back", index=None, params={}),
)


def test_navigate_step_emits_a_go_to_url_node():
    """The pruner keeps navigation because it decides which page every later
    step runs against. The emitter used to drop it, so any workflow that
    navigated mid-run replayed its later steps against the wrong page."""
    from optexity.schema.automation import Automation

    entry = build_entry(
        "k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT, NAVIGATE_STEP, LINK_CLICK]
    )
    assert [s.action_type for s in entry.steps] == ["input", "navigate", "click"]
    assert entry.steps[1].url == "https://www.saucedemo.com/cart.html"

    doc = to_automation(entry, SEARCH_INPUT.url, {})
    Automation.model_validate(doc)  # raises if the node shape is wrong
    assert doc["nodes"][1]["interaction_action"]["go_to_url"] == {
        "url": "https://www.saucedemo.com/cart.html"
    }


def test_excluded_navigation_kinds_are_reported_not_vanished():
    """search/go_back/switch/close have no safe conversion, but a silent drop
    means a mis-replayed automation with nothing to point at."""
    entry = build_entry(
        "k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT, SEARCH_STEP, GO_BACK_STEP]
    )
    assert [s.action_type for s in entry.steps] == ["input"]
    reported = {d.action_type: d for d in entry.skipped}
    assert set(reported) == {"search", "go_back"}
    for d in reported.values():
        assert d.emitted is False
        assert d.reason  # a stated reason, not an empty string


def test_upload_file_exclusion_is_reported_too():
    entry = build_entry(
        "k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT, UPLOAD_STEP, LINK_CLICK]
    )
    assert [d.action_type for d in entry.skipped] == ["upload_file"]


def test_a_fully_emitted_trace_reports_nothing_skipped():
    entry = build_entry("k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT, LINK_CLICK])
    assert entry.skipped == []


UNLABELLED_A = StepRecord(
    step=1,
    url="https://www.roboform.com/filling-test-all-fields",
    action=ActionRecord(type="input", index=6, params={"text": "myname"}),
    element=ElementSignals(
        tag_name="input",
        attributes={},
        xpath="html/body/div[2]/form/div/div[1]/div[5]/div[2]/input",
        element_hash=201,
    ),
)

UNLABELLED_B = StepRecord(
    step=2,
    url="https://www.roboform.com/filling-test-all-fields",
    action=ActionRecord(type="input", index=7, params={"text": "xyz"}),
    element=ElementSignals(
        tag_name="input",
        attributes={},
        xpath="html/body/div[2]/form/div/div[1]/div[8]/div[2]/input",
        element_hash=202,
    ),
)


def test_same_tag_elements_get_distinguishable_prompt_instructions():
    """When the gate declines and skip_prompt is False, prompt_instructions is
    all the LLM index predictor gets. On roboform the old fallback produced
    'input the element previously identified as input' for all twelve fields."""
    entry = build_entry("k1", "ep1", UNLABELLED_A.url, [UNLABELLED_A, UNLABELLED_B])
    a, b = (s.prompt_instructions for s in entry.steps)
    assert a != b
    # And the distinguishing detail is present, not just incidental noise.
    assert UNLABELLED_A.element.xpath in a
    assert UNLABELLED_B.element.xpath in b
    assert "myname" in a and "xyz" in b


def test_prompt_instructions_still_use_a_real_label_when_there_is_one():
    entry = build_entry("k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT])
    assert "firstname" in entry.steps[0].prompt_instructions


def test_input_parameters_the_automation_never_reads_are_not_emitted():
    """Both committed artifacts carried {"stock_ticker": ["NVDA"]} on pages
    that never reference it: emitted values are literal, so nothing binds."""
    entry = build_entry("k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT, LINK_CLICK])
    doc = to_automation(entry, SEARCH_INPUT.url, {"stock_ticker": ["NVDA"]})
    assert doc["parameters"]["input_parameters"] == {}


def test_input_parameters_actually_referenced_are_kept():
    from optexity.replay_cache.store import CachedStep, CacheEntry

    entry = CacheEntry(
        key="k1", endpoint_name="ep1", origin="https://example.com",
        created_at="2026-09-12T00:00:00+00:00",
        steps=[
            CachedStep(
                action_type="input",
                command='locator("#ticker")',
                input_text="{stock_ticker[0]}",
                prompt_instructions="input the <input>",
            )
        ],
    )
    doc = to_automation(entry, "https://example.com", {"stock_ticker": ["NVDA"], "unused": ["x"]})
    assert doc["parameters"]["input_parameters"] == {"stock_ticker": ["NVDA"]}
