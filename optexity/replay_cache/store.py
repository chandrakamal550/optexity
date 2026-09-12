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
    # Empty for non-element steps (navigation), which target a URL, not a node.
    command: str = ""
    # Full ranked candidate list, best first. The replay gate walks this when
    # the primary command fails to resolve uniquely.
    candidates: list[dict] = Field(default_factory=list)
    input_text: str | None = None
    prompt_instructions: str = ""
    # Navigation target. Only set for action_type == "navigate".
    url: str | None = None


class EmitDecision(BaseModel):
    """Why the emitter kept or dropped one pruned step.

    The pruner already returns its decisions; the emitter used to drop steps
    silently, which is how mid-run navigation disappeared from every emitted
    automation without leaving a trace. Emitted decisions are persisted on the
    entry so a dropped step is visible in the artifact, not only in a log line
    nobody kept.
    """

    step: int
    action_type: str
    emitted: bool
    reason: str


class CacheEntry(BaseModel):
    key: str
    endpoint_name: str
    origin: str
    created_at: str
    steps: list[CachedStep] = Field(default_factory=list)
    # Steps the pruner kept but the emitter could not express as an optexity
    # action. Never empty-by-omission: if this list has entries, the emitted
    # automation is knowingly incomplete.
    skipped: list[EmitDecision] = Field(default_factory=list)


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
        # get() looks the entry up by filename and never re-checks entry.key,
        # so a mismatch here writes an entry that is returned for a key it was
        # not built for — a cache that lies, discovered by a customer rather
        # than by a test. Fail loudly at write time instead.
        if entry.key != key:
            raise ValueError(
                f"Refusing to store entry keyed {entry.key!r} under {key!r}"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(
            json.dumps(entry.model_dump(), indent=2), encoding="utf-8"
        )
