# Replay Cache — Design

**Date:** 2026-09-12
**Status:** Approved, ready for implementation planning
**Repos:** `optexity` (this repo, branch `replay-cache`) and the browser-use fork (branch off `optexity`)

---

## 1. Problem

An `agentic_task` node runs browser-use, which re-derives every step from scratch on every
run. The page is flattened to an accessibility tree, an LLM picks an element index, and the
index is converted back into an action. Nothing is remembered between runs, so identical
workflows pay full LLM latency and tokens every time.

Measured on task `de991e4f-85aa-4193-ab62-cfaddd63fd4a` (stockanalysis.com, local run):

| Node | Kind | Wall clock | LLM |
|------|------|-----------|-----|
| `input_text` with a literal `command` | deterministic | 0.3 s | none |
| `click_element`, prompt only | LLM index prediction | 2.5 s | `claude-sonnet-4-6` |
| extraction | LLM | 2.9 s | `claude-sonnet-4-6` |

The deterministic node is roughly 8× faster and free. In a pure agentic run, every node
behaves like the two expensive ones.

**Goal:** run the agent once, cache what it did, and emit a deterministic automation that
replays without LLM calls — without ever replaying something wrong.

## 2. Scope

In scope: capture, pruning, cache entry, emission of `test_automation_cached.json`, replay
safety, and before/after metrics.

Out of scope (assignment bonuses, deliberately not built): LLM-driven automatic generation of
the output automation, and the iterative re-cache convergence loop. The design leaves room for
both but does not implement them.

## 3. Cache identity — decision

**Key:** `sha(endpoint_id + origin + task_text + input_values)`
**Values in steps:** literal, not parameterized.
**Invalidation:** at replay, per step, not in the key.

### Why input values are in the key

Without them, a parameterized automation produces a cache *hit* for the wrong inputs. The key
matches, every locator resolves, nothing throws — and the run returns NVDA's price for an AAPL
request. A cache that misses costs tokens; a cache that lies costs trust, and it is discovered
by a customer rather than by a test.

Including the input values degrades that case to a miss, which is the correct direction to
fail. The assignment's own test automation has `"input_parameters": {}` with values inline in
the task string, so it behaves identically either way there.

### Options rejected

| Option | Rejected because |
|--------|------------------|
| Parameterize values (`{{stock_ticker}}`) | Nothing to bind against in the test case — values live in the task prose, so binding means substring-matching `"myname"`, `"xyz"`, `"SF"` against action params. `"SF"` is two characters and will collide. Remains available later: the key is unchanged, so adding it means input values simply stop participating. No migration. |
| DOM fingerprint in the key | Measures a proxy for "did my element move", wrong in both directions: trips on ad slots, A/B buckets and personalization that touch nothing we clicked; stays put when the one button we care about is renamed. Every false miss is a full LLM re-derivation, the exact cost being removed. |
| Content-address individual steps | Right long-term shape, wrong build now. An automation is ordered — step *n* is only valid on the page step *n−1* produced. Content-addressed fragments drop that precondition and reassembling a correct ordering is a research problem. |

## 4. Architecture — where code lives

### 4.1 The constraint

