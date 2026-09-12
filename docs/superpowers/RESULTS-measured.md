# Task 8 report: metrics and end-to-end verification

Branch `replay-cache`, starting HEAD `182556d`. Both controller amendments
applied in full: metrics are wired into production, not just defined; the
second site (saucedemo.com) was chosen specifically because its elements
carry `id` and `data-test` attributes, so the emitted locators exercise
higher-scoring tiers than roboform's xpath-only baseline.

**This report includes a "Fix round 1" section** (after "Before/after
comparison") addressing two Important review findings: the roboform
before/after originally paired mismatched runs (corrected — see that
section and the updated Site 1 table above it), and the metrics-capture
code could have suppressed browser/agent cleanup on any exception (fixed,
with a new regression test). Sections above "Fix round 1" are left as
originally written except where a note flags a correction, so the document
shows both what was first submitted and what changed.

## What was implemented

### 1. `optexity/replay_cache/metrics.py` (new, leaf module)

`RunMetrics` (pydantic model: `wall_clock_s`, `llm_calls`, `prompt_tokens`,
`completion_tokens`, `steps`, plus a `total_tokens` property), `from_history()`
and `compare()`, exactly as specified in the brief. Imports nothing but
`pydantic`, mirroring `optexity/replay_cache/counters.py`'s import-light
discipline — the same real cycle Task 7 hit
(`schema/memory.py` → `gate.py` → `emit.py` →
`inference.core.interaction.utils` → `schema.memory`) would reappear if this
module pulled in anything from `optexity.replay_cache.gate` or `.emit`.

### 2. Wiring into `Memory` (amendment 1)

`optexity/schema/memory.py`: added `from optexity.replay_cache.metrics import
RunMetrics` and a new field, following the `replay_cache: ReplayCounters`
precedent:

```python
replay_cache: ReplayCounters = Field(default_factory=ReplayCounters)
# One entry per handle_agentic_task() call (one per agentic step). Empty
# whenever the automation never fell back to the LLM at all.
agentic_run_metrics: list[RunMetrics] = Field(default_factory=list)
```

Used a `list` rather than a single object (unlike `ReplayCounters`, which
*aggregates* across many gate outcomes) because each `handle_agentic_task()`
call produces one independent `RunMetrics` for one agentic step; an
automation can contain more than one agentic node.

### 3. Wiring into `handle_agentic_task.py` (amendment 1)

`optexity/inference/core/interaction/handle_agentic_task.py`: wrapped
`time.monotonic()` around the `await agent.run(...)` call, built a
`RunMetrics` via `from_history()` immediately after, appended it to
`memory.agentic_run_metrics`, and logged it at INFO:

```python
run_started_at = time.monotonic()
history = await agent.run(max_steps=agentic_task_action.max_steps)
wall_clock_s = time.monotonic() - run_started_at
...
run_metrics = from_history(history, wall_clock_s)
memory.agentic_run_metrics.append(run_metrics)
logger.info(
    f"Agentic run metrics: wall_clock={run_metrics.wall_clock_s:.2f}s "
    f"llm_calls={run_metrics.llm_calls} "
    f"prompt_tokens={run_metrics.prompt_tokens} "
    f"completion_tokens={run_metrics.completion_tokens} "
    f"steps={run_metrics.steps}"
)
```

If `agent.run()` raises, this code never executes and nothing is appended —
consistent with `from_history`'s own defensiveness (it tolerates
`history.usage is None`, but there is no history at all to read from if the
call itself raised).

**Import-cycle verification** (fresh interpreter, both required modules):

```
$ ENV_PATH=.../.env .venv/bin/python -c "
import optexity.schema.memory
print('memory ok')
import optexity.inference.core.run_automation
print('run_automation ok')
"
memory ok
run_automation ok
```

