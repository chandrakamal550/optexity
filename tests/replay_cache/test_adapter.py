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
