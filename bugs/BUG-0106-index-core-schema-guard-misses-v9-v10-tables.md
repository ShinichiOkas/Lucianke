# BUG-0106 統合の全体テストで test_index_core の 4 件が落ちる（スキーマ v9・v10 の表と版の数を見張りが知らない）

- 報告: 2026-10-31 / 見た人: 親（統合の全体テスト。`main` = `48a1704`）

## 症状

`uv run pytest -q -p no:cacheprovider -rfs` → `4 failed, 4109 passed, 30 skipped in 884.21s`。落ちたのは次の 4 件だけ:

```
FAILED tests/unit/test_index_core.py::test_ensure_schema_creates_public_internal_tables_and_meta
FAILED tests/unit/test_index_core.py::test_schema_version_is_read_from_schema_sql
FAILED tests/unit/test_index_core.py::test_ensure_schema_rebuilds_on_other_version[0]
FAILED tests/unit/test_index_core.py::test_ensure_schema_rebuilds_on_other_version[99]
E       Extra items in the left set: '_attr_defs' '_var_types' '_reexport_deps' '_reexports'
E       assert 10 == 8
```

## 原因・直し方・確認（2026-10-31）

- 原因: 並行の直す役が BUG-0093/0094/0095（`0a3a505`、v9: `_attr_defs`・`_var_types`）と BUG-0100/0101（`6da931d`、v10: `_reexports`・`_reexport_deps`）でスキーマを上げたとき、`tests/unit/test_index_schema.py` の許可リストは直したが、`tests/unit/test_index_core.py` の `INTERNAL_TABLES` と版の数（8 のまま）を直さなかった。どちらの役も「関係するテスト」にこのファイルを入れていなかった（統合の全体テストで初めて見えた）。
- 直し方: `INTERNAL_TABLES` に 4 表を足し、版の期待を 10 に。v1→v2・v2→v3 の移行の試験が「古い DB」を作るときに、v9・v10 で足した表も落とす（古い形を正しく再現する）。
- 確認: `uv run pytest -q tests/unit/test_index_core.py tests/unit/test_index_schema.py` → 50 passed。`uv run ruff check` clean。
- 再発の防止: スキーマを上げる直す役への指示に「`test_index_core.py` と `test_index_schema.py` の両方」を入れる（親の Skill `parallel-fixers-run-targeted-tests-parent-runs-one-full-suite` の「番人は直す役も流す」に該当）。
