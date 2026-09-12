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
