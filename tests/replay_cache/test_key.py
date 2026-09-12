from optexity.replay_cache.key import cache_key

TASK = "fill the full name as myname, address line one as xyz"


def test_same_inputs_produce_the_same_key():
    a = cache_key("ep1", "https://example.com/a", TASK, {"t": ["NVDA"]})
    b = cache_key("ep1", "https://example.com/a", TASK, {"t": ["NVDA"]})
    assert a == b


def test_different_input_values_produce_different_keys():
    """The decision that stops a cache hit returning the previous run's answer."""
    nvda = cache_key("ep1", "https://example.com/a", TASK, {"t": ["NVDA"]})
    aapl = cache_key("ep1", "https://example.com/a", TASK, {"t": ["AAPL"]})
    assert nvda != aapl


def test_key_ignores_path_and_query_but_not_origin():
    same = cache_key("ep1", "https://example.com/a?x=1", TASK, {})
    also = cache_key("ep1", "https://example.com/b", TASK, {})
    other = cache_key("ep1", "https://other.com/a", TASK, {})
    assert same == also
    assert same != other


def test_input_parameter_ordering_does_not_matter():
    a = cache_key("ep1", "https://example.com", TASK, {"a": ["1"], "b": ["2"]})
    b = cache_key("ep1", "https://example.com", TASK, {"b": ["2"], "a": ["1"]})
    assert a == b


def test_task_text_participates():
    a = cache_key("ep1", "https://example.com", "do X", {})
    b = cache_key("ep1", "https://example.com", "do Y", {})
    assert a != b
