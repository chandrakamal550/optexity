from optexity.replay_cache.counters import ReplayCounters
from optexity.schema.memory import Memory


def test_memory_exposes_replay_cache_with_zeroed_counters_by_default():
    memory = Memory(unique_child_arn="test")
    assert memory.replay_cache.hits == 0
    assert memory.replay_cache.chain_recoveries == 0
    assert memory.replay_cache.escalations == 0
    assert memory.replay_cache.total == 0


def test_summary_renders_expected_string_for_mixed_outcomes():
    c = ReplayCounters()
    c.record(type("O", (), {"resolved": True, "candidate_rank": 0})())
    c.record(type("O", (), {"resolved": True, "candidate_rank": 0})())
    c.record(type("O", (), {"resolved": True, "candidate_rank": 2})())
    c.record(type("O", (), {"resolved": False, "candidate_rank": -1})())
    assert c.summary() == (
        "replay gate: 2 resolved on primary, 1 recovered via candidate chain, "
        "1 escalated to LLM (4 command steps gated)"
    )


def test_summary_does_not_claim_cached_steps():
    """The gate runs on every command action, including automations that were
    hand-written and never touched a cache entry. Claiming 'N cached steps'
    there is a false statement about the feature's own instrument."""
    c = ReplayCounters()
    c.record(type("O", (), {"resolved": True, "candidate_rank": 0})())
    assert "cached step" not in c.summary()
