# Replay Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a browser-use agentic task once, cache what it did, and emit a deterministic automation that replays the same workflow with no LLM calls.

**Architecture:** The browser-use fork records raw DOM signals per step to a JSONL trace and interprets nothing. Optexity reads that trace, scores locator candidates with its existing `LocatorExtraction`, prunes redundant steps, and emits a cached automation. At replay, every cached locator is uniqueness-checked before it commits, and hits are counted separately from fallbacks.

**Tech Stack:** Python 3.11+, pydantic v2, Playwright, pytest. Two git repos, one-directional dependency (`optexity` → `optexity-browser-use`).

**Spec:** `docs/superpowers/specs/2026-09-12-replay-cache-design.md`

## Global Constraints

- **Dependency direction is one-way.** Nothing under `browser_use/` may import `optexity`. The recorder emits plain dicts; only optexity parses them.
- **The recorder must never affect agent control flow.** Every recorder entry point wraps its whole body in `try/except Exception` and swallows.
- **The recorder is inert unless `OPTEXITY_REPLAY_CACHE_TRACE` is set** to a writable file path. No signature changes to `Agent`.
- **Adapter field names are exact:** `tag_name`, `xpath`, `attributes`, `ax_node.role`, `ax_node.name`, `get_meaningful_text_for_llm` (callable). `DOMInteractedElement` uses `x_path` and has no `tag_name` — getting this wrong returns an empty candidate list with no error.
- **Never emit `.first`** in a generated locator command. It rewrites the selector to `>> nth=0` and exempts it from Playwright strict mode.
- **Never prune** a `check` / `uncheck` step, or a failed `input` step.
- Run everything with the project venv: `/Users/kamal/Desktop/optexity/.venv/bin/python` and `.../.venv/bin/pytest`.
- `ENV_PATH=/Users/kamal/Desktop/optexity/.env` must be set for any command that imports `optexity.utils.settings`.

---

### Task 1: Scaffolding and the adapter

The adapter is first because it carries the spec's named silent-failure mode (§4.4). Branch creation, pytest install and the tests directory fold in here since this task's tests need them.

**Files:**
- Create: `optexity/replay_cache/__init__.py`
- Create: `optexity/replay_cache/records.py`
- Create: `optexity/replay_cache/adapter.py`
- Create: `tests/replay_cache/__init__.py`
- Test: `tests/replay_cache/test_adapter.py`
- Modify: `pyproject.toml` (add a pytest section)

**Interfaces:**
- Consumes: `optexity.inference.core.interaction.utils.LocatorExtraction` (existing).
- Produces:
  - `ElementSignals` pydantic model with fields `tag_name: str`, `attributes: dict[str, str]`, `xpath: str`, `ax_role: str | None`, `ax_name: str | None`, `text: str`, `bounds: dict[str, float] | None`, `element_hash: int | None`
  - `ActionRecord` with `type: str`, `index: int | None`, `params: dict`
  - `StepRecord` with `step: int`, `url: str`, `action: ActionRecord`, `element: ElementSignals | None`, `selector_map_hashes: list[int]`, `success: bool`, `error: str | None`, `duration_s: float | None`
  - `to_scorer_element(signals: ElementSignals) -> SimpleNamespace`

- [ ] **Step 1: Create the branch in this repo**

Already on `replay-cache` if the spec commit landed. Verify:

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
git branch --show-current
```

Expected: `replay-cache`. If not: `git checkout -b replay-cache`.

- [ ] **Step 2: Install pytest into the shared venv**

pytest is not currently installed.

```bash
/Users/kamal/Desktop/optexity/.venv/bin/pip install pytest pytest-asyncio
```

- [ ] **Step 3: Add a pytest section to pyproject.toml**

Append to `/Users/kamal/Desktop/optexity/optexity-fork/pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
asyncio_mode = "auto"
```

- [ ] **Step 4: Create the package directories**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
mkdir -p optexity/replay_cache tests/replay_cache
touch optexity/replay_cache/__init__.py tests/replay_cache/__init__.py
```

- [ ] **Step 5: Write the record models**

Create `optexity/replay_cache/records.py`:

```python
"""Wire format for the browser-use step trace.

The browser-use fork writes these as plain dicts (it cannot import optexity).
This module is the only place that knows how to read them back.
"""

from pydantic import BaseModel, Field


class ElementSignals(BaseModel):
    """Everything LocatorExtraction needs about one DOM element.

    Captured from the live EnhancedDOMTreeNode, because the conversion to
    DOMInteractedElement drops ax_node and element text.
    """

    tag_name: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)
    xpath: str = ""
    ax_role: str | None = None
    ax_name: str | None = None
    text: str = ""
    bounds: dict[str, float] | None = None
    element_hash: int | None = None


class ActionRecord(BaseModel):
    type: str
    index: int | None = None
    params: dict = Field(default_factory=dict)


class StepRecord(BaseModel):
    step: int
    url: str
    action: ActionRecord
    element: ElementSignals | None = None
    # Element hashes present in the selector map BEFORE this step ran.
    # Used by the scroll pruning rule to tell "moved a known element" from
    # "caused the next element to exist".
    selector_map_hashes: list[int] = Field(default_factory=list)
    success: bool = True
    error: str | None = None
    duration_s: float | None = None
```

- [ ] **Step 6: Write the failing adapter test**

Create `tests/replay_cache/test_adapter.py`:

```python
from optexity.inference.core.interaction.utils import LocatorExtraction
from optexity.replay_cache.adapter import to_scorer_element
from optexity.replay_cache.records import ElementSignals

# The NVDA search-result link from task de991e4f. Its only stable handle is
# its accessible name: no id, no name attribute, no test id.
NVDA_LINK = ElementSignals(
    tag_name="a",
    attributes={"href": "/stocks/nvda/"},
    xpath="/html/body/div/header/div/div[2]/form/div/a",
    ax_role="link",
    ax_name="NVDA NVIDIA Corporation Stock",
    text="NVDA NVIDIA Corporation Stock",
    element_hash=8823,
)


def _kinds(signals):
    cands = LocatorExtraction.locator_candidates(to_scorer_element(signals), ".click()")
    return {c["kind"]: c for c in cands}


def test_role_and_name_tier_is_reachable_through_the_adapter():
    """ax_node must arrive as an object with .role and .name, not as flat fields."""
    kinds = _kinds(NVDA_LINK)
    assert "role+name" in kinds, f"role+name tier missing; got {sorted(kinds)}"
    assert kinds["role+name"]["score"] == 72
    assert 'get_by_role("link", name="NVDA NVIDIA Corporation Stock")' in kinds["role+name"]["locator"]


def test_xpath_tier_is_reachable_through_the_adapter():
    """Guards the x_path/xpath naming trap: a wrong field name yields no xpath tier."""
    kinds = _kinds(NVDA_LINK)
    assert "xpath" in kinds, f"xpath tier missing; got {sorted(kinds)}"
    assert kinds["xpath"]["score"] == 10


def test_attribute_tiers_still_work():
    """An element with an id must still produce the id tier at 92."""
    signals = ElementSignals(
        tag_name="input",
        attributes={"id": "search-header", "name": "q"},
        xpath="/html/body/div/header/div/div[2]/form/div/input",
    )
    kinds = _kinds(signals)
    assert kinds["id"]["score"] == 92
    assert 'locator("#search-header")' in kinds["id"]["locator"]


def test_empty_candidate_list_is_never_silently_acceptable():
    """A bare element with nothing to anchor on must still yield the xpath tier."""
    signals = ElementSignals(tag_name="div", xpath="/html/body/div[3]")
    cands = LocatorExtraction.locator_candidates(to_scorer_element(signals), ".click()")
    assert cands, "adapter produced no candidates at all — check field names"
```

