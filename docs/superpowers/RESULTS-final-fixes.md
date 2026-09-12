# Final whole-branch fix wave — report

**Date:** 2026-09-12
**Repos:** `optexity-fork` (branch `replay-cache`, from `63f5fcb`) and `browser-use`
(branch `replay-cache`, at `86de5d69` — **unchanged**, no fix in this wave touched it).
**Test command (optexity):**
`ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests -q`
**Test command (browser-use):**
`/Users/kamal/Desktop/optexity/.venv/bin/pytest tests/ci/test_replay_cache_recorder.py -q`

**Result:** optexity **70 passed** (was 49), browser-use **9 passed** (unchanged).
The ~45 `PydanticDeprecatedSince20` warnings are pre-existing.

---

## 1. CRITICAL — the gate broke the retry loop's waiting (fixed)

### What was wrong

`resolve_unique` was called ahead of the pre-existing
`await locator.wait_for(state="visible", …)`. `Locator.count()` does not auto-wait, so an
element not yet in the DOM produced `n == 0`, the gate declined, and `continue` skipped both
that `wait_for` **and** the `await asyncio.sleep(max_timeout_seconds_per_try)` that every
other failure branch performs. All `max_tries` burned with zero elapsed time — for every
Optexity automation, cached or not.

### What I changed

`optexity/inference/core/interaction/handle_command.py`. I took the reviewer's first option
(resolve the primary command, wait on it, gate afterwards) rather than putting a bounded wait
inside `_count`, because:

- it preserves the pre-gate semantics *exactly* — same locator, same `state="visible"`, same
  timeout, same `try_index == 0` condition, same swallow-and-continue on failure — rather than
  approximating them with a different wait state (`attached`);
- a bounded wait inside `_count` would be paid **once per candidate** whenever the chain is
  walked, turning an N-candidate miss into N × timeout.

The loop body now reads:

```python
primary_locator = await browser.get_locator_from_command(action.command)
if primary_locator is not None and try_index == 0:
    try:
        await primary_locator.wait_for(
            state="visible", timeout=max_timeout_seconds_per_try * 1000
        )
    except Exception:
        pass

outcome = await resolve_unique(browser, action.command, …)
outcome_box.append(outcome)
if not outcome.resolved:
    logger.warning(…)
    last_error = f"error: {outcome.reason}"
    await asyncio.sleep(max_timeout_seconds_per_try)   # <- the missing backoff
    continue
```

Note `primary_locator is not None` rather than an early `continue`: if the primary command
resolves to nothing, the gate must still run so it can recover from the candidate chain and
so the outcome is still counted. The second `continue` (gate command resolved to `None`) also
gained the same `asyncio.sleep`, and now sets a `last_error` instead of leaving `None`.

The `wait_for` is inside `except Exception: pass` because a strict-mode ambiguity, a timeout
and a detached node are all the gate's business to classify, not this wait's.

### Tests

`tests/replay_cache/test_gate_wiring.py`, three new tests built on a `LateLocator` that models
the two Playwright behaviours that matter and only those — `count()` does **not** auto-wait,
`wait_for()` does:

- `test_element_that_appears_late_is_waited_for_and_the_action_still_runs` — element appears at
  +0.3 s, `max_tries=3`, `max_timeout_seconds_per_try=1.0`. Asserts `wait_for` was called,
  elapsed ≥ 0.3 s, the click actually ran, `last_error is None`, and one recorded hit.
- `test_gate_decline_keeps_the_retry_backoff` — element never appears; asserts the loop still
  spends its `max_tries × backoff`.
- `test_gate_records_once_per_action_not_once_per_try` — see item 4.

### Evidence the tests can fail (they are not vacuous)

Controlled experiment: only `handle_command.py` reverted to `63f5fcb`, everything else left at
the fixed state.

