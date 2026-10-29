# BUG-0015 レコードが 1 件も入らない小さい max_tokens で、同じ cursor が返り続け先へ進めない（get_file_skeleton）

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状: 期待は仕様書 §5.6-4（レコード 0 件・next_cursor あり・warnings に理由、の成功応答）と、クライアントが cursor で最後まで読み進められること。実際は 0 件の応答の `# more: cursor=` が毎回同じ値（`"o":0`）で、続きの取得が永遠に進まない。
- 再現手順:
  - `get_file_skeleton {"paths": [tests/unit/test_core_hashing.py, tests/fixtures/poly/src/shop/services/payment.py, src/lucianke/tools/dev_ping.py, src/lucianke/syntax/types.py, src/lucianke/mcp/types.py, tests/unit/test_core_compact_lines.py, tests/unit/test_core_limits.py, src/lucianke/mcp/render/json_out.py, src/lucianke/config.py, src/lucianke/bootstrap_io.py, src/lucianke/edit/lock.py, tests/unit/test_import_graph.py, src/lucianke/core/errors.py, tests/unit/test_core_tokens.py, tests/unit/test_core_errors.py], "max_tokens": 200}` → 本文は `! no record fits in max_tokens=200: the next record needs ~…` と `# more: cursor=…`。その cursor を `cursor` に付けて同じ引数で呼ぶと、同じ応答と同じ cursor。
  - 同じ 1 本目だけ（`paths` に `tests/unit/test_core_hashing.py` 1 件、`max_tokens: 200`）なら骨格が `~193/200` で返る。paths を 15 本にすると 1 本目さえ入らない。
  - `get_symbol_bodies {"targets":[{"path":"src/lucianke/mcp/budget.py","symbol_name":"Packer"}],"max_tokens":200}` も同様: `no record fits … the next record needs ~3122 tokens`、cursor は `"o":0` で先へ進まない。
- 頻度: 上記 3 ケース中 3。`bench/dogfood/run_dogfood.py 3` の `200` 欄（`pages: 2`、2 ページ目の cursor が 1 ページ目と同一で打ち切った）。
- 調査はしていない（直す役の仕事）。

## 原因
- `Packer._fit` は `m == 0`（1 件も入らない）のときも、`cursor_factory(start)` でカーソルを作って返していた（仕様書 §5.6-4 と設計書 §7.2.2「レコード 0 件・next_cursor あり」の字面どおり）。そのオフセットは呼び出しの `cursor` と同じ（先頭なら `"o":0`）なので、辿るクライアントは同じ呼び出しを繰り返すだけで先へ進めない。カーソルは `max_tokens` を引数の照合から外している（`CursorCodec.hash_args`）から、`max_tokens` を上げた呼び直しは手元のカーソル（先頭なら無し）でできる。出し直したカーソルは何も足さない。
- 再現手順の 15 本の骨格は、予約（ヘッダ・`head_kv` の 15 パス）だけで 200 に近く 1 本目が入らない例。T-05 の 3122 トークンの本体は別件（BUG-0016）。

## 直し方
- `mcp/budget.py`（`Packer._fit`）: 1 件も入らないページは、レコード 0 件・`warnings` 1 件の成功応答のまま、`next_cursor` と `# summary:` / `# more:` を出さない。トレーラは描かれないので `used_tokens_est` にも数えない（予約 + 警告行）。警告に必要な大きさを書く: `no record fits in max_tokens=200: the next record needs ~N tokens; raise max_tokens to ~(予約 + N) or more and call again with the same arguments`。`next_cursor` を返すページは必ず 1 件以上を含み、オフセットが進む（不変条件）。エラーにも「最小 1 件を返す」にもしなかった: 仕様書 §5.6-4 は成功応答と明記、1 件の強制は `used_tokens_est <= max_tokens`（MUST）を破る。
- 仕様書 §5.6-4・設計書 §7.2.2 の「next_cursor あり」を「next_cursor は出さない」に改める（別コミット。師匠が覆したいときは言う）。
- テスト: `tests/unit/test_mcp_budget.py`（旧挙動を固定していた 2 件を「カーソル無し・警告に必要な大きさ・トレーラを数えない」に直し、警告の目安で入る 1 件を足し、乱数の不変条件を「カーソルを返すのは 1 件以上入ったときだけ」に）、`tests/unit/test_mcp_budget_cursor.py`（乱数 300 ケースで「カーソルを返すページはオフセットが進む」、パイプライン経由で json / compact とも `next_cursor` 無し・`# more:` 無し・`max_tokens` を上げれば入る）。

## 確認
- HEAD の `src` に直しだけを重ねた状態で `test_mcp_budget_cursor.py`・`test_mcp_budget.py`・`test_mcp_pipeline.py`・`test_mcp_render.py` が 185 件 green。
- 作業ツリーの `budget.py` には BUG-0016 の担当の未コミットの変更（`Record.resume`・`join_offset`）が混ざっているので、私の分だけを HEAD に重ねて別に作ったコミットで入れた。