- [ ] **Step 7: Run the test to verify it fails**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_adapter.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'optexity.replay_cache.adapter'`

- [ ] **Step 8: Write the adapter**

Create `optexity/replay_cache/adapter.py`:

```python
"""Shim ElementSignals into the duck-typed shape LocatorExtraction expects.

LocatorExtraction._scored_candidates reads its input with getattr(..., default),
so a misspelled field does not raise — it silently drops a whole locator tier.
The names below are load-bearing and covered by tests/replay_cache/test_adapter.py.
"""

from types import SimpleNamespace

from optexity.replay_cache.records import ElementSignals


def to_scorer_element(signals: ElementSignals) -> SimpleNamespace:
    """Adapt a recorded element to what LocatorExtraction._scored_candidates reads.

    Required exactly: tag_name, attributes, xpath, ax_node.role, ax_node.name,
    and a callable get_meaningful_text_for_llm.
    """
    return SimpleNamespace(
        tag_name=signals.tag_name or "",
        attributes=dict(signals.attributes or {}),
        xpath=signals.xpath or "",
        ax_node=SimpleNamespace(role=signals.ax_role, name=signals.ax_name),
        get_meaningful_text_for_llm=lambda: signals.text or "",
    )
```

- [ ] **Step 9: Run the tests to verify they pass**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_adapter.py -v
```

Expected: 4 passed.

- [ ] **Step 10: Commit**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
git add optexity/replay_cache tests/replay_cache pyproject.toml
git commit -m "feat(replay-cache): record models and locator-scorer adapter

The adapter's field names are load-bearing: _scored_candidates reads its
input with getattr and a default, so x_path instead of xpath drops the
whole xpath tier without raising. Tests pin the tiers rather than the
adapter's internals."
```

---

### Task 2: Cache key and store

**Files:**
- Create: `optexity/replay_cache/key.py`
- Create: `optexity/replay_cache/store.py`
- Test: `tests/replay_cache/test_key.py`
- Test: `tests/replay_cache/test_store.py`

**Interfaces:**
- Consumes: `StepRecord` from Task 1.
- Produces:
  - `cache_key(endpoint_name: str, url: str, task_text: str, input_parameters: dict) -> str` — 16-char hex
  - `CachedStep` model: `action_type: str`, `command: str`, `candidates: list[dict]`, `input_text: str | None`, `prompt_instructions: str`
  - `CacheEntry` model: `key: str`, `endpoint_name: str`, `origin: str`, `created_at: str`, `steps: list[CachedStep]`
  - `CacheStore` Protocol with `get(key) -> CacheEntry | None` and `put(key, entry) -> None`
  - `FileCacheStore(root: Path)` implementing it

- [ ] **Step 1: Write the failing key test**

Create `tests/replay_cache/test_key.py`:

```python
from optexity.replay_cache.key import cache_key

TASK = "fill the full name as myname, address line one as xyz"


def test_same_inputs_produce_the_same_key():
    a = cache_key("ep1", "https://example.com/a", TASK, {"t": ["NVDA"]})
    b = cache_key("ep1", "https://example.com/a", TASK, {"t": ["NVDA"]})
    assert a == b


def test_different_input_values_produce_different_keys():
    """The decision that stops a cache hit returning the previous run's answer."""
    nvda = cache_key("ep1", "https://example.com/a", TASK, {"t": ["NVDA"]})
    aapl = cache_key("ep1", "https://example.com/a", TASK, {"t": ["AAPL"]})
    assert nvda != aapl


def test_key_ignores_path_and_query_but_not_origin():
    same = cache_key("ep1", "https://example.com/a?x=1", TASK, {})
    also = cache_key("ep1", "https://example.com/b", TASK, {})
    other = cache_key("ep1", "https://other.com/a", TASK, {})
    assert same == also
    assert same != other


def test_input_parameter_ordering_does_not_matter():
    a = cache_key("ep1", "https://example.com", TASK, {"a": ["1"], "b": ["2"]})
    b = cache_key("ep1", "https://example.com", TASK, {"b": ["2"], "a": ["1"]})
    assert a == b


def test_task_text_participates():
    a = cache_key("ep1", "https://example.com", "do X", {})
    b = cache_key("ep1", "https://example.com", "do Y", {})
    assert a != b
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_key.py -v
```

Expected: FAIL — `No module named 'optexity.replay_cache.key'`

- [ ] **Step 3: Write the key module**

Create `optexity/replay_cache/key.py`:

```python
"""Cache identity.

Input values are part of the key on purpose. Without them, a parameterised
automation produces a cache HIT for the wrong inputs: the key matches, every
locator resolves, nothing raises, and the run returns the previous input's
answer. Including them degrades that case to a miss instead.
"""

