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
