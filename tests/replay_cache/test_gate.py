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
    assert "1 resolved on primary" in c.summary()