import hashlib
import json
from urllib.parse import urlsplit


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def cache_key(
    endpoint_name: str,
    url: str,
    task_text: str,
    input_parameters: dict,
) -> str:
    """Stable 16-char identity for one cacheable automation run."""
    payload = json.dumps(
        {
            "endpoint": endpoint_name,
            "origin": origin_of(url),
            "task": " ".join((task_text or "").split()),
            "inputs": input_parameters or {},
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 4: Run it to verify it passes**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_key.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Write the failing store test**

Create `tests/replay_cache/test_store.py`:

```python
from optexity.replay_cache.store import CachedStep, CacheEntry, FileCacheStore

ENTRY = CacheEntry(
    key="abc123",
    endpoint_name="ep1",
    origin="https://example.com",
    created_at="2026-09-12T00:00:00Z",
    steps=[
        CachedStep(
            action_type="input_text",
            command='locator("#search-header")',
            candidates=[{"locator": 'page.locator("#search-header").fill()', "kind": "id", "score": 92}],
            input_text="NVDA",
            prompt_instructions="Enter the ticker in the search field",
        )
    ],
)


def test_put_then_get_round_trips(tmp_path):
    store = FileCacheStore(tmp_path)
    store.put("abc123", ENTRY)
    got = store.get("abc123")
    assert got is not None
    assert got.steps[0].command == 'locator("#search-header")'
    assert got.steps[0].input_text == "NVDA"


def test_get_returns_none_for_unknown_key(tmp_path):
    assert FileCacheStore(tmp_path).get("nope") is None


def test_get_returns_none_for_corrupt_entry(tmp_path):
    store = FileCacheStore(tmp_path)
    (tmp_path / "broken.json").write_text("{not json")
    assert store.get("broken") is None


def test_put_creates_the_root_directory(tmp_path):
    store = FileCacheStore(tmp_path / "nested" / "deeper")
    store.put("abc123", ENTRY)
    assert store.get("abc123") is not None
```

- [ ] **Step 6: Run it to verify it fails**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_store.py -v
```

Expected: FAIL — `No module named 'optexity.replay_cache.store'`

- [ ] **Step 7: Write the store**

Create `optexity/replay_cache/store.py`:

```python
"""Cache persistence behind a two-method protocol.

Optexity dispatches tasks across a fleet of child processes, so a cache on
local disk is learned by one worker and invisible to the next. FileCacheStore
is the local implementation; a shared store later implements the same Protocol
and no call site changes.
"""

import json
import logging
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class CachedStep(BaseModel):
    action_type: str
    command: str
    # Full ranked candidate list, best first. The replay gate walks this when
    # the primary command fails to resolve uniquely.
    candidates: list[dict] = Field(default_factory=list)
    input_text: str | None = None
    prompt_instructions: str = ""


class CacheEntry(BaseModel):
    key: str
    endpoint_name: str
    origin: str
    created_at: str
    steps: list[CachedStep] = Field(default_factory=list)


class CacheStore(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...
    def put(self, key: str, entry: CacheEntry) -> None: ...


class FileCacheStore:
    """One JSON file per cache entry, named by key."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> CacheEntry | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return CacheEntry.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Discarding unreadable cache entry {path}: {e}")
            return None

    def put(self, key: str, entry: CacheEntry) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(
            json.dumps(entry.model_dump(), indent=2), encoding="utf-8"
        )
```

- [ ] **Step 8: Run both test files to verify they pass**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache -v
```

Expected: 13 passed.

- [ ] **Step 9: Commit**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
git add optexity/replay_cache/key.py optexity/replay_cache/store.py tests/replay_cache/test_key.py tests/replay_cache/test_store.py
git commit -m "feat(replay-cache): cache key and file-backed store

Input values participate in the key so a parameterised automation misses
rather than replaying the previous run's values against a new request.
Store is a Protocol so the fleet-wide implementation later is a swap, not
a refactor."
```

---

### Task 3: The browser-use recorder

**Files:**
- Create: `browser_use/replay_cache/__init__.py` (in the browser-use fork)
- Create: `browser_use/replay_cache/recorder.py`
- Test: `tests/ci/test_replay_cache_recorder.py`

**Interfaces:**
- Consumes: nothing from optexity — this repo cannot import it.
- Produces: `StepRecorder` with classmethod `from_env() -> StepRecorder | None` and method `record(*, step: int, url: str, actions: list, results: list, selector_map: dict, duration_s: float | None) -> None`. Writes one JSON object per line matching the `StepRecord` shape from Task 1.

- [ ] **Step 1: Create the branch in the browser-use fork**

The fork is currently on the bare `optexity` branch. Branch from it — not from `main`, which is vanilla upstream browser-use with a different package name.

```bash
cd /Users/kamal/Desktop/optexity/browser-use
git branch --show-current   # expect: optexity
git checkout -b replay-cache
```

- [ ] **Step 2: Create the package**

```bash
cd /Users/kamal/Desktop/optexity/browser-use
mkdir -p browser_use/replay_cache
touch browser_use/replay_cache/__init__.py
```

- [ ] **Step 3: Write the failing recorder test**

Create `tests/ci/test_replay_cache_recorder.py`:

```python
import json
from types import SimpleNamespace

from browser_use.replay_cache.recorder import StepRecorder


def _node(**kw):
    """A stand-in for EnhancedDOMTreeNode carrying only what the recorder reads."""
    defaults = dict(
        node_name="A",
        attributes={"href": "/stocks/nvda/"},
        xpath="/html/body/div/a",
        ax_node=SimpleNamespace(role="link", name="NVDA NVIDIA Corporation Stock"),
        snapshot_node=None,
    )
    defaults.update(kw)
    node = SimpleNamespace(**defaults)
    node.get_meaningful_text_for_llm = lambda: "NVDA NVIDIA Corporation Stock"
    return node


def _action(name, **params):
    return SimpleNamespace(
        model_dump=lambda exclude_none=True: {name: params},
        get_index=lambda: params.get("index"),
    )


def test_records_one_line_per_action(tmp_path):
    trace = tmp_path / "trace.jsonl"
    rec = StepRecorder(trace)
    rec.record(
        step=1,
        url="https://stockanalysis.com/",
        actions=[_action("click", index=2421)],
        results=[SimpleNamespace(error=None)],
        selector_map={2421: _node()},
        duration_s=2.5,
    )
    lines = trace.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["action"]["type"] == "click"
    assert row["action"]["index"] == 2421
    assert row["url"] == "https://stockanalysis.com/"
    assert row["duration_s"] == 2.5


def test_captures_the_signals_history_drops(tmp_path):
    """ax_role, ax_name and text are the whole reason this lives in browser-use."""
    trace = tmp_path / "trace.jsonl"
    StepRecorder(trace).record(
        step=1,
        url="https://stockanalysis.com/",
        actions=[_action("click", index=2421)],
        results=[SimpleNamespace(error=None)],
        selector_map={2421: _node()},
        duration_s=None,
    )
    el = json.loads(trace.read_text().strip())["element"]
    assert el["ax_role"] == "link"
    assert el["ax_name"] == "NVDA NVIDIA Corporation Stock"
    assert el["text"] == "NVDA NVIDIA Corporation Stock"
    assert el["tag_name"] == "a"          # lowercased from node_name
    assert el["xpath"] == "/html/body/div/a"


def test_records_selector_map_hashes_before_the_action(tmp_path):
    """The scroll pruning rule needs to know what existed before each step."""
    trace = tmp_path / "trace.jsonl"
    StepRecorder(trace).record(
        step=1,
        url="https://example.com/",
        actions=[_action("scroll", down=True)],
        results=[SimpleNamespace(error=None)],
        selector_map={11: _node(), 12: _node()},
        duration_s=None,
    )
    row = json.loads(trace.read_text().strip())
    assert len(row["selector_map_hashes"]) == 2


def test_records_failure(tmp_path):
    trace = tmp_path / "trace.jsonl"
    StepRecorder(trace).record(
        step=1,
        url="https://example.com/",
        actions=[_action("click", index=99)],
        results=[SimpleNamespace(error="element not found")],
        selector_map={},
        duration_s=None,
    )
    row = json.loads(trace.read_text().strip())
    assert row["success"] is False
    assert row["error"] == "element not found"
    assert row["element"] is None


def test_never_raises_on_a_hostile_node(tmp_path):
    """A recorder failure must never break the agent loop."""
    trace = tmp_path / "trace.jsonl"

    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    StepRecorder(trace).record(
        step=1,
        url="https://example.com/",
        actions=[_action("click", index=1)],
        results=[SimpleNamespace(error=None)],
        selector_map={1: Exploding()},
        duration_s=None,
    )
    # No exception is the assertion.


def test_from_env_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("OPTEXITY_REPLAY_CACHE_TRACE", raising=False)
    assert StepRecorder.from_env() is None


def test_from_env_builds_a_recorder_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("OPTEXITY_REPLAY_CACHE_TRACE", str(tmp_path / "t.jsonl"))
    assert StepRecorder.from_env() is not None
```

- [ ] **Step 4: Run it to verify it fails**

```bash
cd /Users/kamal/Desktop/optexity/browser-use
/Users/kamal/Desktop/optexity/.venv/bin/pytest tests/ci/test_replay_cache_recorder.py -p no:cacheprovider
```

Expected: FAIL — `No module named 'browser_use.replay_cache.recorder'`

- [ ] **Step 5: Write the recorder**

Create `browser_use/replay_cache/recorder.py`:

```python
"""Records raw per-step DOM signals to a JSONL trace.

Captures nothing that requires interpretation: no locators, no scoring, no
knowledge of optexity's automation schema. It exists here rather than upstream
because DOMInteractedElement.load_from_enhanced_dom_tree drops ax_node and
element text, and those are gone by the time AgentHistory is returned.

Inert unless OPTEXITY_REPLAY_CACHE_TRACE names a writable path. Every public
entry point swallows exceptions: a recorder failure must never affect the
agent loop.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

TRACE_ENV_VAR = 'OPTEXITY_REPLAY_CACHE_TRACE'


def _element_signals(node) -> dict | None:
	"""Pull the signals a locator scorer needs off a live EnhancedDOMTreeNode."""
	if node is None:
		return None
	ax = getattr(node, 'ax_node', None)
	try:
		text = node.get_meaningful_text_for_llm() or ''
	except Exception:
		text = ''
	bounds = None
	snapshot = getattr(node, 'snapshot_node', None)
	raw_bounds = getattr(snapshot, 'bounds', None) if snapshot else None
	if raw_bounds is not None:
		bounds = {
			'x': getattr(raw_bounds, 'x', 0.0),
			'y': getattr(raw_bounds, 'y', 0.0),
			'width': getattr(raw_bounds, 'width', 0.0),
			'height': getattr(raw_bounds, 'height', 0.0),
		}
	try:
		element_hash = hash(node)
	except Exception:
		element_hash = None
	return {
		'tag_name': (getattr(node, 'node_name', '') or '').lower(),
		'attributes': dict(getattr(node, 'attributes', None) or {}),
		'xpath': getattr(node, 'xpath', '') or '',
		'ax_role': getattr(ax, 'role', None) if ax else None,
		'ax_name': getattr(ax, 'name', None) if ax else None,
		'text': text,
		'bounds': bounds,
		'element_hash': element_hash,
	}


def _action_name_and_params(action) -> tuple[str, dict]:
	dumped = action.model_dump(exclude_none=True)
	if not dumped:
		return 'unknown', {}
	name = next(iter(dumped))
	params = dumped[name] if isinstance(dumped[name], dict) else {}
	return name, params


class StepRecorder:
	def __init__(self, path):
		self.path = Path(path)

	@classmethod
	def from_env(cls) -> 'StepRecorder | None':
		path = os.environ.get(TRACE_ENV_VAR)
		return cls(path) if path else None

	def record(self, *, step, url, actions, results, selector_map, duration_s) -> None:
		"""Append one line per action. Never raises."""
		try:
			self.path.parent.mkdir(parents=True, exist_ok=True)
			hashes = []
			for node in (selector_map or {}).values():
				try:
					hashes.append(hash(node))
				except Exception:
					continue
			with open(self.path, 'a', encoding='utf-8') as fh:
				for i, action in enumerate(actions or []):
					try:
						name, params = _action_name_and_params(action)
						index = action.get_index()
					except Exception:
						name, params, index = 'unknown', {}, None
					result = results[i] if results and i < len(results) else None
					error = getattr(result, 'error', None) if result else None
					node = (selector_map or {}).get(index) if index is not None else None
					try:
						element = _element_signals(node)
					except Exception:
						element = None
					row = {
						'step': step,
						'url': url,
						'action': {'type': name, 'index': index, 'params': params},
						'element': element,
						'selector_map_hashes': hashes,
						'success': error is None,
						'error': error,
						'duration_s': duration_s,
					}
					fh.write(json.dumps(row, default=str) + '\n')
		except Exception as e:
			logger.debug(f'replay-cache recorder skipped step {step}: {type(e).__name__}: {e}')
```

Note: this repo indents with tabs. Match it.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd /Users/kamal/Desktop/optexity/browser-use
/Users/kamal/Desktop/optexity/.venv/bin/pytest tests/ci/test_replay_cache_recorder.py -p no:cacheprovider
```

Expected: 7 passed.

- [ ] **Step 7: Commit**

```bash
cd /Users/kamal/Desktop/optexity/browser-use
git add browser_use/replay_cache tests/ci/test_replay_cache_recorder.py
git commit -m "feat(replay-cache): record raw per-step DOM signals to a trace

Captures ax_role, ax_name and element text, which the conversion to
DOMInteractedElement drops and which the locator scorer needs for any
element whose only stable handle is its accessible name.

Inert unless OPTEXITY_REPLAY_CACHE_TRACE is set, and swallows its own
exceptions so it can never affect the agent loop."
```

---

### Task 4: Wire the recorder into the agent loop

**Files:**
- Modify: `browser_use/agent/service.py` (in `_make_history_item`, around line 1059)
- Modify: `optexity/inference/core/interaction/handle_agentic_task.py`

**Interfaces:**
- Consumes: `StepRecorder.from_env()` from Task 3; `cache_key` from Task 2.
- Produces: a real `trace.jsonl` on disk after an agentic run. `trace_path_for(task, memory) -> Path` in `handle_agentic_task.py`.

- [ ] **Step 1: Add the recorder call in browser-use**

In `browser_use/agent/service.py`, inside `_make_history_item`, immediately after the `interacted_elements` assignment (currently line 1059), insert:

```python
		recorder = StepRecorder.from_env()
		if recorder is not None and model_output:
			recorder.record(
				step=self.state.n_steps,
				url=browser_state_summary.url,
				actions=model_output.action,
				results=result,
				selector_map=browser_state_summary.dom_state.selector_map,
				duration_s=metadata.duration_seconds if metadata else None,
			)
```

Add the import at the top of the file, beside the other `browser_use` imports:

```python
from browser_use.replay_cache.recorder import StepRecorder
```

- [ ] **Step 2: Verify browser-use still imports cleanly**

```bash
cd /Users/kamal/Desktop/optexity
.venv/bin/python -c "import browser_use; from browser_use.agent.service import Agent; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit the browser-use side**

```bash
cd /Users/kamal/Desktop/optexity/browser-use
git add browser_use/agent/service.py
git commit -m "feat(replay-cache): call the step recorder from _make_history_item

One call site: model_output, the live selector_map, result and metadata
are all already in scope there, so no plumbing through the agent loop."
```

- [ ] **Step 4: Set the trace path from optexity**

In `optexity/inference/core/interaction/handle_agentic_task.py`, add near the top:

```python
import os
from pathlib import Path

REPLAY_CACHE_TRACE_ENV = "OPTEXITY_REPLAY_CACHE_TRACE"


def trace_path_for(task: Task, memory: Memory) -> Path:
    """Where this agentic step's raw trace is written."""
    return (
        task.logs_directory
        / f"step_{memory.automation_state.step_index}"
        / "replay_cache_trace.jsonl"
    )
```

Then inside `handle_agentic_task`, after `step_directory.mkdir(parents=True, exist_ok=True)` and before `agent = Agent(...)`:

```python
        trace_path = trace_path_for(task, memory)
        os.environ[REPLAY_CACHE_TRACE_ENV] = str(trace_path)
        logger.debug(f"Replay-cache trace for this step: {trace_path}")
```

And after `await agent.browser_session.stop()` / `reset()`, before `return history`:

```python
        os.environ.pop(REPLAY_CACHE_TRACE_ENV, None)
        if trace_path.exists():
            logger.info(
                f"Replay-cache trace written: {trace_path} "
                f"({sum(1 for _ in trace_path.open())} step records)"
            )
```

- [ ] **Step 5: Capture a real trace**

Put the spec's starting automation in `test_automation.json`. The local-override
patch already lives in `child_process.py` (after the task is fetched, before
`task_running = True`), but it is gated on **two** conditions: the file must exist
*and* `OPTEXITY_LOCAL_AUTOMATION_OVERRIDE` must be set to a truthy value
(`1`/`true`/`yes`/`on`). The file alone does nothing — that guard is what keeps a
stray `test_automation.json` in a worker's cwd from silently hijacking every task
on that worker. When it fires it logs at INFO.

Then restart the inference server with the override enabled and fire the endpoint:

```bash
cd /Users/kamal/Desktop/optexity
OPTEXITY_LOCAL_AUTOMATION_OVERRIDE=1 \
ENV_PATH=/Users/kamal/Desktop/optexity/.env .venv/bin/optexity inference --port 9000 --child_process_id 0
# in another shell:
curl -X POST http://localhost:9000/inference -H "Content-Type: application/json" \
  -d '{"endpoint_name":"<your endpoint>","input_parameters":{},"unique_parameter_names":[]}'
```

Expected: the server log prints `Replay-cache trace written: … (N step records)`.

- [ ] **Step 6: Confirm the trace carries the signals that matter**

```bash
cd /Users/kamal/Desktop/optexity
python3 -c "
import json,sys,glob
p=sorted(glob.glob('**/replay_cache_trace.jsonl', recursive=True))[-1]
rows=[json.loads(l) for l in open(p)]
print(p, len(rows), 'records')
for r in rows:
    el = r.get('element') or {}
    print(r['step'], r['action']['type'], '|ax_role=', el.get('ax_role'), '|ax_name=', (el.get('ax_name') or '')[:40])
"
```

Expected: at least one row with a non-null `ax_role`. If every row is null, the recorder is reading the wrong node shape — stop and fix before continuing.

- [ ] **Step 7: Commit the optexity side**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
git add optexity/inference/core/interaction/handle_agentic_task.py
git commit -m "feat(replay-cache): point the recorder at a per-step trace file

Communicates the path by environment variable so browser-use needs no
signature change and stays inert for every caller that does not set it."
```

---

### Task 5: The pruner

**Files:**
- Create: `optexity/replay_cache/prune.py`
- Test: `tests/replay_cache/test_prune.py`

**Interfaces:**
- Consumes: `StepRecord`, `ActionRecord`, `ElementSignals` from Task 1.
- Produces:
  - `PruneDecision` model: `step: int`, `action_type: str`, `kept: bool`, `reason: str`
  - `prune(steps: list[StepRecord]) -> tuple[list[StepRecord], list[PruneDecision]]`

- [ ] **Step 1: Write the failing pruner test**

Create `tests/replay_cache/test_prune.py`:

```python
from optexity.replay_cache.prune import prune
from optexity.replay_cache.records import ActionRecord, ElementSignals, StepRecord


def step(n, type_, *, index=None, hashes=None, success=True, el_hash=None, error=None):
    return StepRecord(
        step=n,
        url="https://example.com/",
        action=ActionRecord(type=type_, index=index),
        element=ElementSignals(tag_name="a", element_hash=el_hash) if el_hash else None,
        selector_map_hashes=hashes or [],
        success=success,
        error=error,
    )


def kept_types(steps):
    kept, _ = prune(steps)
    return [s.action.type for s in kept]


def test_done_is_always_dropped():
    assert kept_types([step(1, "click", el_hash=1), step(2, "done")]) == ["click"]


def test_redundant_scroll_is_dropped():
    """Target already existed before the scroll, so replay will scroll to it itself."""
    steps = [
        step(1, "scroll", hashes=[77]),
        step(2, "click", index=5, el_hash=77),
    ]
    assert kept_types(steps) == ["click"]


def test_load_bearing_scroll_is_kept():
    """Target did NOT exist before the scroll — the scroll created it."""
    steps = [
        step(1, "scroll", hashes=[10, 11]),
        step(2, "click", index=5, el_hash=77),
    ]
    assert kept_types(steps) == ["scroll", "click"]


def test_failed_check_is_never_pruned():
    """check_locator calls uncheck() then check(); a failure may have toggled it."""
    steps = [step(1, "check", index=3, el_hash=5, success=False, error="timeout")]
    assert kept_types(steps) == ["check"]


def test_failed_input_is_never_pruned():
    """browser-use clears the field before typing, so a failure leaves it blank."""
    steps = [step(1, "input", index=3, el_hash=5, success=False, error="boom")]
    assert kept_types(steps) == ["input"]


def test_failed_click_is_dropped():
    steps = [step(1, "click", index=3, el_hash=5, success=False, error="boom"),
             step(2, "click", index=4, el_hash=6)]
    assert kept_types(steps) == ["click"]
    kept, _ = prune(steps)
    assert kept[0].step == 2


def test_consecutive_duplicate_on_same_element_is_dropped():
    steps = [step(1, "click", index=3, el_hash=42), step(2, "click", index=3, el_hash=42)]
    assert kept_types(steps) == ["click"]


def test_same_element_twice_non_consecutive_is_kept():
    steps = [
        step(1, "click", index=3, el_hash=42),
        step(2, "input", index=4, el_hash=43),
        step(3, "click", index=3, el_hash=42),
    ]
    assert kept_types(steps) == ["click", "input", "click"]


def test_navigation_is_kept():
    steps = [step(1, "navigate"), step(2, "click", index=3, el_hash=1)]
    assert kept_types(steps) == ["navigate", "click"]


def test_bookkeeping_actions_are_dropped():
    steps = [
        step(1, "screenshot"),
        step(2, "wait"),
        step(3, "click", index=3, el_hash=1),
    ]
    assert kept_types(steps) == ["click"]


def test_decisions_explain_every_step():
    steps = [step(1, "scroll", hashes=[77]), step(2, "click", index=5, el_hash=77)]
    _, decisions = prune(steps)
    assert len(decisions) == 2
    assert all(d.reason for d in decisions)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_prune.py -v
```

Expected: FAIL — `No module named 'optexity.replay_cache.prune'`

- [ ] **Step 3: Write the pruner**

Create `optexity/replay_cache/prune.py`:

```python
"""Drop exploration noise from a recorded trace.

Conservative by construction: pruning a needed step breaks replay silently,
while keeping a redundant one costs milliseconds. The rules below generate a
candidate sequence; the verification replay in the emitter decides whether it
was right.
"""

from pydantic import BaseModel

from optexity.replay_cache.records import StepRecord

# Actions that target a DOM element by index. Everything else is navigation or
# bookkeeping. Taken from browser-use's registered action set.
ELEMENT_ACTIONS = {"click", "input", "upload_file", "dropdown_options", "select_dropdown"}

# Bookkeeping: no browser side effect worth replaying.
BOOKKEEPING_ACTIONS = {
    "done", "wait", "screenshot", "extract", "find_text",
    "write_file", "replace_file", "read_file", "evaluate",
}

# Actions that change state before they can fail. A failed one may have left
# the page altered, so it is replayed rather than assumed to be a no-op.
NON_IDEMPOTENT_ACTIONS = {"check", "uncheck", "input", "select_dropdown"}

# Navigation always matters: it decides which page later steps run against.
NAVIGATION_ACTIONS = {"navigate", "search", "go_back", "switch", "close"}


class PruneDecision(BaseModel):
    step: int
    action_type: str
    kept: bool
    reason: str


def _next_element_hash(steps: list[StepRecord], after: int) -> int | None:
    """The element hash of the next element-targeting step after index `after`."""
    for s in steps[after + 1:]:
        if s.action.type in ELEMENT_ACTIONS and s.element and s.element.element_hash is not None:
            return s.element.element_hash
    return None


def prune(steps: list[StepRecord]) -> tuple[list[StepRecord], list[PruneDecision]]:
    kept: list[StepRecord] = []
    decisions: list[PruneDecision] = []

    def decide(step: StepRecord, keep: bool, reason: str):
        decisions.append(
            PruneDecision(step=step.step, action_type=step.action.type, kept=keep, reason=reason)
        )
        if keep:
            kept.append(step)

    for i, s in enumerate(steps):
        kind = s.action.type

        if kind in BOOKKEEPING_ACTIONS:
            decide(s, False, "bookkeeping action with no replayable browser effect")
            continue

        if kind in NAVIGATION_ACTIONS:
            decide(s, True, "navigation decides which page later steps run against")
            continue

        if kind == "scroll":
            target = _next_element_hash(steps, i)
            if target is not None and target in s.selector_map_hashes:
                decide(s, False, "next target already existed; replay scrolls into view itself")
            else:
                decide(s, True, "next target absent before this scroll; scroll may have created it")
            continue

        if not s.success:
            if kind in NON_IDEMPOTENT_ACTIONS:
                decide(s, True, "failed but not idempotent; may have changed state before failing")
            else:
                decide(s, False, "action failed and is idempotent")
            continue

        if kept:
            prev = kept[-1]
            same_kind = prev.action.type == kind
            prev_hash = prev.element.element_hash if prev.element else None
            this_hash = s.element.element_hash if s.element else None
            if same_kind and prev_hash is not None and prev_hash == this_hash:
                decide(s, False, "consecutive duplicate on the same element")
                continue

        decide(s, True, "kept")

    return kept, decisions
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_prune.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
git add optexity/replay_cache/prune.py tests/replay_cache/test_prune.py
git commit -m "feat(replay-cache): prune exploration noise from a recorded trace

A scroll is dropped only when the next target already existed before it;
otherwise the scroll may be what caused the element to exist, which is the
normal case on lazy-loaded and virtualised pages.

Failed check/uncheck/input steps are kept: check_locator calls uncheck()
then check(), and browser-use clears a field before typing, so a failure
can leave state behind."
```

---

### Task 6: The emitter

**Files:**
- Create: `optexity/replay_cache/emit.py`
- Test: `tests/replay_cache/test_emit.py`

**Interfaces:**
- Consumes: `StepRecord`, `to_scorer_element`, `CachedStep`, `CacheEntry`, `prune`, `cache_key`, `origin_of`.
- Produces:
  - `build_entry(key: str, endpoint_name: str, url: str, steps: list[StepRecord]) -> CacheEntry`
  - `to_automation(entry: CacheEntry, url: str, input_parameters: dict) -> dict`

- [ ] **Step 1: Write the failing emitter test**

Create `tests/replay_cache/test_emit.py`:

```python
import json

from optexity.replay_cache.emit import build_entry, to_automation
from optexity.replay_cache.records import ActionRecord, ElementSignals, StepRecord

SEARCH_INPUT = StepRecord(
    step=1,
    url="https://www.roboform.com/filling-test-all-fields",
    action=ActionRecord(type="input", index=6, params={"text": "myname"}),
    element=ElementSignals(
        tag_name="input",
        attributes={"id": "02frstname", "name": "02frstname"},
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


def test_entry_uses_the_highest_scoring_candidate_as_the_command():
    entry = build_entry("k1", "ep1", SEARCH_INPUT.url, [SEARCH_INPUT])
    assert entry.steps[0].command == 'locator("#02frstname")'


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
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_emit.py -v
```

Expected: FAIL — `No module named 'optexity.replay_cache.emit'`

- [ ] **Step 3: Write the emitter**

Create `optexity/replay_cache/emit.py`:

```python
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
ACTION_TO_INTERACTION = {
    "click": "click_element",
    "input": "input_text",
    "select_dropdown": "select_option",
    "upload_file": "upload_file",
}

_METHOD_FOR = {
    "click": ".click()",
    "input": ".fill()",
    "select_dropdown": ".select_option()",
    "upload_file": ".set_input_files()",
}


def _strip_page_prefix(locator: str) -> str:
    """Candidates come back as `page.locator(...)…`; commands are `locator(...)`."""
    return locator[len("page."):] if locator.startswith("page.") else locator


def _command_from(locator_expression: str) -> str:
    """Drop the trailing method call, keeping just the locator expression."""
    for method in (".click()", ".fill()", ".select_option()", ".set_input_files()"):
        if locator_expression.endswith(method):
            locator_expression = locator_expression[: -len(method)]
            break
    return _strip_page_prefix(locator_expression)


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
            continue
        cached.append(
            CachedStep(
                action_type=s.action.type,
                command=_command_from(candidates[0]["locator"]),
                candidates=candidates,
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_emit.py -v
```

Expected: 6 passed. If `test_automation_validates_against_the_real_schema` fails, read the pydantic error and fix the node shape in `to_automation` — the schema is the authority, not this plan.

- [ ] **Step 5: Commit**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
git add optexity/replay_cache/emit.py tests/replay_cache/test_emit.py
git commit -m "feat(replay-cache): emit a cache entry and a deterministic automation

Commands carry no .first: it rewrites the selector to >> nth=0 and
exempts the locator from Playwright strict mode, which is the check that
stops an ambiguous locator acting on the wrong element.

The generated automation is validated against the real Automation model
in tests, and generated commands are tested against adversarial page text
because get_locator_from_command eval()s them."
```

---

### Task 7: The replay gate

**Files:**
- Create: `optexity/replay_cache/gate.py`
- Modify: `optexity/inference/core/interaction/handle_command.py`
- Test: `tests/replay_cache/test_gate.py`

**Interfaces:**
- Consumes: `CachedStep` from Task 2.
- Produces:
  - `GateOutcome` model: `resolved: bool`, `command: str | None`, `match_count: int`, `reason: str`, `candidate_rank: int`
  - `async resolve_unique(browser, primary_command: str, candidates: list[dict]) -> GateOutcome`
  - `ReplayCounters` with `hits: int`, `chain_recoveries: int`, `escalations: int` and a `summary() -> str`

- [ ] **Step 1: Write the failing gate test**

Create `tests/replay_cache/test_gate.py`:

```python
import pytest

from optexity.replay_cache.gate import ReplayCounters, resolve_unique


class FakeLocator:
    def __init__(self, n):
        self._n = n

    async def count(self):
        return self._n


class FakeBrowser:
    """Maps command string -> number of elements it matches."""

    def __init__(self, counts):
        self.counts = counts
        self.asked = []

    async def get_locator_from_command(self, command):
        self.asked.append(command)
        if command not in self.counts:
            return None
        return FakeLocator(self.counts[command])


CANDIDATES = [
    {"locator": 'page.locator("#a").click()', "kind": "id", "score": 92},
    {"locator": 'page.get_by_role("link", name="Go").click()', "kind": "role+name", "score": 72},
    {"locator": 'page.locator("xpath=/html/body/a").click()', "kind": "xpath", "score": 10},
]


async def test_unique_match_resolves_on_the_primary():
    browser = FakeBrowser({'locator("#a")': 1})
    out = await resolve_unique(browser, 'locator("#a")', CANDIDATES)
    assert out.resolved is True
    assert out.candidate_rank == 0
    assert out.match_count == 1


async def test_ambiguous_primary_falls_through_to_the_next_candidate():
    browser = FakeBrowser({
        'locator("#a")': 6,
        'get_by_role("link", name="Go")': 1,
    })
    out = await resolve_unique(browser, 'locator("#a")', CANDIDATES)
    assert out.resolved is True
    assert out.candidate_rank == 1
    assert "ambiguous" in out.reason


async def test_missing_primary_falls_through():
    browser = FakeBrowser({
        'locator("#a")': 0,
        'get_by_role("link", name="Go")': 1,
    })
    out = await resolve_unique(browser, 'locator("#a")', CANDIDATES)
    assert out.resolved is True
    assert out.candidate_rank == 1


async def test_exhausted_chain_does_not_resolve():
    browser = FakeBrowser({
        'locator("#a")': 0,
        'get_by_role("link", name="Go")': 0,
        'locator("xpath=/html/body/a")': 3,
    })
    out = await resolve_unique(browser, 'locator("#a")', CANDIDATES)
    assert out.resolved is False
    assert "exhausted" in out.reason


async def test_counters_distinguish_hit_from_recovery_from_escalation():
    c = ReplayCounters()
    c.record(type("O", (), {"resolved": True, "candidate_rank": 0})())
    c.record(type("O", (), {"resolved": True, "candidate_rank": 2})())
    c.record(type("O", (), {"resolved": False, "candidate_rank": -1})())
    assert c.hits == 1
    assert c.chain_recoveries == 1
    assert c.escalations == 1
    assert "1 hit" in c.summary()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_gate.py -v
```

Expected: FAIL — `No module named 'optexity.replay_cache.gate'`

- [ ] **Step 3: Write the gate**

Create `optexity/replay_cache/gate.py`:

```python
"""Uniqueness check for a cached locator, plus the fallback chain.

Playwright already refuses to act on an ambiguous bare locator: click, fill and
friends pass strict=True. But handle_command.py catches that in a blanket
`except Exception`, flattens it into a generic error string, and falls through
to the LLM index path — so a fully degraded cache still succeeds while paying
full token cost.

This gate makes the distinction explicit and countable: resolved on the primary
command, recovered from the candidate chain, or escalated.
"""

import logging

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_TRAILING_METHODS = (".click()", ".fill()", ".select_option()", ".set_input_files()")


class GateOutcome(BaseModel):
    resolved: bool
    command: str | None
    match_count: int
    reason: str
    candidate_rank: int


def _to_command(locator_expression: str) -> str:
    for method in _TRAILING_METHODS:
        if locator_expression.endswith(method):
            locator_expression = locator_expression[: -len(method)]
            break
    if locator_expression.startswith("page."):
        locator_expression = locator_expression[len("page."):]
    return locator_expression


async def _count(browser, command: str) -> int:
    try:
        locator = await browser.get_locator_from_command(command)
        if locator is None:
            return 0
        return await locator.count()
    except Exception as e:
        logger.debug(f"Replay gate could not resolve {command!r}: {type(e).__name__}: {e}")
        return -1


async def resolve_unique(browser, primary_command: str, candidates: list[dict]) -> GateOutcome:
    """First command in the chain that matches exactly one element."""
    chain = [primary_command] + [
        _to_command(c["locator"]) for c in (candidates or [])
    ]
    seen: set[str] = set()
    ordered = [c for c in chain if not (c in seen or seen.add(c))]

    reasons: list[str] = []
    for rank, command in enumerate(ordered):
        n = await _count(browser, command)
        if n == 1:
            reason = "resolved on primary" if rank == 0 else (
                f"recovered at rank {rank} after {'; '.join(reasons)}"
            )
            return GateOutcome(
                resolved=True, command=command, match_count=1,
                reason=reason, candidate_rank=rank,
            )
        if n == 0:
            reasons.append(f"{command} matched nothing")
        elif n < 0:
            reasons.append(f"{command} failed to resolve")
        else:
            reasons.append(f"{command} ambiguous ({n} matches)")

    return GateOutcome(
        resolved=False, command=None, match_count=0,
        reason=f"chain exhausted: {'; '.join(reasons)}",
        candidate_rank=-1,
    )


class ReplayCounters:
    """Without these, 'the cached run worked' is unfalsifiable."""

    def __init__(self):
        self.hits = 0
        self.chain_recoveries = 0
        self.escalations = 0

    def record(self, outcome) -> None:
        if not outcome.resolved:
            self.escalations += 1
        elif outcome.candidate_rank == 0:
            self.hits += 1
        else:
            self.chain_recoveries += 1

    @property
    def total(self) -> int:
        return self.hits + self.chain_recoveries + self.escalations

    def summary(self) -> str:
        return (
            f"replay cache: {self.hits} hit, "
            f"{self.chain_recoveries} chain recovery, "
            f"{self.escalations} escalated to LLM "
            f"({self.total} cached steps)"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_gate.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Wire the gate into the command path**

In `optexity/inference/core/interaction/handle_command.py`, inside `command_based_action_with_retry`, replace the existing resolution at the top of the retry loop:

```python
            locator = await browser.get_locator_from_command(action.command)
            if locator is None:
                continue
```

with a uniqueness-checked resolution:

```python
            outcome = await resolve_unique(
                browser, action.command, getattr(action, "locator_candidates", None) or []
            )
            if not outcome.resolved:
                logger.warning(
                    f"Replay gate declined {action.__class__.__name__}: {outcome.reason}"
                )
                last_error = f"error: {outcome.reason}"
                continue
            if outcome.candidate_rank > 0:
                logger.info(
                    f"Replay gate recovered via candidate rank {outcome.candidate_rank}: "
                    f"{outcome.command}"
                )
            locator = await browser.get_locator_from_command(outcome.command)
            if locator is None:
                continue
```

Add the import at the top of the file:

```python
from optexity.replay_cache.gate import resolve_unique
```

- [ ] **Step 6: Confirm nothing regressed on a known-good automation**

Restart the inference server and re-run the stockanalysis endpoint used earlier:

```bash
cd /Users/kamal/Desktop/optexity
curl -X POST http://localhost:9000/inference -H "Content-Type: application/json" \
  -d '{"endpoint_name":"extract_price_stockanalysis-a16d42fc","input_parameters":{"stock_ticker":["NVDA"]},"unique_parameter_names":[]}'
```

Expected: still `HTTP 202`, and the run still extracts a price. The gate must be invisible on an automation whose locators already resolve uniquely.

- [ ] **Step 7: Commit**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
git add optexity/replay_cache/gate.py tests/replay_cache/test_gate.py optexity/inference/core/interaction/handle_command.py
git commit -m "feat(replay-cache): uniqueness gate and fallback chain on cached steps

Playwright already raises on an ambiguous bare locator, but the blanket
except in command_based_action_with_retry flattens that into a generic
error string and the step silently becomes an LLM call. The gate walks
the stored candidate chain instead, and counts hits separately from
recoveries and escalations."
```

---

### Task 8: Metrics and end-to-end verification

**Files:**
- Create: `optexity/replay_cache/metrics.py`
- Test: `tests/replay_cache/test_metrics.py`
- Create: `test_automation_cached.json` (generated, committed as evidence)

**Interfaces:**
- Consumes: `ReplayCounters` from Task 7.
- Produces: `RunMetrics` model with `wall_clock_s: float`, `llm_calls: int`, `prompt_tokens: int`, `completion_tokens: int`, `steps: int`, and `from_history(history, wall_clock_s) -> RunMetrics`; `compare(agentic: RunMetrics, cached: RunMetrics) -> str`.

- [ ] **Step 1: Write the failing metrics test**

Create `tests/replay_cache/test_metrics.py`:

```python
from types import SimpleNamespace

from optexity.replay_cache.metrics import RunMetrics, compare, from_history


def test_reads_usage_when_present():
    history = SimpleNamespace(
        usage=SimpleNamespace(
            total_prompt_tokens=1200, total_completion_tokens=300, total_tokens=1500
        ),
        history=[1, 2, 3],
    )
    m = from_history(history, wall_clock_s=12.5)
    assert m.prompt_tokens == 1200
    assert m.completion_tokens == 300
    assert m.steps == 3
    assert m.wall_clock_s == 12.5


def test_tolerates_usage_being_none():
    """history.usage is never assigned if agent.run() raised."""
    history = SimpleNamespace(usage=None, history=[1])
    m = from_history(history, wall_clock_s=1.0)
    assert m.prompt_tokens == 0
    assert m.completion_tokens == 0


def test_tolerates_history_being_none():
    m = from_history(None, wall_clock_s=1.0)
    assert m.steps == 0
    assert m.wall_clock_s == 1.0


def test_compare_reports_both_directions():
    agentic = RunMetrics(wall_clock_s=20.0, llm_calls=6, prompt_tokens=9000,
                         completion_tokens=900, steps=6)
    cached = RunMetrics(wall_clock_s=2.0, llm_calls=0, prompt_tokens=0,
                        completion_tokens=0, steps=4)
    text = compare(agentic, cached)
    assert "20.00s" in text and "2.00s" in text
    assert "10.0x" in text
    assert "9900" in text


def test_compare_does_not_divide_by_zero():
    zero = RunMetrics(wall_clock_s=0.0, llm_calls=0, prompt_tokens=0,
                      completion_tokens=0, steps=0)
    assert compare(zero, zero)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache/test_metrics.py -v
```

Expected: FAIL — `No module named 'optexity.replay_cache.metrics'`

- [ ] **Step 3: Write the metrics module**

Create `optexity/replay_cache/metrics.py`:

```python
"""Before-and-after numbers for an agentic run versus its cached replay.

Deliberately defensive about usage: StepMetadata carries no token fields
despite its docstring, and AgentHistoryList.usage is only assigned on normal
completion and KeyboardInterrupt — it stays None if the run raised.
"""

from pydantic import BaseModel


class RunMetrics(BaseModel):
    wall_clock_s: float = 0.0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    steps: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def from_history(history, wall_clock_s: float) -> RunMetrics:
    if history is None:
        return RunMetrics(wall_clock_s=wall_clock_s)
    steps = len(getattr(history, "history", None) or [])
    usage = getattr(history, "usage", None)
    return RunMetrics(
        wall_clock_s=wall_clock_s,
        llm_calls=steps,
        prompt_tokens=getattr(usage, "total_prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "total_completion_tokens", 0) or 0,
        steps=steps,
    )


def _ratio(before: float, after: float) -> str:
    if after <= 0:
        return "n/a"
    return f"{before / after:.1f}x"


def compare(agentic: RunMetrics, cached: RunMetrics) -> str:
    return (
        "replay cache comparison\n"
        f"  wall clock : {agentic.wall_clock_s:.2f}s -> {cached.wall_clock_s:.2f}s "
        f"({_ratio(agentic.wall_clock_s, cached.wall_clock_s)} faster)\n"
        f"  llm calls  : {agentic.llm_calls} -> {cached.llm_calls}\n"
        f"  tokens     : {agentic.total_tokens} -> {cached.total_tokens} "
        f"(saved {agentic.total_tokens - cached.total_tokens})\n"
        f"  steps      : {agentic.steps} -> {cached.steps}"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
ENV_PATH=/Users/kamal/Desktop/optexity/.env /Users/kamal/Desktop/optexity/.venv/bin/pytest tests/replay_cache -v
```

Expected: all tests pass (39 across the six files).

- [ ] **Step 5: Run the full pipeline on roboform**

Put the spec's starting automation in `test_automation.json` (with `OPTEXITY_LOCAL_AUTOMATION_OVERRIDE=1` set on the server), run the agentic version, then build the cached automation from the trace:

```bash
cd /Users/kamal/Desktop/optexity
ENV_PATH=$PWD/.env .venv/bin/python -c "
import glob, json
from optexity.replay_cache.emit import build_entry, to_automation
from optexity.replay_cache.key import cache_key
from optexity.replay_cache.records import StepRecord
from optexity.replay_cache.store import FileCacheStore
from pathlib import Path

trace = sorted(glob.glob('**/replay_cache_trace.jsonl', recursive=True))[-1]
steps = [StepRecord.model_validate_json(l) for l in open(trace) if l.strip()]
url = 'https://www.roboform.com/filling-test-all-fields'
task = 'fill the full name as myname, address line one as xyz and line 2 as abc, city as SF'
key = cache_key('roboform-test', url, task, {})
entry = build_entry(key, 'roboform-test', url, steps)
FileCacheStore(Path('.replay_cache')).put(key, entry)
doc = to_automation(entry, url, {})
Path('test_automation_cached.json').write_text(json.dumps(doc, indent=2))
print('cached steps:', len(entry.steps))
print('wrote test_automation_cached.json')
"
```

Expected: a non-empty `test_automation_cached.json` whose commands are real locators from the run, not placeholders.

- [ ] **Step 6: Verify the cached automation replays**

Copy `test_automation_cached.json` over `test_automation.json`, restart the server (again with `OPTEXITY_LOCAL_AUTOMATION_OVERRIDE=1`), fire the same endpoint, and confirm the form is filled with no agentic node. Record the wall clock from both runs and produce the comparison:

```bash
cd /Users/kamal/Desktop/optexity
ENV_PATH=$PWD/.env .venv/bin/python -c "
from optexity.replay_cache.metrics import RunMetrics, compare
agentic = RunMetrics(wall_clock_s=<measured>, llm_calls=<measured>, prompt_tokens=<measured>, completion_tokens=<measured>, steps=<measured>)
cached  = RunMetrics(wall_clock_s=<measured>, llm_calls=0, prompt_tokens=0, completion_tokens=0, steps=<measured>)
print(compare(agentic, cached))
"
```

Expected: cached wall clock materially lower, `llm calls: N -> 0`.

- [ ] **Step 7: Repeat on a second, multi-step site**

Pick a site whose workflow spans pages — navigate, click something that changes the page, then act on the new page. No captcha. Repeat steps 5 and 6 and record the same comparison.

- [ ] **Step 8: Commit**

```bash
cd /Users/kamal/Desktop/optexity/optexity-fork
git add optexity/replay_cache/metrics.py tests/replay_cache/test_metrics.py
git commit -m "feat(replay-cache): before/after metrics for agentic vs cached runs

Defensive about usage: StepMetadata carries no token fields despite its
docstring, and history.usage stays None when agent.run() raised."
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| §3 cache identity (B′) | Task 2 |
| §4.2 lossy conversion | Task 3 (recorder captures ax/text) |
| §4.3 the split | Tasks 3, 4 |
| §4.4 adapter contract | Task 1 |
| §5 record format | Tasks 1, 3 |
| §6 pruning | Task 5 |
| §7.3 the gate | Task 7 |
| §7.4 no `.first` | Task 6 (emit), Task 7 (gate) |
| §7.5 eval hazard | Task 6 |
| §8 metrics | Task 8 |
| §9 store seam | Task 2 |
| §10 test plan items 1–7 | Tasks 1, 5, 6, 7, 8 |

**Known gap carried deliberately:** the gate in Task 7 reads `action.locator_candidates`, an attribute that does not yet exist on the action schema. The fallback chain therefore degrades to primary-command-only until the cached candidates are threaded onto the action. Task 7's `getattr(..., None) or []` makes that safe rather than broken. Threading it through `InteractionAction` is the first follow-up after this plan lands, and is why Task 7's step 6 only asserts "no regression" rather than "chain recovery observed."

**Type consistency:** `cache_key` / `origin_of` (Task 2) used in Task 6; `CachedStep.candidates` written in Task 6 and read in Task 7; `GateOutcome.candidate_rank` set in Task 7 and consumed by `ReplayCounters.record` in the same task; `StepRecord` fields written as dicts by Task 3 and parsed by Task 1's model — the recorder's key names match the model's field names exactly.
