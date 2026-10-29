# BUG-0041 apply_code_action の E_TARGET_AMBIGUOUS で同じ id・title の候補が重複し、件数のメッセージも回ごとに違う

- 報告: 2026-10-29 / 見た人: dogfood-2（実 stdio クライアント、使い捨て作業ツリー b24f766）
- 症状: `apply_code_action {path: "src/shop/models/lint_plain.py", line: 1, action_kind_or_id: "quickfix", dry_run: true}`（1 行目 `import os, sys`）が `E_TARGET_AMBIGUOUS` を返すとき、`candidates[]` に **同じ id・同じ title の候補が 2 つずつ** 並ぶことがある。断続的（同じ台本の別の回では `bom_lint.py`〈同じ 1 行の BOM + CRLF 版〉が 4 件で重複なし、`lint_plain.py` が 6 件）。
  - 実測（json `error.candidates`、`lint_plain.py`）: `qf_0005 "Ruff (E401): Disable for this line"`、`qf_0006 "Ruff (E401): Split imports"`、`qf_0007 "Ruff (F401): Disable for this line"` ×2、`qf_0008 "Ruff (F401): Remove unused import"` ×2（あと 2 件は未記録）。F401 は `os` と `sys` で 2 つの診断だが、同じ id・同じ title の候補が並ぶ。
  - メッセージの件数が回ごとに違う: 同じ入力で `"4 code actions of kind 'quickfix' are offered"` と `"6 code actions …"`。compact の `! candidate:` 行にも同じ id の重複行が出る（`qf_000b` ×2）。
  - 参考: `qf_0004`（Remove unused import）を ID 指定で適用すると、`﻿import os, sys\r\n` は `﻿`（BOM だけ）に、`import os, sys\n` は空になった（os と sys の両方が消えた。BOM は保たれた）。
- 期待: 同じ action は 1 件だけ列挙される。件数のメッセージは候補の数（重複を除いた数）と一致し、回によって変わらない。
- 再現手順: F-POLY のコピーに `src/shop/models/lint_plain.py`（`import os, sys\n`）を足して `set_roots` → 10 秒待つ → 上の呼び出しを 2〜3 回。台本: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> edit`（`obs2_t13.json` の `cands:*`）。
- 調査はしていない（直す役の仕事）。
