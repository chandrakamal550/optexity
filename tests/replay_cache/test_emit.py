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