```
$ cp optexity/inference/core/interaction/handle_command.py /tmp/hc_fixed.py
$ git checkout HEAD -- optexity/inference/core/interaction/handle_command.py
$ ENV_PATH=… pytest tests/replay_cache/test_gate_wiring.py -q
E       AssertionError: the pre-existing wait_for was skipped
E       assert 0 >= 1
E        +  where 0 = <LateLocator object>.wait_for_calls
E       AssertionError: retry loop kept no backoff (elapsed=0.00s)
E       assert 9.874999523162842e-05 >= 0.3
E       AssertionError: assert 3 == 1
E        +  where 3 = ReplayCounters(hits=0, chain_recoveries=0, escalations=3).escalations
FAILED tests/replay_cache/test_gate_wiring.py::test_element_that_appears_late_is_waited_for_and_the_action_still_runs
FAILED tests/replay_cache/test_gate_wiring.py::test_gate_decline_keeps_the_retry_backoff
FAILED tests/replay_cache/test_gate_wiring.py::test_gate_records_once_per_action_not_once_per_try
3 failed, 2 passed

$ cp /tmp/hc_fixed.py optexity/inference/core/interaction/handle_command.py
$ ENV_PATH=… pytest tests/replay_cache/test_gate_wiring.py -q
5 passed
```

`elapsed=0.00s` and `escalations=3` reproduce the reviewer's two measurements exactly.

---

## 2. IMPORTANT — the local-override guard (fixed)

`optexity/inference/child_process.py`. The override now requires **two independent
conditions**: `OPTEXITY_LOCAL_AUTOMATION_OVERRIDE` set to a truthy value
(`1`/`true`/`yes`/`on`, case- and whitespace-insensitive) **and** `test_automation.json`
existing. Either alone is inert. The INFO log when it fires is kept and now names the env var
that enabled it.

The guard is expressed once, in `local_automation_override_path()`, so it is testable rather
than inlined into the dispatch loop; the dispatch site is `override_path = …; if override_path
is not None:`.

Task 8 tooling updated in the same commit: `docs/superpowers/plans/2026-09-12-replay-cache.md`
Task 4 Step 5 and Task 8 Steps 5–6 now set `OPTEXITY_LOCAL_AUTOMATION_OVERRIDE=1` on the
inference server command and state that the file alone does nothing. The historical
`.superpowers/sdd/…/task-*-report.md` files are records of what was done at the time and were
left alone.

**Tests:** `tests/replay_cache/test_local_override_guard.py` (5 tests) — file alone does not
fire, env var alone does not fire, both fire, every falsy spelling (`""`, `0`, `false`, `no`,
`off`, `maybe`) does not fire, every truthy spelling does.

---

## 3. IMPORTANT — navigation silently discarded (fixed)

`optexity/replay_cache/emit.py`, `optexity/replay_cache/store.py`.

- **`navigate` now round-trips.** `ACTION_TO_INTERACTION` maps it to optexity's `go_to_url`.
  The target comes from `action.params["url"]` — browser-use's `NavigateAction`
  (`browser_use/tools/views.py:28`) is `url: str, new_tab: bool`, so `params` carries it —
  with the step's own `url` as a fallback for a params dict that lost the field. The step's
  `url` is only a fallback because it is the page the step *started on*, which is the target
  only for the first action of a run.
  `CachedStep` gained `url: str | None`, and `command` is now optional (a navigation step
  targets a URL, not a node). `to_automation` emits `{"go_to_url": {"url": …}}` — `GoToUrlAction`
  is a plain `BaseModel` with no `command`/`prompt_instructions`, so the node shape differs
  from the element-targeting ones. The test validates it against the real `Automation` schema.

- **The other drops are now visible.** New `EmitDecision` model (`store.py`) and
  `CacheEntry.skipped: list[EmitDecision]`, mirroring `prune()`'s decisions. `build_entry`
  appends a decision **and logs a warning** for every pruned step it cannot express:
  `search`, `go_back`, `switch`, `close`, `upload_file`, a `navigate` with no target URL, a
  step with no element signals, and a step whose scorer produced no candidates. Because the
  decisions live on the entry, a dropped step is visible in the persisted artifact, not only
  in a log line nobody kept.

- **The exclusions are documented**, in `EXCLUSION_REASONS`, in the same style as the
  `upload_file` comment.

### One place I disagree with the review, stated rather than silently skipped

The brief says to document the exclusion of `go_back`, `switch` and `close`. I did that, but
they are not all equally unmappable and the report should say so:

- `switch` / `close` genuinely do not convert: browser-use addresses tabs by a 4-character
  `tab_id` assigned at run time (`SwitchTabAction`/`CloseTabAction`, `tools/views.py:63-69`),
  optexity's `SwitchTabAction` takes a positional `tab_index`. The trace carries no tab table
  to convert between them. Excluding them is correct, not a stopgap.
- `go_back` **does** have a direct optexity equivalent (`GoBackAction`, a no-field `BaseModel`).
  I still excluded it, for a reason I have written into `EXCLUSION_REASONS`: history depth at
  replay depends on every preceding *emitted* step, and this emitter drops steps, so a
  `go_back` replayed against a shorter history lands on the wrong page — silently, which is
  the same failure class as the one this item is fixing. Mapping it safely means first knowing
  that nothing before it was dropped; that check is cheap but it is new behaviour and neither
  captured trace exercises `go_back`, so I did not add it blind. It is a clean follow-up.

**Tests** (`tests/replay_cache/test_emit.py`):
`test_navigate_step_emits_a_go_to_url_node` (asserts the node exists, carries the right URL,
sits in the right position, and validates against the real schema),
`test_excluded_navigation_kinds_are_reported_not_vanished`,
`test_upload_file_exclusion_is_reported_too`,
`test_a_fully_emitted_trace_reports_nothing_skipped` (so "skipped is empty" is a real signal,
not the only state ever reached).

---

## 4. IMPORTANT — counters double-counted and mislabelled (fixed)

### Record once per action

`command_based_action_with_retry` is now a thin wrapper around `_command_retry_loop`. The loop
appends each gate outcome to an `outcome_box`; the wrapper records the **last** one exactly
once, in a `finally`, so it is recorded on the success return, on the exhausted-retries
return, and on the `ExpectedDownloadFailedException` / unexpected-exception paths too. A
single click exhausting three tries is one escalation.

### Summary wording — what I chose and why

I **renamed the wording** rather than scoping the summary to cache-sourced actions.

Scoping is not implementable in this branch: there is no runtime cache-lookup path (explicitly
out of scope), and `action.locator_candidates` is not on the schema (explicitly out of scope),
so nothing on an action distinguishes "came from a cache entry" from "was hand-written". Any
"scoping" I could write today would be a constant `False` — a worse lie than the wording.

New summary:

```
replay gate: 2 resolved on primary, 1 recovered via candidate chain, 1 escalated to LLM (4 command steps gated)
```

The docstring records that if a runtime lookup path is added later, the counters should be
scoped to cache-sourced actions and the wording can go back to talking about the cache.

**Tests:** `test_gate_records_once_per_action_not_once_per_try` (item 1's evidence block shows
it printing `escalations=3` against the old code);
`tests/replay_cache/test_counters.py::test_summary_does_not_claim_cached_steps` plus the
updated exact-string test; `tests/replay_cache/test_gate.py` wording assertion updated.

---

## 5. IMPORTANT — `prompt_instructions` could not discriminate (fixed)

`optexity/replay_cache/emit.py::prompt_instructions_for`. The instruction now carries, in
order: the action, the tag, a label (`ax_name` → `id` → **`name`** → **`placeholder`**, the
last two newly consulted), the sibling ordinal parsed off the xpath's trailing `[n]`, the
xpath, and the value being entered.

Real roboform output, regenerated from the captured trace:

| before | after |
|---|---|
| `input the element previously identified as input` (×12, identical) | `input the <input> labelled '04fullname', at xpath html/body/div[2]/form/div/div[1]/div[5]/div[2]/input, receiving 'myname'` |

Adding `name` to the label chain alone already fixes roboform (the fields have `name`
attributes but no `id`), and the xpath makes it unambiguous regardless.

**Tests:** `test_same_tag_elements_get_distinguishable_prompt_instructions` — two unlabelled
`<input>`s on one page; asserts the strings differ **and** that each carries its own xpath and
its own value, so the test cannot pass on incidental noise.
`test_prompt_instructions_still_use_a_real_label_when_there_is_one`.

---

