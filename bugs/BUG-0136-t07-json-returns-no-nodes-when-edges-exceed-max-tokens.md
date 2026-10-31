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
