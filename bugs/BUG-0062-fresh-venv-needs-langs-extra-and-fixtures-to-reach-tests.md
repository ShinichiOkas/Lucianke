# BUG-0062 新しい `.venv` では `uv sync --extra langs` と `fetch_fixtures.py` を踏まないと全体テストに到達できない（収集エラー・skip になり「2 failed」にすら届かない）

見たことだけを書く（調査していない）。2026-10-29、別 PC・別セッションで HANDOFF の手続き（§0）をそのまま流したときに観測。

## 症状

### (1) langs extra 無し → 収集エラーで全体テストに到達しない
`.venv` が無い状態で `uv run pytest -q` を流すと、`.venv` が新規作成（既定同期・langs extra なし）され、次の収集エラーで中断する。

```
ERROR collecting tests/unit/test_syntax_boundaries.py
E   ModuleNotFoundError: No module named 'tree_sitter_rust'
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 14.23s
```

- `tests/unit/test_syntax_boundaries.py:11-14` が `tree_sitter_javascript` / `tree_sitter_python` / `tree_sitter_rust` / `tree_sitter_typescript` を無条件 import している。
- `tree_sitter_rust` は `pyproject.toml` の `[project.optional-dependencies].langs`（33-41 行）側。既定の `uv run` には入らない（`uv.lock` も `marker == "extra == 'langs'"`）。
- 本番コード `src/lucianke/syntax/support.py` は rust/go/c などを遅延ロードし `build_registry` で `try/except ModuleNotFoundError` して飛ばすので、langs 無しでも import は壊れない。壊れるのは**テスト側の無条件 import** だけ。

`uv sync --extra langs` で解消する:
```
+ tree-sitter-c==0.24.2
+ tree-sitter-c-sharp==0.23.5
+ tree-sitter-cpp==0.23.4
+ tree-sitter-go==0.25.0
+ tree-sitter-rst==0.2.0
+ tree-sitter-rust==0.24.2
+ tree-sitter-sql==0.3.11
```

### (2) フィクスチャ未取得 → 契約テストが skip（「検査していない」のに 0 failed に見える）
langs extra を入れた後、`bench/.cache/` が無い状態で全体テストを流すと `3734 passed / 71 skipped / 0 failed` になり、**BUG-0061 の赤 2 件が skip に化けて見えなくなる**。

```
uv run pytest tests/contract_fixtures/test_fx_db_T19_T20.py -q
ssssss                                                                   [100%]
6 skipped in 1.44s
```

- `bench/.cache/` が無い（`ls: cannot access 'bench/.cache/': No such file or directory`）。
- `uv run --no-project python bench/fetch_fixtures.py --list` → `F-PY httpx ... missing` / `F-TS ky ... missing`。
- 取得後は同じ 2 件が `2 failed, 4 passed`（BUG-0061 が見えるようになる）。HANDOFF §4 にも「skip は pass ではない」とある。

## 影響
HANDOFF §0 の 3 コマンド（`git pull` / `uv run pytest -q` / 契約テスト単独）をそのまま踏むと、新しい `.venv` では「期待: 2 failed, 3781 passed, 22 skipped」に到達できない。収集エラーで止まるか、skip で赤が隠れる。引き継いだ別 PC が「赤が消えた＝直った」と誤認する危険がある。

## 直し方（候補・未着手）
次のいずれか、または複数。どれを採るかは方針判断。
- (A) HANDOFF §0 に `uv sync --extra langs` と `uv run --no-project python bench/fetch_fixtures.py` を 3 コマンドの前に明記する（最小）。
- (B) `tests/unit/test_syntax_boundaries.py` の `tree_sitter_rust` import を、本番 support.py と同じく「無ければ skip」にする（langs 無しでも収集が通る）。
- (C) 契約テストの skip 理由を可視化する（`bench/.cache/` が無いときに理由付き skip で、全体サマリに出す）。既に skip ではあるが「検査していない」と分かる形にする。
- (D) langs extra を既定 dependencies に昇格する（配布サイズとのトレードオフ。要方針判断）。

## 観測した環境
- HEAD `9839053`（当時）/ Windows 11 / CPython 3.12.10 / `uv` 管理の `.venv` 新規作成。
- 全体テスト: langs 同期後・フィクスチャ未取得で `3734 passed, 71 skipped in 482.18s`。