No cycle. `run_automation.py` already logs `memory.replay_cache.summary()`
at the end of a task when `total > 0` (Task 7's wiring) — `agentic_run_metrics`
is visible per-run in the task log via the INFO line above rather than an
end-of-run summary, since (unlike counters) there's no single scalar to roll
many `RunMetrics` into that's more informative than the per-run lines already
emitted at the moment each agentic step actually ran.

## TDD evidence

1. Wrote `tests/replay_cache/test_metrics.py` (the 5 tests specified in the
   brief) before creating `metrics.py`.
2. Ran it and confirmed failure:
   ```
   ModuleNotFoundError: No module named 'optexity.replay_cache.metrics'
   ```
3. Implemented `optexity/replay_cache/metrics.py`.
4. Ran the full suite:
   ```
   $ ENV_PATH=... .venv/bin/pytest tests/replay_cache -v
   ... 47 passed, 45 warnings in 1.61s
   ```
   (47, not the brief's stated 39 — this fork's `tests/replay_cache/` already
   held 42 tests before this task from Tasks 1–7; +5 new metrics tests = 47.
   The brief's "39 across six files" undercounts because it was written
   before `test_gate_wiring.py` existed. Not a discrepancy in this task's
   work.)
5. Full repo suite: `pytest tests/ -q` → same 47 passed (this fork's `tests/`
   tree is entirely `tests/replay_cache/`).

45 `PydanticDeprecatedSince20` warnings present throughout, as expected —
not findings.

## Site selection (amendment 2)

- **Site 1 (baseline, unchanged from Task 4):** roboform.com test form.
  Single-page fill; already known from Task 4 to produce xpath-only,
  score-10, single-candidate locators (its `name` attributes fail
  `_looks_dynamic`, `ax_name` is empty, no `id`). Kept as-is specifically to
  show that known shape again for contrast.
- **Site 2 (new, per amendment 2):** **saucedemo.com** (Swag Labs demo
  store). Genuine multi-page workflow — login page → inventory page → cart
  page → checkout-information page → checkout-overview page →
  checkout-complete page — and every interactive element carries both an
  `id` and a `data-test` attribute (e.g. `id="user-name"
  data-test="username"`). No captcha. This satisfies both the brief's
  original multi-page requirement and the amendment's locator-tier
  requirement.

Task text used: *"Log in with username 'standard_user' and password
'secret_sauce'. On the products page, add the 'Sauce Labs Backpack' to the
cart. Open the cart by clicking the cart icon, then click Checkout. On the
checkout information page, enter First Name 'John', Last Name 'Doe', and
Zip/Postal Code '12345', then click Continue. On the overview page, click
Finish to complete the order."*

## The four live runs

All runs used endpoint `extract_price_stockanalysis-a16d42fc` (the base
automation is always overridden by `/Users/kamal/Desktop/optexity/test_automation.json`,
per `optexity/inference/child_process.py`'s local-override check, which
re-reads the file per task — no server restart needed between runs, only a
swap of the file's contents before firing the next request). Server started
once with `ENV_PATH=/Users/kamal/Desktop/optexity/.env
/Users/kamal/Desktop/optexity/.venv/bin/python -m optexity.inference.child_process
--port 9000 --child_process_id 0`, run from `/Users/kamal/Desktop/optexity`,
backgrounded to `server.log`, and kept up across all four runs.

### Run 1 — Site 1 agentic (roboform)

`test_automation.json` in place: the original Task-4 roboform agentic
automation (task: *"fill the full name as myname, address line one as xyz
and line 2 as abc, city as SF"*, `max_steps: 15`).

```
$ curl -s -X POST http://localhost:9000/inference -H "Content-Type: application/json" \
    -d '{"endpoint_name":"extract_price_stockanalysis-a16d42fc","input_parameters":{"stock_ticker":["NVDA"]},"unique_parameter_names":[]}'
{"success":true,...,"task_id":"4a1bb50b-627c-44d9-824c-a0a3db05bee0"}
```

Server log:
```
16:15:57,938 [INFO] ...run_automation: Task 4a1bb50b-... started running
16:17:13,656 [INFO] ...handle_agentic_task: Agentic run metrics: wall_clock=68.86s llm_calls=15 prompt_tokens=154844 completion_tokens=2565 steps=15
16:17:33,350 [INFO] ...run_automation: Task 4a1bb50b-... completed with status success
```
Status: success. `max_steps` (15) was fully consumed by the agent — the
metrics line's `llm_calls=15` reflects that exactly (each `AgentHistoryList`
history item is one LLM step).

One pre-existing, unrelated error appeared, same as Task 4's report: a
`Judge trace failed: litellm.BadRequestError ... image/png ... image/jpeg`
from browser-use's post-hoc trace judge — cosmetic, does not affect the run,
not part of this task's surface.

### Cache build — Site 1

**[CORRECTED in fix round 1 — see "Fix round 1" section below. The text in
this subsection describes the original, wrong build; kept for the historical
record of what was run and why it was flagged, not as current guidance.]**

Per the controller's original instruction, this cache entry was first built
from the **existing Task 4 trace**
(`/private/tmp/optexity/c2ccc4da-5f58-4d2b-b82a-1ccf157ba658/logs/step_0/replay_cache_trace.jsonl`,
still present, 31 rows) rather than from Run 1's own trace above:

```python
trace = '.../c2ccc4da.../replay_cache_trace.jsonl'
steps = [StepRecord.model_validate_json(l) for l in open(trace) if l.strip()]
key = cache_key('roboform-test', url, task, {'stock_ticker': ['NVDA']})
entry = build_entry(key, 'roboform-test', url, steps)
FileCacheStore(Path('.replay_cache')).put(key, entry)
doc = to_automation(entry, url, {'stock_ticker': ['NVDA']})
```
Result: `cached steps: 20` — matches the known 31-rows/20-inputs shape
**of the Task-4 trace, not of Run 1**. This is the mismatch review flagged:
Run 1 produced its own trace (24 rows / 12 inputs) that was never used.

**Locator tier per cached step (Site 1):** all steps → `xpath`, score 10,
exactly 1 candidate each, in both the original (wrong-trace) build and the
corrected one below. This is the same defect-free-but-unhappy outcome the
controller flagged in advance: roboform's `name` attributes (`04fullname`,
`10address1`, ...) fail `_looks_dynamic` (letters + ≥2 digits), `ax_name` is
empty (labels in unassociated table cells), no `id`. Confirmed, not a bug —
this is precisely why a second site was required. The tier finding is
unaffected by which trace was used to build the entry.

### Run 2 — Site 1 cached (roboform), original (superseded)

`test_automation.json` swapped to the (wrong-trace) 20-node
`test_automation_cached_roboform.json` described above.

```
$ curl ... 
{"success":true,...,"task_id":"33ddd549-3079-4eb4-b4d6-27a95a10a850"}
```
```
16:18:38,736 started running
16:19:25,483 completed with status success
16:19:25,483 [INFO] ...run_automation: replay cache: 20 hit, 0 chain recovery, 0 escalated to LLM (20 cached steps)
```
No `Agentic run metrics` line anywhere in this run's log — zero LLM calls.
No `ERROR`/`Traceback` lines. All 20 `InputTextAction`s succeeded on try 1.
Visually confirmed via the final step screenshot
(`.../33ddd549.../logs/step_20/screenshot.png`): Full Name = "myname",
Address Line 1 = "xyz", Address Line 2 = "abc", City = "SF" — the form was
genuinely filled correctly, but by locators built from a trace Run 1 never
produced. **This run and its numbers are superseded by the corrected Run 2
in the "Fix round 1" section below** and are no longer part of the reported
before/after comparison.

### Run 3 — Site 2 agentic (saucedemo)

`test_automation.json` swapped to the saucedemo agentic automation described
above (`max_steps: 20`).

```
$ curl ...
{"success":true,...,"task_id":"28cc08b6-9689-49e7-bc2c-fd44270639c2"}
```
```
16:20:17,961 started running
16:20:59,197 [INFO] ...handle_agentic_task: Agentic run metrics: wall_clock=38.28s llm_calls=7 prompt_tokens=60393 completion_tokens=1622 steps=7
16:21:11,204 completed with status success
```
Status: success, well under `max_steps`. Trace written to
`/private/tmp/optexity/28cc08b6-9689-49e7-bc2c-fd44270639c2/logs/step_0/replay_cache_trace.jsonl`
(12 rows: 2 inputs for login, 1 login click, 1 add-to-cart click, 1
cart-open click, 1 checkout click, 3 inputs for name/zip, 1 continue click,
1 finish click, 1 terminal `done`). Every recorded element carries a
`data-test` attribute and an `id`.

### Cache build — Site 2

```python
trace = '.../28cc08b6.../replay_cache_trace.jsonl'
key = cache_key('saucedemo-test', url, task, {'stock_ticker': ['NVDA']})
entry = build_entry(key, 'saucedemo-test', url, steps)
doc = to_automation(entry, url, {'stock_ticker': ['NVDA']})
```
Result: `cached steps: 11` (the terminal `done` record is correctly dropped —
its action type isn't in `ACTION_TO_INTERACTION`). Written to
`test_automation_cached_saucedemo.json` (committed).

**Locator tier per cached step (Site 2)** — every single step:

| # | action | command | kind | score |
|---|--------|---------|------|-------|
| 0 | input | `input[data-test='username']` | test-id | 98 |
| 1 | input | `input[data-test='password']` | test-id | 98 |
| 2 | click | `input[data-test='login-button']` | test-id | 98 |
| 3 | click | `button[data-test='add-to-cart-sauce-labs-backpack']` | test-id | 98 |
| 4 | click | `a[data-test='shopping-cart-link']` | test-id | 98 |
| 5 | click | `button[data-test='checkout']` | test-id | 98 |
| 6 | input | `input[data-test='firstName']` | test-id | 98 |
| 7 | input | `input[data-test='lastName']` | test-id | 98 |
| 8 | input | `input[data-test='postalCode']` | test-id | 98 |
| 9 | click | `input[data-test='continue']` | test-id | 98 |
| 10 | click | `button[data-test='finish']` | test-id | 98 |

All 11 steps carry 6–9 candidates each (test-id, id, name, aria-label,
role+name, etc. all present) — a real, exercised candidate chain, unlike
roboform's single-candidate steps. This is the direct payoff of amendment 2.

### Run 4 — Site 2 cached (saucedemo)

`test_automation.json` swapped to `test_automation_cached_saucedemo.json`'s
content.

```
$ curl ...
{"success":true,...,"task_id":"8ee1b0a2-a629-4c42-87bc-53b4f3f5d5da"}
```
```
16:21:46,015 started running
16:22:16,397 [INFO] ...run_automation: replay cache: 11 hit, 0 chain recovery, 0 escalated to LLM (11 cached steps)
16:22:16,397 completed with status success
```
No `Agentic run metrics` line — zero LLM calls. No `ERROR`/`Traceback`. All
11 actions (`InputTextAction`/`ClickElementAction`) succeeded on try 1.
Visually confirmed via the final screenshot
(`.../8ee1b0a2.../logs/step_11/screenshot.png`): page shows "Checkout:
Complete!" / "Thank you for your order!" — the full multi-page workflow
replayed end to end from cached, real `data-test` locators.

## Before/after comparison

Wall clock is reported two ways per the raw log timestamps: **total** (task
`started running` → `completed with status`, i.e. what a caller actually
waits for, including trajectory/screenshot S3 uploads that both paths incur
equally) and **core** (task start → last meaningful action/agentic
completion, excluding that shared upload tail) — so the reader can see the
replay-specific speedup isolated from infra overhead common to both paths.

### Site 1 — roboform.com (single page, xpath-only locators)

**Corrected in fix round 1.** See "Fix round 1" below for the full
before/after of what was wrong and the re-run. Numbers here are the
corrected, same-run pairing (Run 1 agentic vs. the corrected Run 2 cached,
both built from and reflecting task_id `4a1bb50b-...`'s own trace):

| metric | agentic (Run 1) | cached (Run 2, corrected) |
|---|---|---|
| wall clock (total) | 95.41s | 39.43s |
| wall clock (core) | 75.72s | 20.77s |
| LLM calls | 15 | 0 |
| prompt tokens | 154,844 | 0 |
| completion tokens | 2,565 | 0 |
| total tokens | 157,409 | 0 |
| steps | 15 | 12 |

`compare()` output:
```
replay cache comparison
  wall clock : 95.41s -> 39.43s (2.4x faster)
  llm calls  : 15 -> 0
  tokens     : 157409 -> 0 (saved 157409)
  steps      : 15 -> 12
```
(core-only speedup: 75.72s / 20.77s = 3.65x)

### Site 2 — saucedemo.com (multi-page, test-id locators)

| metric | agentic (Run 3) | cached (Run 4) |
|---|---|---|
| wall clock (total) | 53.24s | 30.38s |
| wall clock (core) | 41.13s | 13.13s |
| LLM calls | 7 | 0 |
| prompt tokens | 60,393 | 0 |
| completion tokens | 1,622 | 0 |
| total tokens | 62,015 | 0 |
| steps | 7 | 11 |

`compare()` output:
```
replay cache comparison
  wall clock : 53.24s -> 30.38s (1.8x faster)
  llm calls  : 7 -> 0
  tokens     : 62015 -> 0 (saved 62015)
  steps      : 7 -> 11
```
(core-only speedup: 41.13s / 13.13s = 3.13x)

**Honest read:** total-wall-clock speedup (1.8x–2.4x) is real but modest
because a large, fixed slice of every run — browser CDP startup plus
end-of-task trajectory/screenshot uploads to S3 — is identical whether the
step logic is agentic or cached, and neither run in this environment is long
enough to make that fixed cost negligible. The core-execution numbers
(3.13x–3.65x) isolate what the replay cache itself controls, and both show
it eliminating essentially all step-execution latency and all token cost.
The **llm_calls: N → 0** and **tokens: N → 0** numbers are unconditional wins
regardless of which wall-clock framing is used.

## Fix round 1 (review: "Needs fixes", two Important findings)

### Finding 1 — roboform before/after paired two different executions

**What was wrong:** `test_automation_cached_roboform.json` had been built
from the pre-existing Task-4 trace (`c2ccc4da…`, 31 rows / 20 inputs)
instead of from this task's own Run 1 (`4a1bb50b…`, 24 rows / 12 inputs).
The reported comparison therefore set Run 1's real agentic cost (95.41s,
15 LLM calls, 157,409 tokens) against a cached automation Run 1 never
produced — visible as the nonsensical `steps: 15 -> 20` in the original
table. The controller's own instruction was self-contradictory (reuse the
existing trace *and* pair it with a fresh agentic run's numbers); I followed
it exactly as written, which was the bug. **The earlier roboform figure
paired mismatched runs and has been corrected below; saucedemo was
unaffected and untouched.**

**Fix:** rebuilt the roboform cache entry and `test_automation_cached_roboform.json`
from Run 1's own trace, and re-ran the cached replay once.

Rebuild command:
```
$ ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/python -c "
import json
from optexity.replay_cache.emit import build_entry, to_automation
from optexity.replay_cache.key import cache_key
from optexity.replay_cache.records import StepRecord
from optexity.replay_cache.store import FileCacheStore
from pathlib import Path

trace = '/private/tmp/optexity/4a1bb50b-627c-44d9-824c-a0a3db05bee0/logs/step_0/replay_cache_trace.jsonl'
steps = [StepRecord.model_validate_json(l) for l in open(trace) if l.strip()]
url = 'https://www.roboform.com/filling-test-all-fields'
task = 'fill the full name as myname, address line one as xyz and line 2 as abc, city as SF'
key = cache_key('roboform-test', url, task, {'stock_ticker': ['NVDA']})
entry = build_entry(key, 'roboform-test', url, steps)
FileCacheStore(Path('.replay_cache')).put(key, entry)
doc = to_automation(entry, url, {'stock_ticker': ['NVDA']})
Path('optexity-fork/test_automation_cached_roboform.json').write_text(json.dumps(doc, indent=2))
print('cached steps:', len(entry.steps))
"
cached steps: 12
```
The Run-1 trace's 24 rows (12 `input`, 11 `screenshot`, 1 `done`) prune down
to 12 cached `input_text` nodes — a genuinely different, smaller automation
than the Task-4-trace build's 20, and the **correct** result: Run 1's agent
filled each of the 4 fields exactly 3 times (a retry pattern, not 5x as in
the Task-4 trace), all still non-consecutive so `prune()`'s
consecutive-duplicate rule doesn't collapse them. All 12 steps land on
`xpath`, score 10, 1 candidate each — same tier finding as before, unaffected
by which trace was used.

Re-run (new server start, same endpoint, `test_automation.json` swapped to
the corrected 12-node automation):
```
$ curl -s -w "\nHTTP_STATUS:%{http_code}\n" -X POST http://localhost:9000/inference \
    -H "Content-Type: application/json" \
    -d '{"endpoint_name":"extract_price_stockanalysis-a16d42fc","input_parameters":{"stock_ticker":["NVDA"]},"unique_parameter_names":[]}'
{"success":true,...,"task_id":"59c47069-195e-483e-8e84-5f09e65b38e2"}
HTTP_STATUS:202
```
Server log:
```
16:34:12,060 [INFO] ...run_automation: Task 59c47069-... started running
16:34:51,490 [INFO] ...run_automation: replay cache: 12 hit, 0 chain recovery, 0 escalated to LLM (12 cached steps)
16:34:51,490 [INFO] ...run_automation: Task 59c47069-... completed with status success
```
No `Agentic run metrics` line, no `ERROR`/`Traceback`. All 12
`InputTextAction`s succeeded on try 1 (`grep -c "Agentic run metrics"` → 0;
`grep -n "ERROR|Traceback"` → no matches). Last action completed at
16:34:32,834 (core wall clock = 20.77s from task start; total = 39.43s).
Visually confirmed via the final screenshot
(`/private/tmp/optexity/59c47069-195e-483e-8e84-5f09e65b38e2/logs/step_12/screenshot.png`):
Full Name = "myname", Address Line 1 = "xyz", Address Line 2 = "abc",
City = "SF" — correct, and this time built from and verified against the
same run's own trace throughout.

**Corrected numbers** (Run 1 agentic vs. corrected Run 2 cached, same
underlying execution paired both ways):

| metric | agentic (Run 1, `4a1bb50b`) | cached (corrected Run 2, `59c47069`) |
|---|---|---|
| wall clock (total) | 95.41s | 39.43s |
| wall clock (core) | 75.72s | 20.77s |
| LLM calls | 15 | 0 |
| prompt tokens | 154,844 | 0 |
| completion tokens | 2,565 | 0 |
| total tokens | 157,409 | 0 |
| steps | 15 | 12 |

`compare()`:
```
replay cache comparison
  wall clock : 95.41s -> 39.43s (2.4x faster)
  llm calls  : 15 -> 0
  tokens     : 157409 -> 0 (saved 157409)
  steps      : 15 -> 12
```
(core-only speedup: 75.72s / 20.77s = 3.65x)

`steps: 15 -> 12` is now a coherent number: 15 LLM decision-steps in the
agentic run collapse to 12 replayable command-steps after pruning — no
mismatch symptom remains. This one additional live run (cached only, zero
LLM calls) was the full cost of the fix, as anticipated.

### Finding 2 — metrics code sat ahead of browser cleanup in a shared path

**What was wrong:** in `handle_agentic_task.py`, `from_history(...)`, the
`memory.agentic_run_metrics.append(...)`, and the `logger.info(...)` sat
between `await agent.run(...)` and `agent.stop()` /
`await agent.browser_session.stop()` / `.reset()`, inside the same `try`
block with nothing guarding them. Any exception there (a pydantic validation
error building `RunMetrics`, a formatting error in the f-string) would have
skipped browser/agent cleanup for **every** agentic task in the codebase,
not only replay-cache ones — the same principle the trace-count log a few
lines below already follows correctly.

**Fix:** wrapped the metrics block in its own `try/except Exception`,
swallowing and debug-logging, matching the existing trace-count-log pattern
in the same function:

```python
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
```

### Covering test for finding 2

New file `tests/replay_cache/test_agentic_metrics_wiring.py`, in the style
of `test_gate_wiring.py`: fakes `Agent`/`BrowserSession`/`build_agent_llm`/
`normalize_model` at the `handle_agentic_task` module level and drives the
**real** `handle_agentic_task()` coroutine end to end (not just `from_history()`
in isolation, which `test_metrics.py` already covers).

- `test_successful_run_records_metrics_and_still_cleans_up`: a normal run
  appends exactly one `RunMetrics` to `memory.agentic_run_metrics` with the
  fake history's token/step counts, and cleanup (`agent.stop()`,
  `browser_session.stop()`, `.reset()`) all ran.
- `test_raising_metrics_capture_does_not_prevent_cleanup`: monkeypatches
  `hat.from_history` to raise `ValueError`, then asserts
  `memory.agentic_run_metrics == []` (nothing recorded, as expected) **and**
  that cleanup still ran — the actual defect under test.

**Proof the test is a real regression guard, not a tautology:** temporarily
reverted the try/except guard (restoring the pre-fix code) and re-ran:
```
$ ENV_PATH=... .venv/bin/pytest tests/replay_cache/test_agentic_metrics_wiring.py -v
test_successful_run_records_metrics_and_still_cleans_up PASSED
test_raising_metrics_capture_does_not_prevent_cleanup FAILED
    raise ValueError("boom")
E   ValueError: boom
```
Confirmed the second test genuinely fails without the fix (the injected
exception propagates uncaught, exactly the pre-fix behavior that would have
skipped `agent.stop()`/`browser_session.stop()`/`.reset()`), then restored
the guarded code and re-ran to confirm both pass:
```
$ ENV_PATH=... .venv/bin/pytest tests/replay_cache/test_agentic_metrics_wiring.py -v
test_successful_run_records_metrics_and_still_cleans_up PASSED
test_raising_metrics_capture_does_not_prevent_cleanup PASSED
```

### Full suite after fix round 1

```
$ ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/ -q
49 passed, 45 warnings in 1.62s
```
(47 from the initial Task 8 submission + 2 new in
`test_agentic_metrics_wiring.py`.)

### Environment restored after fix round 1

Server stopped (`pkill -f optexity.inference.child_process`; confirmed port
9000 free). `/Users/kamal/Desktop/optexity/test_automation.json` restored to
its pre-task-8 state (the original Task 4 roboform agentic automation),
same as after the initial submission.

## Files changed

- `optexity/replay_cache/metrics.py` — new, `RunMetrics`/`from_history`/`compare`.
- `tests/replay_cache/test_metrics.py` — new, 5 tests from the brief.
- `optexity/schema/memory.py` — added `agentic_run_metrics: list[RunMetrics]` field.
- `optexity/inference/core/interaction/handle_agentic_task.py` — wall-clock
  capture around `agent.run()`, `RunMetrics` construction, append to memory,
  INFO log; **fix round 1**: that block wrapped in its own `try/except
  Exception` so it can never suppress `agent.stop()`/`browser_session.stop()`/
  `.reset()`.
- `tests/replay_cache/test_agentic_metrics_wiring.py` — new, **fix round 1**
  covering test for the wiring and for the cleanup-safety fix (2 tests).
- `test_automation_cached_roboform.json` — new; **fix round 1**: rebuilt from
  Run 1's own trace (12-step cached automation, Site 1) — the original
  20-step version (built from a different run's trace) was overwritten, not
  kept alongside.
- `test_automation_cached_saucedemo.json` — new, committed as evidence
  (11-step cached automation, Site 2) — unaffected by fix round 1.

Note on naming: the brief specifies a single `test_automation_cached.json`,
written for one site. Since this task (per the controller's amendment)
covers two sites, I produced two distinctly-named files instead of
overwriting one with the other — both are real generated evidence, not
placeholders, and each is what was actually copied over
`/Users/kamal/Desktop/optexity/test_automation.json` to run its respective
Run 2/Run 4.

`/Users/kamal/Desktop/optexity/test_automation.json` itself (outside this
repo, the server's cwd) was restored to its pre-task-8 state (the original
Task 4 roboform agentic automation) after the last live run, so the
environment is left as it was found.

## Self-review

- **Metrics actually wired, not just defined** (amendment 1): confirmed by
  grepping `server.log` for `Agentic run metrics` — present exactly twice
  (Run 1, Run 3), each time the agentic path actually ran an LLM loop, and
  absent from both cached runs. This is the exact defect pattern the
  controller warned against (`ReplayCounters` defined-but-uncalled) — here
  verified NOT repeated: the field is appended to and read from a live
  `Memory` instance during an actual server run, and the number in the log
  matches what `from_history()` would compute from that run's own
  `AgentHistoryList`.
- **Import cycle**: verified in a fresh interpreter (see above) — no error
  importing `optexity.schema.memory` then `optexity.inference.core.run_automation`.
- **Site 2 satisfies both original and amended requirements**: genuine
  multi-page workflow (6 distinct pages/states) and every element carries
  `id` + `data-test` — confirmed by inspecting the raw trace's
  `element.attributes` before building the cache entry, not assumed.
- **No `.first` anywhere** in either generated automation — checked both
  `test_automation_cached_roboform.json` and `test_automation_cached_saucedemo.json`
  by grep; none present (consistent with `emit.py`'s design, unchanged by
  this task).
- **Five live runs total** (four in the initial submission, plus one
  corrected roboform cached replay in fix round 1): no retries were needed
  in any of them (every agentic run succeeded on the first attempt; the
  fix-round re-run of the cached automation also succeeded first try). The
  extra run was mandated by fix round 1's Finding 1, not a retry of a
  failure.
- **Visual verification, not just log-trusting**: read the final screenshot
  for every cached run (roboform form fields, both the original and the
  fix-round corrected replay; saucedemo "Checkout: Complete!") rather than
  relying solely on `status: success` and gate counters.
- **Known limitation carried from Task 7, unaffected by this task**: the
  gate's fallback chain (`action.locator_candidates`) is still not threaded
  onto the action schema, so all cached runs show `0 chain recovery` by
  construction — every step happened to resolve on `candidate_rank == 0`
  (primary command), so this task's runs cannot yet demonstrate chain
  recovery on real data. This is an accepted, pre-existing gap (documented in
  Task 7's report and the brief's own self-review), not something this task
  was scoped to close.
- **Fix round 1, finding 1, re-verified**: rebuilt `test_automation_cached_roboform.json`
  from Run 1's own trace (`4a1bb50b…`) rather than the Task-4 trace, re-ran
  the cached replay, and confirmed by direct field inspection of the trace
  file (`python3 -c "..."` row dump, see the "Cache build — Site 1" section)
  that the 12 cached steps trace back to Run 1's own 24-row trace, not a
  different run's.
- **Fix round 1, finding 2, re-verified**: proved the new regression test
  fails without the fix by temporarily reverting the `try/except` guard and
  re-running `pytest tests/replay_cache/test_agentic_metrics_wiring.py -v`
  — `test_raising_metrics_capture_does_not_prevent_cleanup` failed with the
  injected `ValueError` propagating uncaught, then passed again once the
  guard was restored. Not just "should fail" — verified failing.

## Concerns

- The wall-clock "before" numbers include one full LLM agentic session each
  (15 steps for roboform hitting `max_steps`, 7 steps for saucedemo finishing
  early) — these are real single-sample measurements, not averaged over
  multiple runs, so they carry normal LLM-latency variance. The `llm_calls:
  N → 0` and `tokens: N → 0` findings do not have this caveat: they are
  exact and structural (the cached path performs no LLM calls by
  construction), not sampled.
- Total-wall-clock speedup is materially smaller than core-execution
  speedup because of fixed startup/upload overhead identical to both paths
  in this environment; a caller judging "is the cache worth it" purely by
  total request latency in a similarly overhead-heavy environment should use
  the core numbers instead, or amortize the fixed cost over a longer/more
  step-heavy automation where it would matter proportionally less.
- `agentic_run_metrics` is appended per agentic-node call but nothing
  currently aggregates/logs it at end-of-run the way
  `memory.replay_cache.summary()` does in `run_automation.py`. I judged this
  unnecessary for now: the per-run INFO log line at the moment of capture
  already puts the numbers in the task log as required, and a
  multi-agentic-node automation is rare in current usage — but if that
  becomes common, adding an end-of-run rollup log line (sum of wall clocks,
  tokens, calls across `memory.agentic_run_metrics`) in
  `run_automation.py`'s `finally` block, mirroring the existing
  `replay_cache.summary()` call, would be a natural next step.
- The original submission's controller instruction for Site 1 ("reuse the
  existing trace" + "one agentic run per site with metrics captured") was
  self-contradictory when the trace being reused doesn't correspond to that
  fresh run, and I followed it literally rather than flagging the tension
  before building the mismatched artifact. Fix round 1 corrected the
  artifact and the numbers; the general lesson (stated back to me correctly
  in the fix-round message) is to treat "reuse this trace" and "here are
  this run's real numbers" as requiring the trace and the numbers to come
  from the *same* execution, and to say so explicitly if a controller
  instruction would produce a mismatch rather than silently complying.
