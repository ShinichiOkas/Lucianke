"""core.limits のテスト。仕様書 §12.3・§12.8 などの値が定数表から変わっていないことを守る。"""

from __future__ import annotations

from lucianke.core import limits

EXPECTED: dict[str, object] = {
    "REGISTRY_MIN_DEFAULT_MAX_TOKENS": 1,
    "BUDGET_FLOOR_FOR_PAGING": 200,
    "BATCH_MAX_ITEMS": 20,
    "CANDIDATES_MAX": 10,
    "SNIPPET_MAX_CHARS": 200,
    "WATCH_STEP_MS": 10,  # S-06: step=10 が必須（既定の 50 では単発の p95 が 100 ms 前後）
    "WATCH_DEBOUNCE_MS": 1000,  # S-06: パスが増え続ける間にグループ化する最大時間
    "WATCH_BURST_HINT": 100,
    "WATCH_BURST_QUIET_S": 0.3,
    "WATCH_BURST_FILES": 500,
    "WATCH_EXPECT_TTL_S": 10.0,
    "MAX_FILE_BYTES": 5 * 1024 * 1024,
    "TREE_CACHE_MAX_FILES": 2000,
    "TREE_CACHE_MAX_BYTES": 512 * 1024 * 1024,
    "LSP_TIMEOUT_DEFINITION_MS": 3000,
    "LSP_TIMEOUT_REFERENCES_MS": 3000,
    "LSP_TIMEOUT_HOVER_MS": 3000,
    "LSP_TIMEOUT_CALL_HIERARCHY_MS": 5000,
    "LSP_TIMEOUT_CODE_ACTION_MS": 5000,
    "LSP_TIMEOUT_RENAME_MS": 10000,
    "LSP_RESTART_BACKOFF_S": (1, 2, 4),
    "LSP_RESTART_MAX": 3,
    "LSP_OPEN_DOCS_MAX": 200,
    "LSP_IDLE_SHUTDOWN_S": 1800,
    "EDIT_LOCK_WAIT_MS": 5000,
    "DIAGNOSTICS_WAIT_DEFAULT_MS": 2000,
    "DIAGNOSTICS_WAIT_MAX_MS": 10000,
    "DIRECT_REFERRERS_MAX": 20,
    "EDIT_MAX_FILES": 500,
    "NEW_CODE_MAX_BYTES": 1024 * 1024,
    "JOURNAL_KEEP": 50,
    "QUERY_TIMEOUT_DEFAULT_MS": 300,
    "QUERY_TIMEOUT_MAX_MS": 5000,
    "QUERY_ROWS_DEFAULT": 200,
    "QUERY_ROWS_MAX": 2000,
    "QUERY_LENGTH_MAX": 64 * 1024,
    "QUERY_CTE_DEPTH_MAX": 10000,
    "GREP_MATCHES_MAX": 10000,
    "REGEX_SCAN_MS": 200,
    "REGEX_PATTERN_MAX_CHARS": 1000,
    "SELECTOR_EVAL_MS": 1000,
    "DEGRADED_GREP_MS": 2000,
    "STRUCTURAL_SEARCH_MS": 5000,
    "INDEX_REFLECT_MS": 500,
    "READER_POOL_SIZE": 8,
    "MAX_CONCURRENT_READS": 16,
    "READ_REQUEST_TIMEOUT_MS": 10000,
    "EDIT_REQUEST_TIMEOUT_MS": 30000,
    "SHUTDOWN_GRACE_MS": 500,
    "CO_CHANGE_WINDOW": 500,
}


def test_values() -> None:
    for name, value in EXPECTED.items():
        assert getattr(limits, name) == value, name


def test_no_unlisted_constants() -> None:
    public = {n for n in vars(limits) if n.isupper()}
    assert public == set(EXPECTED), public ^ set(EXPECTED)


def test_internal_consistency() -> None:
    assert limits.QUERY_TIMEOUT_DEFAULT_MS <= limits.QUERY_TIMEOUT_MAX_MS
    assert limits.QUERY_ROWS_DEFAULT <= limits.QUERY_ROWS_MAX
    assert limits.DIAGNOSTICS_WAIT_DEFAULT_MS <= limits.DIAGNOSTICS_WAIT_MAX_MS
    assert len(limits.LSP_RESTART_BACKOFF_S) == limits.LSP_RESTART_MAX
    assert limits.READ_REQUEST_TIMEOUT_MS < limits.EDIT_REQUEST_TIMEOUT_MS
    assert limits.EDIT_LOCK_WAIT_MS < limits.EDIT_REQUEST_TIMEOUT_MS
