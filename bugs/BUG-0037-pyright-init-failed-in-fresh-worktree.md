# BUG-0037 使い捨て作業ツリー（origin/main b24f766）で pyright が init_failed のまま 3 回再起動して unavailable になる

- 報告: 2026-10-29 / 見た人: ドッグフーディング 2 周目（dogfood-2）
- 症状: push 済みの最新コミットの使い捨て作業ツリー（`git worktree add <temp> origin/main` → `uv sync --frozen`、サーバーは `<temp>/.venv/Scripts/python.exe -m lucianke`）で F-POLY のコピーを `set_roots` すると、Python の型 LSP（pyright）が毎回 `init_failed` になり、3 回再起動して `unavailable` に落ちる。lint の ruff は `ready` になる。以降 T-04・T-06・T-07・T-08 は `lsp_status=unavailable`・`confidence=structural` の縮退のまま（`exact` にならない）。
  - サーバーのログ（stderr）:
    - `WARNING lucianke.lsp.client: [pyright] LSP disconnected: server closed stdout (exit code None)`
    - `WARNING lucianke.lsp.pool: LSP python/pyright init_failed; restart 1/3 in 1s`（2/3 in 2s、3/3 in 4s と続く）
    - `ERROR lucianke.lsp.pool: LSP python/pyright: giving up after 3 restarts (init_failed)`
  - `configure_workspace status` の `lsp_daemons[]`: `{language: python, role: types, server: basedpyright, status: unavailable, pid: null, reason: init_failed}` と `{role: lint, server: ruff, status: ready, pid: <数値>}`。
  - 同じ呼び出しの `get_diagnostics` の `lsp_sources[].server` は `pyright`（T-00 の `lsp_daemons[].server` は `basedpyright`）。同じサーバーの名前が 2 つのツールで違う。
  - 作業ツリーの `.venv/Scripts/` には `basedpyright-langserver.exe` がある。PATH 上に `node.exe` もある。
- 再現手順:
  1. `git worktree add C:/Users/shini/AppData/Local/Temp/lucianke-dogfood2 origin/main`、その中で `uv sync --frozen`
  2. `bench/dogfood/run_dogfood2.py --repo <その作業ツリー> ro`（または `configure_workspace set_roots` で `tests/fixtures/poly` のコピーを登録し、`get_diagnostics` を 2 秒おきに 10 回呼ぶ）
  3. `lsp_sources` が `pyright: indexing` → `unavailable` になる（約 8 秒後）
- 備考: 本体の作業ツリー側で同じことが起きるかは未確認（使い捨て側だけ見た）。
- 調査はしていない（直す役の仕事）。

## HEAD での再確認（dogfood-2、2026-10-29 15:21、42640ac の使い捨て作業ツリー）
- HEAD でも再現する: pyright が init_failed のまま 3 回再起動して unavailable（log: ERROR lucianke.lsp.pool: LSP python/pyright: giving up after 3 restarts (init_failed)。ro・edit の両セッション）。

## 原因
使い捨て作業ツリーに限らず、**ワークスペースに `.venv` が無く、サーバーを起こした venv を有効化していない**ときに起きる（本体の作業ツリーでも、`.venv` の無い F-POLY のコピーを `set_roots` すれば同じに再現した）。重なった 3 つ。
1. 探索が Lucianke 自身の Python 環境を見ない（主因）。`find_command` の順は 環境変数 → 指定の venv → `<ワークスペース>/.venv` → `node_modules/.bin` → `PATH` だった。F-POLY のコピーには `.venv` が無く（`detect_python_env` は PATH の python に落ちるので `venv_root_of` も `None`）、`<temp>/.venv/Scripts` は PATH に無い（`python.exe -m lucianke` で起こしただけで有効化していない）。このため既定の `basedpyright-langserver` は見つからず、代替の `pyright-langserver` を PATH から掴んだ（この機は `C:\Users\shini\AppData\Roaming\Python\Python313\Scripts\pyright-langserver.exe`、`pip install --user` の pyright で、claude.exe 配下の IDE 用ではない）。再現: 空のディレクトリで `find_command` を引くと `basedpyright None` / `pyright [...Roaming\Python\Python313\Scripts\pyright-langserver.exe]` / `ruff [...Python310\Scripts\ruff.exe]`。
2. 掴んだ PATH 上の pyright が、Lucianke が渡す環境では起動できない。`minimal_env` が `APPDATA`・`LOCALAPPDATA` を子に渡していなかったため、pip の Python 製ランチャーがユーザーの site-packages を見つけられず落ちた。`LspClient` で直接起こして stderr を見ると `ModuleNotFoundError: No module named 'pyright'`（`pyright-langserver.exe\__main__.py` の `from pyright.langserver import entrypoint`）。`APPDATA` を足すと `initialize` が通る。これが `server closed stdout (exit code None)` → `init_failed` の中身。
3. 理由が見えない・名前が揃わない。再起動と諦めのログに `last_error`（stderr の末尾）が出ず、`not_found` の理由が候補名を並べるだけだった。起こす前の `status`（T-00 の standby、T-08 の `lsp_sources`）は `defs[0]`（basedpyright）の名前を返し、起こした後は実際に起こした定義（代替の pyright）の名前になる。票の「T-00 は basedpyright、T-08 は pyright」はこの食い違い。

