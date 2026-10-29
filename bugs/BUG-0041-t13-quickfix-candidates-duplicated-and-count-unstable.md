# BUG-0041 apply_code_action の E_TARGET_AMBIGUOUS で同じ id・title の候補が重複し、件数のメッセージも回ごとに違う

- 報告: 2026-10-29 / 見た人: dogfood-2（実 stdio クライアント、使い捨て作業ツリー b24f766）
- 症状: `apply_code_action {path: "src/shop/models/lint_plain.py", line: 1, action_kind_or_id: "quickfix", dry_run: true}`（1 行目 `import os, sys`）が `E_TARGET_AMBIGUOUS` を返すとき、`candidates[]` に **同じ id・同じ title の候補が 2 つずつ** 並ぶことがある。断続的（同じ台本の別の回では `bom_lint.py`〈同じ 1 行の BOM + CRLF 版〉が 4 件で重複なし、`lint_plain.py` が 6 件）。
  - 実測（json `error.candidates`、`lint_plain.py`）: `qf_0005 "Ruff (E401): Disable for this line"`、`qf_0006 "Ruff (E401): Split imports"`、`qf_0007 "Ruff (F401): Disable for this line"` ×2、`qf_0008 "Ruff (F401): Remove unused import"` ×2（あと 2 件は未記録）。F401 は `os` と `sys` で 2 つの診断だが、同じ id・同じ title の候補が並ぶ。
  - メッセージの件数が回ごとに違う: 同じ入力で `"4 code actions of kind 'quickfix' are offered"` と `"6 code actions …"`。compact の `! candidate:` 行にも同じ id の重複行が出る（`qf_000b` ×2）。
  - 参考: `qf_0004`（Remove unused import）を ID 指定で適用すると、`﻿import os, sys\r\n` は `﻿`（BOM だけ）に、`import os, sys\n` は空になった（os と sys の両方が消えた。BOM は保たれた）。
- 期待: 同じ action は 1 件だけ列挙される。件数のメッセージは候補の数（重複を除いた数）と一致し、回によって変わらない。
- 再現手順: F-POLY のコピーに `src/shop/models/lint_plain.py`（`import os, sys\n`）を足して `set_roots` → 10 秒待つ → 上の呼び出しを 2〜3 回。台本: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> edit`（`obs2_t13.json` の `cands:*`）。
- 調査はしていない（直す役の仕事）。

## HEAD での再確認（dogfood-2、2026-10-29 15:21、42640ac の使い捨て作業ツリー）
- HEAD でも再現する: lint_plain.py の候補に qf_0007 ×2（F401 Disable for this line）が出る。bom_lint.py は重複なし。

## 原因
- 重複は T-13 の候補の組み立て（`_choose_by_kind`）にあった。QuickFix では範囲に重なる診断を全部 `context.diagnostics` に載せて問い合わせる。1 行に同じ規則の診断が複数ある（`import os, sys` の F401 が `os` と `sys` の 2 つ）と、ruff は**診断ごとに**同じタイトルのアクション（"Remove unused import"、"Disable for this line"）を返す。T-13 はそれを全部 `found` に積み、`CodeActionStore.put` へ渡していた。台帳のキーは（ファイル・sha・供給元・範囲・種別・**タイトル**）で、同じキーには同じ ID を返す（`action` は後のもので置き換わる）。だから**同じ ID・同じタイトルの候補が並び、件数も重複の数だけ増えた**。
- 実機で再現（実 ruff 0.17.3。`import os, sys` の 1 行 + `def main()`）: 修正前の `candidates[].id` は `qf_0001, qf_0001, qf_0002, qf_0003, qf_0003, qf_0004` の 6 件（ID は 4 種類）、メッセージも "6 code actions"。
- 並べ方にも穴があった。`isPreferred` → 役 → 種別 → タイトルで並べても、同じタイトルどうしは入力順のまま（安定ソート）で、ID の割り当て順が ruff の返す順（診断の順）に依っていた。
- 件数の揺れ（4 と 6）: 実機の ruff は 1 つの `publishDiagnostics` に全診断（F401 ×2 と I001）を載せて 1 回で返す（JSON-RPC で直接確認: `PUB +0.03s 1 ['F401', 'F401', 'I001']`）。診断ハブは「最初の受信が空」の仮の診断を待ちから外す（`is_provisional`）ので、`wait_for` が返った時点の文脈は揃っている。診断を待つ時機で件数が変わる経路は、実機（LF と BOM+CRLF の両版、4 回連続、CPU 負荷あり）では再現しなかった。再現できたのは重複だけで、重複の数（診断の数）が件数に直接出ていた。

## 直し方
- `tools/t13_apply_code_action.py` に `_unique_candidates` を足した。候補を（`isPreferred`、役、種別、タイトル、供給元、**編集の中身〈JSON を正規化〉**）で決定的に並べ、**台帳が同じ ID を返す単位（供給元・種別・タイトル）で 1 件にする**（先頭を残す。編集が違うのに落としたときはデバッグログ）。ID の発番と件数（`E_TARGET_AMBIGUOUS` のメッセージ・`candidates[]`・compact の `! candidate:` 行）は、この重複除去の後の候補で数える。除いた結果 1 件になれば、曖昧ではなく、そのまま適用に進む。
- 診断の待ちは変えていない（上のとおり、揃ってから数える作りは既にある）。
- テスト: `tests/contract/test_T13_code_action.py` に `test_real_quickfix_candidates_are_unique_and_stable_when_one_line_has_two_diagnostics`（実 ruff。LF と BOM+CRLF の 2 版）。`import os, sys` の 1 行で、`candidates[]` の ID・（種別, タイトル）が重ならず、メッセージの件数が候補の数と一致し、4 回呼んでも ID・並び・件数が同じ、ファイルは不変。

## 確認
- 修正前は新しいテストが落ちる（ID 6 件に対して 4 種類）。修正後は `tests/contract/test_T13_code_action.py` 25 件 green。
- 負荷（CPU を食う 16 プロセスを並行）の下で `tests/contract/test_T13_code_action.py` を 10 回続けて実行: 10 回とも 25 件 green（19〜21 秒）。
- 未確認: dogfood-2 の台本（basedpyright も `ready` の実 stdio、`lint_plain.py`・`bom_lint.py`）での 4 件/6 件。契約テストは ruff だけの `LspPool` なので、basedpyright が同じ範囲に QuickFix を返す場合は見ていない。dogfood-2 の台本で `cands:*` を取り直し、重複が消え、件数が回によらず同じかを確かめること。
