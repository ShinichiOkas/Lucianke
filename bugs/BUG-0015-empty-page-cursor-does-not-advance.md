# BUG-0015 レコードが 1 件も入らない小さい max_tokens で、同じ cursor が返り続け先へ進めない（get_file_skeleton）

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状: 期待は仕様書 §5.6-4（レコード 0 件・next_cursor あり・warnings に理由、の成功応答）と、クライアントが cursor で最後まで読み進められること。実際は 0 件の応答の `# more: cursor=` が毎回同じ値（`"o":0`）で、続きの取得が永遠に進まない。
- 再現手順:
  - `get_file_skeleton {"paths": [tests/unit/test_core_hashing.py, tests/fixtures/poly/src/shop/services/payment.py, src/lucianke/tools/dev_ping.py, src/lucianke/syntax/types.py, src/lucianke/mcp/types.py, tests/unit/test_core_compact_lines.py, tests/unit/test_core_limits.py, src/lucianke/mcp/render/json_out.py, src/lucianke/config.py, src/lucianke/bootstrap_io.py, src/lucianke/edit/lock.py, tests/unit/test_import_graph.py, src/lucianke/core/errors.py, tests/unit/test_core_tokens.py, tests/unit/test_core_errors.py], "max_tokens": 200}` → 本文は `! no record fits in max_tokens=200: the next record needs ~…` と `# more: cursor=…`。その cursor を `cursor` に付けて同じ引数で呼ぶと、同じ応答と同じ cursor。
  - 同じ 1 本目だけ（`paths` に `tests/unit/test_core_hashing.py` 1 件、`max_tokens: 200`）なら骨格が `~193/200` で返る。paths を 15 本にすると 1 本目さえ入らない。
  - `get_symbol_bodies {"targets":[{"path":"src/lucianke/mcp/budget.py","symbol_name":"Packer"}],"max_tokens":200}` も同様: `no record fits … the next record needs ~3122 tokens`、cursor は `"o":0` で先へ進まない。
- 頻度: 上記 3 ケース中 3。`bench/dogfood/run_dogfood.py 3` の `200` 欄（`pages: 2`、2 ページ目の cursor が 1 ページ目と同一で打ち切った）。
- 調査はしていない（直す役の仕事）。
