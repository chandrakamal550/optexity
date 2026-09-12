from optexity.replay_cache.store import CachedStep, CacheEntry, FileCacheStore

ENTRY = CacheEntry(
    key="abc123",
    endpoint_name="ep1",
    origin="https://example.com",
    created_at="2026-09-12T00:00:00Z",
    steps=[
        CachedStep(
            action_type="input_text",
            command='locator("#search-header")',
            candidates=[{"locator": 'page.locator("#search-header").fill()', "kind": "id", "score": 92}],
            input_text="NVDA",
            prompt_instructions="Enter the ticker in the search field",
        )
    ],
)


def test_put_then_get_round_trips(tmp_path):
    store = FileCacheStore(tmp_path)
    store.put("abc123", ENTRY)
    got = store.get("abc123")
    assert got is not None
    assert got.steps[0].command == 'locator("#search-header")'
    assert got.steps[0].input_text == "NVDA"


def test_get_returns_none_for_unknown_key(tmp_path):
    assert FileCacheStore(tmp_path).get("nope") is None


def test_get_returns_none_for_corrupt_entry(tmp_path):
    store = FileCacheStore(tmp_path)
    (tmp_path / "broken.json").write_text("{not json")
    assert store.get("broken") is None


def test_put_creates_the_root_directory(tmp_path):
    store = FileCacheStore(tmp_path / "nested" / "deeper")
    store.put("abc123", ENTRY)
    assert store.get("abc123") is not None
