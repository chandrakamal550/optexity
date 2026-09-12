import json

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


def test_adversarial_page_text_stays_inert():
    """Locator commands are eval()'d, and are built from page-controlled strings."""
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
    blob = json.dumps(entry.model_dump())
    assert "__import__" not in blob or '\\"' in blob
    for s in entry.steps:
        # No unescaped quote may close the locator string early.
        assert s.command.count('"') % 2 == 0
