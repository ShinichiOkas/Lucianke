# BUG-0043 configure_workspace の lsp_daemons[].started_at が ready でも常に null（set_python_env の事後条件が検査できない）

- 報告: 2026-10-29 / 見た人: dogfood-2（実 stdio クライアント、使い捨て作業ツリー b24f766）
- 症状: `configure_workspace status` の `lsp_daemons[].started_at` が、LSP が `ready` で `pid` を持っていても常に `null`。
  - 実測: ruff `{status: "ready", pid: 67796, started_at: null}`。`set_python_env`（インタープリタ指定）の応答後 3 秒の `status` では、`basedpyright`・`ruff` ともに `started_at: null`（`status: indexing`）。
  - 仕様書 §11 T-00 の事後条件: 「`set_python_env` 成功後の `status` で `python_env.interpreter` が指定値の正規化形と一致し、Python 系 LSP の `started_at` が呼び出し時刻より後である」。`python_env.interpreter` は一致した（`manager: explicit`）が、`started_at` は検査できない（null）。
  - 参考: 同じサーバーの名前が T-00 では `basedpyright`、T-08 の `lsp_sources[].server` では `pyright`（BUG-0037 にも記載）。
- 再現手順: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> cfg`（`obs2_cfg.json` の `python_env.lsp_started`）。
- 調査はしていない（直す役の仕事）。

## 原因
HEAD（c613fb4 時点）では再現しない。見た作業ツリー（b24f766）は 16c の `LspHandle.started_at`（8a6f40f。pool.py が起動を始めた時刻を入れる）より前で、ハンドルに `started_at` がまだ無かった。T-00 の `_lsp_daemons`（`tools/t00_configure_workspace.py`）は `getattr(h, "started_at", None)` で読むので、属性が無い版では常に `null` になる。いまは `LspHandle._mark_started`（`_begin` 内）が入れ、`_stop` で `None` に戻す。T-00 の側の修正は要らない。

## 確認
- 実 stdio（`python -P -m lucianke`、F-POLY のコピー、キャッシュは tmp）で `set_roots` → `set_python_env`（このリポジトリの `.venv` の python）→ `status` を 1 秒おきに 10 回: 呼び出しの 1 秒後から basedpyright・ruff ともに `started_at` が入り（`2026-10-29T06:20:34.283+00:00` / `.293+00:00`）、呼び出し時刻（`06:20:33.553`）より後。`status` が `indexing` のままでも、`pid` が付いていれば `started_at` は入っている。起動の試みは各 1 回（`started_at` がその後動かない）。
- 回帰テスト `tests/unit/test_t00_set_python_env_restart.py::test_status_reports_started_at_of_the_python_daemons_after_set_python_env`（実物の `RuntimeHub` + `LspPool` + 偽 LSP。`_lsp_daemons` の `started_at` が `null` でなく、`set_python_env` の呼び出し時刻より後）。
- `tests/contract/test_T00_configure.py::test_post3_...` も `started_at > called_at` を検査している（偽プールでの確認）。
- 票の「サーバーの名前が T-00 では basedpyright、T-08 の `lsp_sources[].server` では pyright」は別件（BUG-0037）。

## HEAD での再確認（dogfood-2、2026-10-29 15:21、42640ac の使い捨て作業ツリー）
- HEAD では再現しない。`set_python_env`（インタープリタ指定）の直後の `status` で `lsp_daemons[]` の `started_at` は `basedpyright: 2026-10-29T06:21:09.541+00:00`、`ruff: 2026-10-29T06:21:09.579+00:00`（呼び出しより後）。見たコミットが `LspHandle.started_at` の追加（8a6f40f）より前（b24f766）だったための誤報。閉じてよい。
