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

## 原因
原因は 2 つあった（どちらも単独で「連続呼び出しの途中でサーバーが止まる」を起こす）。

1. **構文木が解析時の `read_callback`（5 秒の期限つき）を保持し、`Node.text` のたびに呼び直す。期限を過ぎると `b""` を返して `Node.text` が戻らない。**
   - `syntax/parsers.py::_parse_with_deadline` の `read` は、期限（`DEFAULT_PARSE_DEADLINE_MS` = `STRUCTURAL_SEARCH_MS` = 5,000 ms）を過ぎると `b""` を返して解析を打ち切る。木はこの関数を保持し、py-tree-sitter 0.26 の `Node.text` は **解析が終わった後でも** その関数でソースを読み直す。解析から 5 秒後に `Node.text` を呼ぶと `read` が `b""` を返し続け、`Node.text`（C の中）が戻らない。イベントループのスレッドで空回りするので、以降のすべての要求が止まり、stdin を閉じても終わらない。
   - 最小再現（py-tree-sitter だけ、5 秒の安全網つき）: `parser.parse(read)`（期限 0.3 s）→ 0.5 s 待つ → `node.text` が戻らない。待たずに呼べば戻る。
   - `get_symbol_bodies` は `syntax/boundaries.py::_comment_text`（`node.text`）を通るので、`get_file_skeleton` を 5 秒以上続けたあとの最初の `get_symbol_bodies` で止まる。「100 回未満（5 秒未満）は 10 回以上すべて成功、200 回超は 5 回中 4 回で停止」「skeleton 200 回（11.5 秒）→ json 1 回のあとの最初の bodies で切断」「`--calls 20` では再現しない」と一致する。`relations.py`・`imports.py` の `node.text` も同じ（索引器のスレッドで、解析から 5 秒以上あとに呼ぶと同じ空回り）。
   - 証拠 1（`faulthandler.dump_traceback_later` を仕込んだサーバーを、一時の作業ツリー〈020a19e〉でループ呼び出し。107〜133 回目の `get_symbol_bodies` で再現）: MainThread が 6 秒おきのダンプのすべてで `boundaries.py:101 _comment_text`（と、そこから戻る `parsers.py:119 read`）にいた。asyncio のタスクは全部待機中で、空回りは MainThread だけ。
   - 証拠 2（問題出しの役が残した、止まったままの実プロセス〈14:15 起動、`py-spy dump --pid 23272`〉）: `MainThread (active+gil)`: `_comment_text (boundaries.py:101)` ← `_is_doc_comment` ← `_leading_start` ← `definition_range` ← `body_lines (t05_get_symbol_bodies.py:185)` ← `build_record` ← `handler` ← `pipeline._run` ← `server.on_call_tool`。ほかのスレッドはすべて idle。CPU 時間が 1 秒に 1 秒ずつ増えていた（2,064 秒＝約 34 分、空回り）。
2. **ホストが stderr のパイプを読まないと、ログの書き込みでサーバーが止まる。**
   - 呼び出しごとに INFO を 1 行（約 110 バイト）stderr に出す。`StreamHandler` の同期書き込みは、パイプが満杯（Windows は約 4 KB）になるとイベントループごと止まる。stderr のパイプを作って読まないクライアントで、33 回目の呼び出しで止まった（`tests/e2e/test_long_session_stdio.py` を直す前のコードで再現）。
   - 問題出しの役の台本は stderr をファイルへ向けていたので 1 は見えて 2 は見えなかったが、stderr を読まないホストでは 2 が先に出る。

## 直し方
- 1: `_parse_with_deadline` の `read` の期限・打ち切りを **解析の間だけ**（`parsing` フラグ。`parse()` を抜けたら落とす）効かせ、解析後はソースをそのまま返す。`Node.text` は期限切れの木でも（打ち切られた木の解析できた部分でも）戻る。`syntax/parsers.py`。
- 2: stderr へは別スレッドの書き手（`logging_setup.NonBlockingLineHandler`、daemon、`lucianke-stderr`）が書く。`emit` は行をキューに入れるだけで待たない。書き手が詰まっている間に 512 行を超えた分は捨て、戻ったら「N log lines dropped」を 1 行出す（JSON Lines のログファイルには全行が残る）。書き込みは `sys.stderr` ではなく fd へ直接（`_FdWriter`）なので、書き手が詰まったままでも終了時の `sys.stderr.flush()` が巻き込まれない。fd が取れなければ従来の同期 `StreamHandler`。`logging_setup.py`。
- テスト: `tests/unit/test_syntax_parsers.py`（期限後の `Node.text`。別プロセスで見張る）、`tests/unit/test_config_logging.py`（詰まった書き手でも `emit` が戻る・溢れた分を捨てる・順序）、`tests/e2e/test_long_session_stdio.py`（実プロセスで 1,000 回の連続呼び出し〈skeleton と bodies を交互、json と compact を混ぜる〉を stderr の読まないパイプで。最初の解析から 6 秒後の `get_symbol_bodies`）。直す前のコードでは前者が `bodies` の 1 回目（期限後）、後者が 33 回目で、いずれも 30 秒のタイムアウトで落ちることを確かめた。

## 確認
- 一時の作業ツリー（origin/main の 020a19e + 直し）で、stderr を読まないパイプのまま 1,000 回（skeleton / bodies 交互、json と compact 混在）を 5 回続けて完走（各 11〜13 秒）。ほかに stderr をファイルへ向けた 1,000 回も完走（35 秒）。直す前は 89〜133 回目で必ず止まった（4 回中 4 回）。
- `tests/e2e`・`tests/invariants`・`tests/unit/test_config_logging.py`・`test_syntax_parsers.py`・`test_import_graph.py` が green（262 件）、`ruff check` green。
- 止まったまま残っていた実プロセス（`lucianke.exe` PID 38660、親 `uv.exe` PID 83000、本体 `python.exe` PID 23272 / 82208）は、スタックを取ったあと止めようとしたが、自動モードの分類器に拒否された。残っている。`.venv\Scripts\lucianke.exe` を握っているので、止まっていれば BUG-0030（`uv run` の環境同期が os error 32 で失敗）の原因になる。調整役が止める。
