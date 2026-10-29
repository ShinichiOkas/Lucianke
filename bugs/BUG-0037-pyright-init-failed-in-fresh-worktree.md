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
