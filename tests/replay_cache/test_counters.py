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
        "replay cache: 2 hit, 1 chain recovery, 1 escalated to LLM (4 cached steps)"
    )
