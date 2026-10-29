# BUG-0043 configure_workspace の lsp_daemons[].started_at が ready でも常に null（set_python_env の事後条件が検査できない）

- 報告: 2026-10-29 / 見た人: dogfood-2（実 stdio クライアント、使い捨て作業ツリー b24f766）
- 症状: `configure_workspace status` の `lsp_daemons[].started_at` が、LSP が `ready` で `pid` を持っていても常に `null`。
  - 実測: ruff `{status: "ready", pid: 67796, started_at: null}`。`set_python_env`（インタープリタ指定）の応答後 3 秒の `status` では、`basedpyright`・`ruff` ともに `started_at: null`（`status: indexing`）。
  - 仕様書 §11 T-00 の事後条件: 「`set_python_env` 成功後の `status` で `python_env.interpreter` が指定値の正規化形と一致し、Python 系 LSP の `started_at` が呼び出し時刻より後である」。`python_env.interpreter` は一致した（`manager: explicit`）が、`started_at` は検査できない（null）。
  - 参考: 同じサーバーの名前が T-00 では `basedpyright`、T-08 の `lsp_sources[].server` では `pyright`（BUG-0037 にも記載）。
- 再現手順: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> cfg`（`obs2_cfg.json` の `python_env.lsp_started`）。
- 調査はしていない（直す役の仕事）。