`optexity` depends on `optexity-browser-use`; nothing in `browser_use` imports `optexity`
(verified: zero references outside the fork's own `pyproject.toml` name). So browser-use
**cannot** import `LocatorExtraction`.

### 4.2 The lossy conversion that forces a browser-use change

`DOMInteractedElement.load_from_enhanced_dom_tree` (`browser_use/dom/views.py:909`) copies
`node_id, backend_node_id, frame_id, node_type, node_value, node_name, attributes, bounds,
x_path, element_hash`. It never reads `ax_node` and never calls
`get_meaningful_text_for_llm()`. `node_value` is not element text — for element nodes it holds
the value of a `TEXT_NODE`, per its own docstring.

Mapped against `LocatorExtraction._scored_candidates`
(`optexity/inference/core/interaction/utils.py:340-417`):

| Tier | Score | Source | Survives conversion |
|------|-------|--------|---------------------|
| `data-testid` | 100 | attributes | yes |
| other test-id attrs | 98 | attributes | yes |
| `id` | 92 | attributes | yes |
| `name` | 84 | attributes | yes |
| `aria-label` | 76 | **attributes**, not `ax_node` | yes |
| role + name | 72 | `ax_node` | **no** |
| `placeholder` | 64 | attributes | yes |
| role + text | 40 | `ax_node` / text | **no** |
| visible text | 38 | text | **no** |
| xpath | 10 | xpath | yes |

Harmless for an element carrying an `id`. Fatal for one whose only stable handle is its
accessible name — a link, a menu item, most buttons. The NVDA click is exactly that: with
`ax_node` it resolves to `get_by_role("link", name="NVDA NVIDIA Corporation Stock")` at 72;
without it, the best remaining candidate is xpath at 10.

This rules out the tempting shortcut. `handle_agentic_task` already receives the full
`AgentHistoryList` and discards it at all three call sites, so the entire cache could be built
in optexity with no browser-use change at all — and would quietly emit xpath locators for
every element without an `id`.

> Note: in production `_scored_candidates` only ever runs on a live `EnhancedDOMTreeNode`
> fetched via `get_dom_element_by_index`, never on a converted element. The loss is not a
> latent bug in optexity; it is a constraint on this design.

### 4.3 The split

**browser-use fork — capture only, no interpretation.**

- `browser_use/replay_cache/recorder.py` (new): serializes raw element signals to JSONL.
- `browser_use/agent/service.py:1059` (edit): one recorder call inside `_make_history_item`,
  where `model_output`, `result`, `metadata` and a live
  `browser_state_summary.dom_state.selector_map` (`dict[int, EnhancedDOMTreeNode]`) are all
  already in scope. No new plumbing through the agent loop.

Guarded so a recorder failure can never affect control flow — the pattern optexity already
uses around its own locator logging.

**optexity fork — all interpretation.**

- `optexity/replay_cache/{adapter,store,prune,emit}.py` (new)
- `handle_agentic_task.py` (edit): enable the recorder, resolve the trace path, hand the trace
  to the cache builder after `agent.run()`.
- `handle_command.py` (edit): the replay gate (§7).

Copying the scorer downstream instead would fork ~250 lines of tuned heuristics — dynamic-id
detection, UUID stripping, consonant-soup rejection — into a second place that will drift.
Raw signals move up; interpretation never moves down. One scorer, one place to fix it.

### 4.4 Adapter contract — a silent failure mode

`_scored_candidates` reads its input with `getattr(element, …, default)`. It wants `tag_name`
and `xpath`. `DOMInteractedElement` exposes `x_path` (underscore) and no `tag_name`.

Get the field names wrong and nothing raises: tag defaults to `"*"`, the xpath tier never
fires, and for an element with no useful attributes the scorer returns an **empty candidate
list** — no locator at all, logged as if it simply found nothing.

The adapter must expose exactly: `tag_name`, `xpath`, `attributes`, `ax_node.role`,
`ax_node.name`, and a callable `get_meaningful_text_for_llm`. This gets a test asserting a
known element yields a known candidate list, written before the adapter.

## 5. Record format

One JSONL line per action:

```json
{
  "step": 2,
  "url": "https://stockanalysis.com/",
  "action": { "type": "click", "index": 2421, "params": {} },
  "element": {
    "tag_name": "a",
    "attributes": { "href": "/stocks/nvda/" },
    "xpath": "/html/body/div/header/div/div[2]/…",
    "ax_role": "link",
    "ax_name": "NVDA NVIDIA Corporation Stock",
    "text": "NVDA NVIDIA Corporation Stock",
    "bounds": { "x": 216, "y": 49, "width": 598, "height": 36 },
    "element_hash": 8823
  },
  "selector_map_hashes": [8823, 9104, 7731],
  "result": { "success": true, "error": null },
  "timing": { "duration_s": 2.5 }
}
```

`selector_map_hashes` — the element hashes present in the selector map at this step — exists
solely for the scroll pruning rule in §6. `result` and `timing` feed pruning and metrics.

## 6. Pruning

Pruning a needed step breaks replay silently, so the rules are conservative and the decision
is ultimately empirical.

### 6.1 Rules

1. **`done`** — a genuinely registered action, terminal, no browser effect. Always dropped.
2. **Non-element actions that changed nothing** — from the real registry: `search`,
   `navigate`, `go_back`, `wait`, `switch`, `close`, `extract`, `send_keys`, `find_text`,
   `screenshot`, `write_file`, `replace_file`, `read_file`, `evaluate`. Kept when the step
   produced output the automation returns, or changed the URL.
3. **Scroll — conditionally.** A `scroll` at step *n* is droppable **only if the element_hash
   of the next element-targeting step already appears in step *n*'s own
   `selector_map_hashes`** — that is, in the selector map as captured before the scroll ran.
   If the hash is absent there and present afterwards, the scroll created the element and is
   load-bearing.
4. **Consecutive duplicates** on the same `(action_type, element_hash)`.

**Never pruned:** failed `input`, `select_dropdown`, `send_keys`, `upload_file`. See §6.3.

### 6.2 Why scroll is not dropped wholesale

An earlier version of this design dropped all scroll steps, reasoning that optexity's command
path calls `scroll_into_view_if_needed` so replay scrolls itself. That is a non sequitur and
was wrong.

`ScrollAction` (`optexity/schema/actions/interaction_action.py:219`) extends `BaseModel`, not
`BaseAction` — it has no `.command` and never enters `command_based_action_with_retry`. It is
handled by `handle_scroll` (`run_interaction.py:176-201`), which runs a *scroll-until-idle
loop* whose explicit purpose is to let content load.

`scroll_into_view_if_needed` (`handle_command.py:103`) fires only when the element is already
visible, and only for the seven element-targeting action types. It brings an **already-indexed**
element into the viewport; it cannot reveal content that does not yet exist. On infinite
scroll, lazy-loaded sections, or virtualized lists, a pruned scroll means the next step's
element is absent from the DOM entirely.

Rule 3 encodes the real distinction: a scroll that merely moved a known element is redundant;
a scroll that *caused* the next element to exist is load-bearing.

### 6.3 Why a failed action is not automatically dropped

`ActionResult.error` is a terminal-outcome flag with no atomicity guarantee. Actions that
mutate page state before they can fail (`input`, `select_dropdown`, `send_keys`, `upload_file`)
are replayed rather than assumed to be no-ops. Verified examples:

- browser-use's `input` clears the field before typing
  (`default_action_watchdog.py:236-270`); a mid-way failure leaves it blanked, then falls back
  to click-and-type.
- `send_keys` may dispatch part of its keystroke sequence before a later character fails.
- Multi-page `scroll` swallows per-page failures and returns **success** anyway
  (`tools/service.py:738-770`) — partial completion reported as full success.

So a failed step may have left state behind, and a successful step may have done less than it
claims.

### 6.4 Pruning is a hypothesis; replay is the test

Emit the pruned candidate, run it once, accept only if it completes. On failure, restore the
most recently dropped class and retry. Under-pruning costs milliseconds; over-pruning breaks
the automation. The assignment requires a verification run regardless, so this is free.

## 7. Replay safety

### 7.1 What already exists

Escalation is already wired end to end, and it is reachable from a command failure:

```
command fails after max_tries
  → returns last_error STRING (does not raise unless assert_locator_presence)
  → handler falls through to the prompt-based *_index path   [unless skip_prompt=True]
  → index predictor returns ≤ 0
  → ElementNotFoundInAxtreeException  (utils.py:578)
  → run_interaction.py:165 → handle_element_not_found_in_axtree → run_axtree_fallback_agent
```

Defaults are `assert_locator_presence=False` and `skip_prompt=False`.

### 7.2 The actual gap — observability, not safety

Three findings reframe what the gate is for:

1. **Playwright already refuses an ambiguous locator.** `click`, `fill`, `is_visible` etc. all
   pass `strict=True`, so a bare locator matching six elements **raises**; it does not click
   the wrong one.
2. **That error is then thrown away.** `handle_command.py:221-223` catches it with a blanket
   `except Exception` into a generic `last_error` string, indistinguishable from a timeout or a
   detached node. Nothing inspects it for "multiple elements matched".
3. **The step then silently becomes an LLM call.** Falling through to the `*_index` path means
   a fully degraded cache still *succeeds* while paying full token cost. Without counters,
   "the cached run worked" is unfalsifiable.

A genuine silent skip — step never executed, no exception, no breadcrumb — occurs only when
`skip_prompt=True` and `assert_locator_presence=False`.

### 7.3 The gate

Before executing a cached step: resolve the locator, `count()`.

- exactly 1 → execute, record `cache_hit`
- 0 or >1 → advance to the next candidate in the stored chain, record the reason
- chain exhausted → enter the existing escalation ladder with a **distinct, counted** signal
  rather than an anonymous error string; re-cache whatever the fallback finds

The stored fallback chain is the full ranked candidate list, which the scorer already produces
and the run currently discards. Near-free, and it converts a large share of would-be-stale
steps into hits.

### 7.4 `.first` is not emitted

The assignment's example output shows `locator("something").first`. In Playwright's Python
implementation `.first` rewrites the selector to `>> nth=0`, which structurally exempts it
from strict mode — it converts "six elements matched" into "silently acted on one of them".
The emitter produces bare locators and lets the gate enforce uniqueness.

### 7.5 `eval` on generated locator strings

`Browser.get_locator_from_command` (`optexity/inference/infra/browser.py:299-306`) is literally
`eval(f"page.{command}")` — no parser, no whitelist, no sandbox. The emitted automation is
therefore executable Python assembled from page-controlled strings (accessible names, text,
attribute values).

`_quote_locator_value` escapes `\` and `"`, collapses all whitespace, and truncates at 120
characters, so quote-breaking appears blocked. Not treated as an alarm, but the emitter gets a
test that feeds adversarial page text (quotes, backslashes, newlines, `")` sequences) through
candidate generation and asserts the result is inert.

## 8. Metrics

Captured for the agentic run and the cached run:

- wall clock, total and per step
- LLM call count
- tokens from `AgentHistoryList.usage`
- steps executed
- `cache_hit` / `prompt_fallback` / `agentic_fallback` counts

Caveats, verified:

- `StepMetadata` carries **no** token fields despite its docstring claiming otherwise — only
  `step_start_time`, `step_end_time`, `step_number`.
- `history.usage` is assigned only on normal completion and on `KeyboardInterrupt`. If the run
  raises, it stays `None`. Capture must tolerate that.
- Token counts depend on the provider returning a `usage` object; `TokenCost.register_llm`
  skips silently when absent.
- Cost is off by default, but `handle_agentic_task` already passes `calculate_cost=True`.

## 9. Scale

Not built now, but the shape is chosen so they stay cheap:

- **Store seam.** `CacheStore` protocol — `get(key)` / `put(key, entry)`. `FileCacheStore`
  now. Optexity dispatches across a fleet of child processes, so a local-disk cache is learned
  by one worker and invisible to the next: at *N* workers, roughly 1/N of the benefit and *N*
  copies of the learning cost. Keying on `endpoint_id` rather than anything machine-local keeps
  an entry portable; the protocol keeps the swap off the call sites.
- Prefix replay with an agentic tail, per-step health statistics, canary re-derivation —
  all deferred, none blocked by these choices.

## 10. Test plan

1. Adapter yields the expected ranked candidate list for a known element (§4.4).
2. Adversarial page text through candidate generation stays inert (§7.5).
3. Pruner keeps a load-bearing scroll on a lazy-loading fixture (§6.2).
4. Pruner never drops a failed `check` (§6.3).
5. Gate falls through the candidate chain on a deliberately ambiguous locator and records the
   reason (§7.3).
6. End-to-end: roboform agentic run → cache → emitted automation → verification replay, with
   the metrics of §8 captured for both.
7. End-to-end on a second, multi-step site involving navigation between pages.

## 11. Verification record

Every load-bearing claim in this document was audited against the source by three independent
review passes instructed to refute rather than confirm. Five claims in earlier drafts were
wrong and have been corrected here:

- `think` and `read_state` were named as prunable actions; neither is a registered action.
- "Drop all scroll actions" rested on a non sequitur (§6.2).
- "A failed action had no effect" is false (§6.3).
- "A dead locator silently skips the step" — it silently becomes an LLM call instead (§7.2).
- "The agentic fallback needs rewiring" — it is already reachable from a command failure
  (§7.1).

Unrelated, worth knowing: the browser-use fork inherits upstream's `CLAUDE.md`, which contains
a "Personality" section (commit `97d022d5`, upstream author) instructing assistants to print
`"!!!!"` when policy blocks a response, adopt a named persona, and reply dismissively. Not
adversarial in origin, but it is live instruction text for any agent working in that repo. Left
untouched — it is unrelated to this change.