## 6. IMPORTANT — the recorder had no off-switch (fixed)

`optexity/inference/core/interaction/handle_agentic_task.py`. New
`OPTEXITY_REPLAY_CACHE_RECORD`, read via `recording_enabled()`, **defaulting to on**: only an
explicit falsy value (`0`/`false`/`no`/`off`) disables it. When disabled,
`OPTEXITY_REPLAY_CACHE_TRACE` is *popped* rather than left alone — the browser-use recorder is
inert unless that variable names a path, so clearing it is the entire off-switch, and popping
also defends against a stale value inherited from the environment. `recording_enabled()` is
itself wrapped in `try/except`, matching the recorder's posture: failing to read configuration
must never break an agentic task.

**Tests** (`tests/replay_cache/test_agentic_metrics_wiring.py`): `FakeAgent.run` now captures
what the recorder would see. `test_recording_is_on_by_default`,
`test_recording_can_be_switched_off_without_a_deploy` (asserts the recorder sees `None`, not
merely a different path — and pre-sets a stale value to prove the pop), and
`test_only_explicit_falsy_values_disable_recording`.

---

## 7. Minor items (all done)

- **`store.py` `put` key guard.** `get()` looks entries up by filename and never re-checks
  `entry.key`, so a mismatched `put` writes an entry returned for a key it was not built for.
  Now raises `ValueError` and writes nothing.
  Test: `test_put_refuses_an_entry_whose_key_does_not_match` (also asserts no file was left
  behind).
- **`prune.py` scroll comment.** Records that the scroll branch is placed *before* the
  `if not s.success` branch deliberately, so a scroll's success flag is never consulted —
  browser-use's multi-page scroll swallows per-page failures and returns success anyway
  (`tools/service.py:738-770`), so the flag cannot tell a full scroll from a partial one.
- **`to_automation` input parameters.** New `_parameters_actually_used`: emitted values are
  literal (spec §3), and optexity placeholders are `{name[index]}`
  (`optexity/schema/automation.py:109`), so a parameter survives only if `{name[` appears in
  the rendered nodes. `input_parameters` is now optional and defaults to `None`.
  Tests: `test_input_parameters_the_automation_never_reads_are_not_emitted` and
  `test_input_parameters_actually_referenced_are_kept` (so the filter is not just "always {}").
- **Evidence artifacts moved** to `docs/superpowers/test_automation_cached_roboform.json` and
  `…_saucedemo.json`, and **regenerated** from the original captured traces with the fixed
  emitter, so they show the real current output rather than stale text:
  - roboform from `/private/tmp/optexity/4a1bb50b-…/logs/step_0/replay_cache_trace.jsonl`
  - saucedemo from `/private/tmp/optexity/28cc08b6-…/logs/step_0/replay_cache_trace.jsonl`

  Verification that the regeneration is faithful and only the intended fields changed:

  ```
  roboform  nodes 12 (was 12)  commands match: True   params: {}  skipped: []
  saucedemo nodes 11 (was 11)  commands match: True   params: {}  skipped: []
  ```

  Every locator command is byte-identical to the committed artifact; what changed is
  `input_parameters` (`{"stock_ticker": ["NVDA"]}` → `{}`) and `prompt_instructions`. Neither
  trace contains a `navigate` step, which is why neither exercised item 3 — consistent with the
  review's finding.

---

## Out of scope — confirmed not done

`LocatorExtraction._looks_dynamic` untouched; no runtime cache-lookup path; the pruner's
rotating-duplicate limitation (spec §6 rule 4) left as specified; `_css_attr`'s single-quote
escaping untouched; `action.locator_candidates` not threaded onto the schema (the gate still
reads it via `getattr(..., None) or []`, so it is `[]` in production and the chain has exactly
one entry — unchanged from before this wave).

## Full test output

```
$ cd /Users/kamal/Desktop/optexity/optexity-fork
$ ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests -q
70 passed, 54 warnings in 2.59s

$ cd /Users/kamal/Desktop/optexity/browser-use
$ /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/ci/test_replay_cache_recorder.py -q
9 passed in 0.01s
```

21 new tests, 0 regressions. browser-use is untouched by this wave.
