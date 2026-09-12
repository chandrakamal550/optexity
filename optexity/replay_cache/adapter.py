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
