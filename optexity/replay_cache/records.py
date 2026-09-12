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
