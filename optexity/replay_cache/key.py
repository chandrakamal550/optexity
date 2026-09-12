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
