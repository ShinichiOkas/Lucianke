# BUG-0061 T-19 `get_project_db_schema` が公開ビューを 2 つ（files・workspaces）しか返さず、契約テストが落ちる

見たことだけを書く（調査していない）。2026-10-29、引き継ぎ時の全体テストで観測。

## 症状
`uv run pytest -q`（main `7aa42e2` + 作業ツリーの未コミット変更）で次の 2 件が落ちる。ほかは 3781 passed・22 skipped。

```
FAILED tests/contract_fixtures/test_fx_db_T19_T20.py::test_t19_postcondition_1_2_3_4_5_schema[F-PY]
FAILED tests/contract_fixtures/test_fx_db_T19_T20.py::test_t19_postcondition_1_2_3_4_5_schema[F-TS]
```

```
>       assert {t["name"] for t in tables} == set(t19.PUBLIC_VIEWS) and len(tables) == 10  # 事後条件 1
E       AssertionError: assert ({'files', 'workspaces'} == {'contracts',...commits', ...}
E         Extra items in the right set:
E         'diagnostics'
E         'git_co_changes'
E         'relations'
E         'git_symbol_changes'
E         'contracts'...
```

- ツールが返した `data.tables` の名前は `{'files', 'workspaces'}` の 2 つだけ。`t19.PUBLIC_VIEWS` は 10 個。
- 同じファイルの T-20 のテスト 2 件は通る（`4 passed`）。

## 原因ではないと確かめたこと
`src/` とテストの未コミット変更を `git stash` して HEAD（`7aa42e2`）のまま同じ 2 件を走らせても、同じく 2 failed（`4 passed, 2 failed in 32.96s`）。この失敗は作業ツリーの変更が原因ではない（`separate-preexisting-breakage-from-own-regression`）。

## 再現
`uv run python bench/fetch_fixtures.py` 済み（`bench/.cache/httpx`・`bench/.cache/ky` がある）状態で:

```
uv run pytest tests/contract_fixtures/test_fx_db_T19_T20.py -q
```

## 手掛かり（未調査）
- 失敗は事後条件 1（ビューの一覧）だけ。列・行数・coverage の比較には到達していない。
- `_read_schema` は `_wanted(args)` の名前をそのまま並べるので、2 件だけになるのは上流（`data` の組み立て、または `_wanted` に渡る前）で絞られている可能性がある。

## 追試（2026-10-29、別セッション・新 .venv）
HEAD `9839053`（HANDOFF を含むコミット）で、`uv sync --extra langs` と `uv run --no-project python bench/fetch_fixtures.py`（`bench/.cache/httpx`・`bench/.cache/ky` 取得）を済ませた状態で再現した。

- `uv run pytest tests/contract_fixtures/test_fx_db_T19_T20.py -q` → `2 failed, 4 passed in 29.53s`。落ちるのは `[F-PY]`・`[F-TS]` の T-19#1 のまま。
- フィクスチャ未取得だと同じ 2 件は `6 skipped`（＝検査されない）。langs extra が無いと `tests/unit/test_syntax_boundaries.py` の `import tree_sitter_rust` で収集エラーになり、全体テストに到達しない。
- T-19 が返した `data.tables` の名前は今回も `{'files', 'workspaces'}` の 2 つだけ。`t19.PUBLIC_VIEWS`（10 個）には `contracts`・`symbols`・`relations`・`diagnostics`・`commits`・`git_symbol_changes` などが含まれる。

## 仕様書
§15.2 M1-1、§11 の T-19。
