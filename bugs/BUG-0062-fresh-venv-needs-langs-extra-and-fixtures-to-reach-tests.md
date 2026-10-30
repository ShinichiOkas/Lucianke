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

## 原因・直し方・確認（2026-10-31）

### 原因
- (1) テストが無条件に import する文法（`tree_sitter_rust` など）が、利用者向けの optional extra `langs` にしか無かった。
  `uv run` が既定で同期するのは本体の依存と dev 組だけなので、新しい `.venv` では入らない。
- (2) `tests/contract_fixtures/conftest.py` の `rig` が、`bench/.cache/` に F-PY・F-TS が無いと理由つきで `skip` していた。
  取得は手作業（`bench/fetch_fixtures.py`）だけで、取らなければ「0 failed」のまま契約テストが検査されない。

### 直し方
- (1) `pyproject.toml` の `[dependency-groups].dev` に `lucianke[langs]`（自分自身の extra を参照）を足し、`uv lock` で更新した。
  - 採った理由: dev 組は `uv run` / `uv sync` の既定（default-groups の既定が `["dev"]`）なので、手順なしで入る。文法の一覧は
    `langs` の 1 か所のままで写しを作らない。配布物の既定依存は増やさない（候補 (D) は配布サイズの方針判断が要るので採らない）。
    候補 (B)（テスト側で rust を skip）は、skip を増やすだけで検査が減るので採らない。
  - `THIRD_PARTY_NOTICES.md` は dev 組を数えないので変わらない（`tests/unit/test_notices_up_to_date.py` で確認）。
- (2) `tests/contract_fixtures/conftest.py` にセッションに 1 回の `fetched_fixtures` を足し、`rig` がこれを使う。
  - 未取得（または SHA 違い）の F-PY・F-TS を `bench/fetch_fixtures.py` で取りにいく。**失敗したら `pytest.fail` で赤（エラー）**。
    同じ作業ツリーで pytest が同時に走っても二重に clone しないよう、`bench/.cache/.fetch.lock`（`O_EXCL`）で排他する。
  - オフライン用に `LUCIANKE_NO_FIXTURE_FETCH=1` で取得を止められる。そのときの skip 理由は
    `NOT CHECKED: ... (this skip is not a pass)` と明記する。
  - 採った理由: 候補 (C)（skip の理由を見せる）だけでは、全体サマリが green のまま検査していない状態が残る。自動取得なら
    手順を覚えていなくても本物の契約テストに届き、届かないときは赤で分かる。`-m "not slow"` や単体だけの実行では取得しない。
  - `bench/run_exit_criteria.py` は `exit_criteria.toml` の `network = "none"` を守るため、子の pytest に
    `LUCIANKE_NO_FIXTURE_FETCH=1` を渡す（従来どおり needs で blocked、片方だけ未取得なら NOT CHECKED の skip）。
  - CI（`.github/workflows/ci.yml` の `test` ジョブ）は `uv run pytest -q` の前に `bench/fetch_fixtures.py` の段を足した。
    **CI でも契約テスト（slow）が走るようになる**（従来は skip）。取得の失敗は試験の赤と別の段で見える。
  - `_rig.fixture_dir`: 取得が途中で失敗した残骸（`git init` 済み・コミット無し）を「is at HEAD」と表示していたので、
    `git rev-parse --verify -q HEAD` で判定し「incomplete clone」と出すようにした。
- `docs/HANDOFF.md` §0・§1・§4 と `ARCHITECTURE.md` §6 から手作業の 2 コマンドを外し、自動になったことと opt-out を書いた。

### 確認（このリポジトリの作業ツリー、Windows 11 / CPython 3.12.10 / uv 0.11.25）
- `.venv` と `bench/.cache/` が**両方無い**状態から
  `uv run pytest -q tests/unit/test_syntax_boundaries.py tests/contract_fixtures -q -rs`
  → `.venv` 新規作成（`tree-sitter-rust` などを含む 65 パッケージ）、収集エラー無し、httpx・ky を自動取得して実行。
  結果 `79 passed, 5 skipped, 1 failed`。
  - skip 5 件はすべて `no TypeScript LSP: F-TS exact postconditions unverified`（BUG-0057 の明示 skip。フィクスチャ未取得の skip は 0）。
  - failed 1 件は `test_t02_postcondition_1_2_5_skeleton_matches_the_source[F-PY]`（T-02 が 2 ファイル中 1 ファイルしか返さない）。
    ツールの出力の問題で、この直しとは別。2 回とも再現。既に **BUG-0065**（T-02 skeleton 系の 7 件）に載っている。
- `bench/.cache/` を外して `LUCIANKE_NO_FIXTURE_FETCH=1 uv run pytest tests/contract_fixtures/test_fx_db_T19_T20.py -q -rs`
  → `6 skipped`、理由はすべて `NOT CHECKED: ... (LUCIANKE_NO_FIXTURE_FETCH is set, ...; this skip is not a pass)`。取得もしない。
- `bench/.cache/` を外し、通信を止めて（`HTTPS_PROXY=http://127.0.0.1:9`）同じ試験 → `6 errors`。理由に未取得のキー・取得コマンド・
  git のエラー・opt-out の方法が出る。
- `bench/.cache/` が無い状態で `uv run pytest -q tests/unit/test_syntax_boundaries.py` → `41 passed`、取得は走らない。
- `uv run pytest -q tests/unit/test_notices_up_to_date.py` → `8 passed, 1 skipped`（skip は verify/xml extras が無い環境の
  本文完全一致。従来どおり。CI は `--all-extras`）。
- `uv run ruff check src tests` は clean（`origin/main` の `b1ce8ac` に載せ直した後。載せ直す前の `87d9451` では
  `src/lucianke/mcp/render/graph.py` の 2 件で落ちていたが、`b1ce8ac` が直している）。
