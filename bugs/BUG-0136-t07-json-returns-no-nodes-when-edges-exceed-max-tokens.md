# BUG-0136 T-07 の json の形で、辺の多い木はノードが 1 件も返らず、続きも取れない

見たことだけを書く（調査していない）。2026-10-31 19:45 ごろ、BUG-0133 の直す役が `d3c676e`（BUG-0133 の直しの後。モジュール直下の呼び出しが
exact の木の `(module)` のノードとして出る）で、ky（F-TS）の `test/resilience.ts#client` の incoming を聞いたときに見た。typescript-language-server
4.3.3 は bun 経由。

## 症状
- `query_call_hierarchy`（`direction: incoming`、`depth: 2`、`format: json`、`max_tokens` は既定の 2000）:
  - `data.nodes` が 0 件、`data.edges` は 49 本（`test/resilience.ts` のモジュール直下から `client` への呼び出し 48 箇所と、`bodyCases` から 1 箇所）。
  - `page`: `{"next_cursor": null, "returned": 0, "total_estimate": 2}`。`budget`: `{"max_tokens": 2000, "used_tokens_est": 2644, "truncated": true}`。
  - `warnings`: `no record fits in max_tokens=2000: the next record needs ~847 tokens; raise max_tokens to ~3468 or more and call again with the same arguments`。
  - `_resolution_meta`: `lsp_status: ready`、`confidence_level: exact`。
  - 同じ呼び出しを 2 回しても同じ（2 回目は `used_tokens_est` 2642）。
- 同じ対象を `format: compact` で聞くと 2 行で返る（`~75/2000tok`）:
  ```
  <- test/resilience.ts (module) @test/resilience.ts:121
  <- bodyCases @test/resilience.ts:860
  ```
- BUG-0133 の直しの前は、この対象の exact の木は `bodyCases` の 1 ノードだけだった（モジュール直下の呼び元を捨てていた）。直しの前でも、
  モジュール直下の呼び出しの多い対象の構文木の答え（`lsp_status: indexing` の降格）は同じ形の `(module)` のノードを出していた。

## 再現
ky（`bench/.cache/ky`、`bun install --ignore-scripts` 済み）で、`query_call_hierarchy` を
`{"target": {"path": "test/resilience.ts", "symbol_name": "client"}, "direction": "incoming", "depth": 2, "format": "json"}` で呼ぶ。

## 仕様書
§11 T-07、§6（予算・ページング）。関連: BUG-0133。

## 原因・直し方・確認（2026-10-31）

### 原因

- json の `data.edges` は木全体の辺を `ToolOutput.data_static` に置いていた。パッカーの前置き（`mcp/fit.py` の `preambles`）は `data_static` を
  含む全文をレコード 0 件で描いて数えるので、辺 49 本（約 1,800 トークン）が **ノードを 1 件も詰める前に** 前置きとして予算を食い、残りの
  約 200 トークンには、先頭のノード（`(module)` の呼び出し箇所 48 を `call_sites[]` に持つ約 850 トークン）が入らなかった。0 件で返し、
  警告は「次のレコードは約 847 トークン」と、前置きの辺を含まない `max_tokens>=3468` を勧めた。1 件も入らないので `next_cursor` も無い。
  `used_tokens_est` 2,644 が `max_tokens` 2,000 を超えたのは、前置きだけでもう超えている（0 件のページは保証できない）ため。
- compact は辺を描かず（ノード 1 件 1 行）、前置きが小さいので同じ木が 2 行・約 75 トークンで返った。
- 辺は呼び出し箇所ごとにノードの `call_sites[]` と同じ数あり、辺とノードが予算の上で別々に数えられていた（ノードが入っても辺は全部、
  ノードが入らなくても辺は全部）。

### 直し方

- 辺はそれを持つノードのレコードに付ける（`core/records.Record.attached`。`{"edges": 行の列}`）。json は入ったレコードの付随行だけを
  `data.edges` に連ね（`mcp/render/json_out.build_envelope`。同一の行は最初の 1 件だけ）、付随行はレコードの大きさに数える
  （`core/records.json_record_text`。`json_out.record_text` と縮退版の測りが共有）。ノードが入らなければ辺も入らず、辺だけが残ることがない。
  `data.edges` は 0 件でもキーを出す。dot / mermaid は今までどおり木全体の辺から `graph` を組む（予算はレンダラが切り詰める）。
- 1 ノードの呼び出し箇所が多く入りきらないとき（`t07._shrinkable`）は、ノードを落とさず `call_sites[]` と辺を先頭から縮める
  （`Record.shrink_json`。大きさが予算以下の最大の件数を二分探索。省く件数の桁数で逆転しないよう、桁数を最大にして数える。予算に
  ついて単調）。縮めた版にだけ `call_sites_omitted`（省いた箇所の数）と `call_sites_note`（`call_sites truncated; all N sites:
  max_tokens>=M`。M はそのノードを丸ごと入れる予算の目安 = 丸ごとの大きさ + 前置き 800 を 100 単位に切り上げ）を付ける。
  呼び出し箇所を 1 つも入れられない予算では、今までどおり 0 件と「要る max_tokens」の警告。
- 仕様書 T-07 の json の表（`nodes[]` の縮めたノード・`edges[]` は入ったノードの分）、設計書の `Record.attached`。

### 確認

- 新しい試験 `tests/contract/test_T07_json_edges_budget.py`（5 件。修正前の src では既定の予算の再現が赤: 構文木の経路で 1 ノードに 80 箇所、
  既定の `max_tokens` 2,000 の json が 4,888 トークン・ノード 0 件）。応答が `max_tokens`（描いた全文の数え直し）を超えないこと、辺とノードの
  呼び出し箇所が 1 対 1 で端点が入ったノードか根であること、`max_tokens` を 500〜6,000 で上げて入るノード数・箇所数が減らないこと、
  縮めたノードの注が名指しする M で丸ごと返ること、カーソルで辿ると丸ごとの木と同じノードの並びになること、compact と丸ごとの json は
  変わらないこと。
- 関係する試験（T-06 / T-07・予算・見張り・Record / fit / render の単体）284 件 + 66 件 passed・3 skipped、`uv run ruff check` クリーン。