## 直し方
- `lsp/servers.py`: `find_command(host_venv=)` と `host_venv()`（`sys.prefix`）を足し、探索順を 環境変数 → 指定の venv → `<ワークスペース>/.venv` → `node_modules/.bin` → **Lucianke 自身の環境の `Scripts|bin`** → `PATH` にした（ワークスペースの venv が先。プロジェクトが固定した版が勝つ）。`find_command` の既定は `host_venv=None`（探索の単体テストが実行環境に依らないように）で、`pool.default_resolver` が渡す。
- `lsp/pool.py`: `default_resolver` が `host_venv()` を渡す。`_pick_for` に切り出し、見つからないときの `not_found` の理由に候補（サーバー名と実行ファイル名）と探した場所を入れる。standby の名前（`LspPool.status` と `LspHandle.snapshot`）は、次に起こす定義（優先順で最初に見つかるもの。どれも見つからなければ既定）にする。再起動と諦めのログに `last_error` を 300 文字まで付ける。
- `core/procs.py`: `_ENV_BASELINE` に `APPDATA`・`LOCALAPPDATA` を足した（仕様書 §13.3 の「PATH、ロケール、VIRTUAL_ENV 等」の範囲。秘密ではなくユーザーの設定・site-packages の置き場）。
- テスト: `tests/unit/test_lsp_client.py`（`host_venv` が PATH より先・ワークスペースの venv より後・無ければ `None`）、`tests/unit/test_lsp_pool.py`（`default_resolver` が PATH に pyright だけあっても自環境の basedpyright を選ぶ、standby の名前が実際に起こす定義と一致する、`not_found` の理由に全候補が出る）、`tests/unit/test_core_procs.py`（`APPDATA`・`LOCALAPPDATA` が通り、秘密は通らない）。
- コミット: pool と servers の本体は 20d のコミット 73d39d4 に一緒に入った（同じファイルを同時に触ったため）。残り（`procs.py`・テスト）は f1c0f59。

## 確認
- 手順: `git worktree add --detach <temp> <コミット>` → その中で `uv sync --frozen` → `<temp>/.venv/Scripts/python.exe` を MCP の stdio クライアントから `python -m lucianke` で起こし、`.venv` の無い F-POLY のコピー（`tests/fixtures/poly`）を `set_roots`。`configure_workspace status` を直後と最後に、`get_diagnostics` を 2 秒おきに引いて `lsp_sources` と `lsp_daemons` を見る（台本は使い捨て。観点は `bench/dogfood/run_dogfood2.py` の `wait_lsp` と同じ）。
- 修正前（本体の作業ツリー、F-POLY のコピー）: `lsp_sources` が `pyright: indexing` → 約 8 秒後に `unavailable`。`lsp_daemons` は `{types, pyright, unavailable, pid: null, init_failed}`、ruff は `ready`。ログは `[pyright] LSP disconnected: server closed stdout` ×4 → `giving up after 3 restarts (init_failed)`。
- 修正後（f1c0f59 の使い捨て作業ツリー + `uv sync --frozen`）: `status` 直後は `basedpyright standby` / `ruff standby`、約 19 秒で `lsp_sources` が `basedpyright: ready` / `ruff: ready`、最後の `lsp_daemons` は `{types, basedpyright, ready, pid: 87020}` / `{lint, ruff, ready, pid: 81160}`。T-00 と T-08 の名前は最初から最後まで一致。使い捨ての作業ツリーと LSP は片付け済み。
- `tests/unit/test_lsp_pool.py`・`test_lsp_client.py`・`test_core_procs.py`・`test_lsp_features.py`・`test_lsp_documents_router.py` 190 件 green、`tests/contract/test_T00_configure.py`・`test_T08_diagnostics.py` 67 件 green、`ruff check` green。
- 切り分け: PATH の pyright は `APPDATA` を足すと `initialize` を通る（`LspClient` を直接）ところまで確かめた。basedpyright が無い環境で代替の pyright が最後まで ready になるところまでは通していない（この機の PATH の pyright は 1.1.409）。
