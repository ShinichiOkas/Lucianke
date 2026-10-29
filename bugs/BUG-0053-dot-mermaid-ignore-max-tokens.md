# BUG-0053 format=dot / mermaid の応答が max_tokens を守らない（max_tokens 200 に対し本文 778 / 321 トークン、ヘッダの ~used は 126）

- 報告: 2026-10-29 / 見た人: BUG-0014・BUG-0017 を仕上げた役
- 症状: 期待は仕様書 §5.7「`max_tokens` は応答テキスト全体の上限、`used_tokens_est` は全文の数え直しと一致」。実際は `format: "dot"` / `"mermaid"` の応答が `max_tokens` に関係なく同じ全文で、ヘッダの `~used` は全文より小さい。
- 再現手順（`tests/fixtures/poly`、`tests/contract/test_T06_T07.py` の `pipeline` と同じ組み立て）: `query_call_hierarchy {"target": {"path":"src/shop/services/payment.py","symbol_name":"PaymentProcessor.process_charge"}, "direction":"both", "depth":5, "format":"dot", "max_tokens":200}` → 先頭行 `// # query_call_hierarchy ok ... ~126/200tok`、全文を `TokenEstimator().count` で数えると 778。`max_tokens` を 600 にしても同じ 778（切り詰めも起きない）。`"format":"mermaid"` は先頭行 `%% ... ~126/200tok`、全文 321（600 でも同じ）。同じ呼び出しの json は全文（`max_tokens: 8000`）で 1,755。
- 頻度: dot・mermaid の全呼び出し（2 形式 × 2 予算で 4/4）。
- 備考: `mcp/fit.py` の `FITTED_FORMATS` は compact と json だけ（dot / mermaid は従来どおりレコードを compact の行で数えるだけ）。`data_static["graph"]` を丸ごと描いているように見える（調査はしていない）。
- 調査はしていない（直す役の仕事）。
