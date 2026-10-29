# BUG-0020 連続呼び出しの途中で、サーバーが無応答になる／Connection closed になることが断続的にある（再現は不安定）

- 報告: 2026-10-29 14:38 / 見た人: ドッグフーディング担当
- 症状: `uv run --quiet --directory S:/work/develop/Lucianke lucianke` を MCP クライアント（stdio）で起動し、同じセッションで 2 ツールを続けて呼ぶと、次のどれかが起きた。
  1. `bench/dogfood/loop_probe.py`: `get_file_skeleton` 200 回（各 15〜60 ms、合計 11.5 秒）→ json 1 回（17 ms）のあと、最初の `get_symbol_bodies`（`src/lucianke/mcp/budget.py` の `Packer.pack`）で `mcp.shared.exceptions.MCPError: Connection closed`。サーバーの標準エラーに例外の出力は無く、`tool call finished` が 201 行で止まっていた。
  2. `bench/dogfood/crash_probe.py 200`（skeleton 200 回 → bodies 5 回）を 1 回目に走らせたとき 60 秒で応答が返らず `timeout` で打ち切り。同じ引数の 2 回目は 5 回とも成功（12 ms 前後）。
  3. `bench/dogfood/timing_probe.py`: `get_file_skeleton` の計測（115 回）は完走したが、続く `get_symbol_bodies`（ウォームアップ 15 回 + 100 回 + json 100 回）は 120 秒で終わらず打ち切り。
  4. `bench/dogfood/run_dogfood.py 7 8`（観点 7・8）が 300 秒で終わらなかった（`get_symbol_bodies` を含む）。
- 期待: 仕様書 §14.4（定常状態の応答）— 数百回の読み取り呼び出しが連続して応答する。
- 状況の観察（原因は調べていない）: この間、同じ作業ツリーでほかの役が `src/` を変更し続けていた。起動ログの `tools=` は 11 → 12 → 20 と変わり、`index opened … schema=unchanged` から `schema=migrated` に変わった回があり、起動直後に `scan job job_0001 started` が走る。
- 再現手順: 上記 3 本のスクリプトを `PYTHONUTF8=1 uv run --quiet python bench/dogfood/<name>.py` で実行（標準エラーを別ファイルに）。
- 頻度: 連続 200 回超の呼び出しを含む実行 5 回中 4 回で停止または切断（loop_probe 1/1、crash_probe 1/2、timing_probe 1/1、run_dogfood 7 8 の 1/1。残りの 1 回は完走）。100 回未満の実行は 10 回以上すべて成功。
- 調査はしていない（直す役の仕事）。
