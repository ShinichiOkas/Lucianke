# BUG-0017 format=json の budget.used_tokens_est が応答 JSON の実サイズの約 1/7（max_tokens 2000 に対し JSON 本文は約 14,000 トークン相当）

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状: 期待は仕様書 §5.7「`budget.used_tokens_est` には応答テキスト全体の推定値」と、§5.6-1「`max_tokens` を超えない範囲で詰める」。実際は `format: "json"` の `used_tokens_est`（1884）が、応答の JSON テキストを `TokenEstimator().count()` で数えた値（14192）の約 13%。json で呼ぶと max_tokens を大幅に超える応答が返る。
- 再現手順: `get_file_skeleton {"paths":["src/lucianke/core/urn.py","src/lucianke/core/errors.py"],"max_tokens":2000,"format":"json"}` → `budget: {"max_tokens":2000,"used_tokens_est":1884,"truncated":false}`、`page.returned: 2`、`content[0].text` を `lucianke.mcp.budget.TokenEstimator().count` で数えると 14192（`bench/dogfood/run_dogfood.py 5` の `json_used_vs_recount`）。
- 頻度: 1 件中 1。
- 備考: 仕様書が json の予算を「compact 相当の量」で数えると決めているなら、その旨が書かれていない（§5.7 は応答テキスト全体）。
- 調査はしていない（直す役の仕事）。
