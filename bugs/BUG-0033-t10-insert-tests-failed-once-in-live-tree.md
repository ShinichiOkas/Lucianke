# BUG: T-10 挿入テスト（test_T10_insert.py）が共有作業ツリーでの 1 回の実行だけ 9 件落ちた

見たことだけ（原因は調べていない）。

- 症状: コミット 96866a5（T-10 の挿入 3 操作）の push 直後、共有作業ツリーで
  `uv run`（`.venv/Scripts/python.exe -m pytest -p no:cacheprovider -q tests/contract/test_T10_insert.py tests/contract/test_T10_edit_body.py tests/unit/test_import_graph.py`）を 1 回走らせたところ `9 failed, 104 passed`。
  末尾に見えた失敗は次の 3 件（残り 6 件の ID は取っていない。同じ 2 つのテスト群だった可能性が高いが未確認）:
  - `tests/contract/test_T10_insert.py::test_postcondition_3_target_urn_and_new_range_point_at_the_inserted_symbol[target7-insert_before-...PaymentClient.refund-end]`
  - `tests/contract/test_T10_insert.py::test_postcondition_3_target_urn_and_new_range_point_at_the_inserted_symbol[target8-insert_member-...PaymentClient.refund-start]`
  - `tests/contract/test_T10_insert.py::test_compact_output_follows_the_grammar[target2-insert_member-PaymentProcessor.refund]`
- そのときの作業ツリー: 他の役の未コミット変更が多数（`git status` で見えたもの: `src/lucianke/mcp/budget.py`、`mcp/pipeline.py`、`mcp/render/json_out.py`、
  `core/urn.py`、`syntax/symbols.py`、`syntax/parsers.py`、`syntax/skeleton.py`、`index/*`、`lsp/*`、`tools/t01・t06・t07` ほか）。
  同じ変更途中の状態で、これより前に `Packer.pack() got an unexpected keyword argument 'record_text'`（`mcp/fit.py` が `mcp/budget.py` より先に進んでいた）で 47 件落ちた観測もある。
- その後の再実行: 同じコマンドが 68 passed（test_T10_insert.py のみ）→ 113 passed → 113 passed。
- 隔離環境（`git archive HEAD` に自分の変更だけを重ねた木。他の役の未コミット変更なし）: 同じ 4 ファイル（insert・edit_body・delete・import_graph）が 3 回連続 146 passed。
- 再現手順: 現時点では再現していない。落ちた回の出力は取れていない（失敗名の末尾 3 件のみ）。
- 原因は断定しない。他の役の変更途中の状態（上記ファイル群）との相互作用か、テスト自体の揺れかは未調査。直す役が、失敗時の出力を取りながら共有作業ツリーで繰り返し走らせて確かめる。
