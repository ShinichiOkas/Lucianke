# BUG-0039 ruff が ready でも get_diagnostics が 0 件（同じ行に T-13 は ruff の quickfix を返す）

- 報告: 2026-10-29 / 見た人: dogfood-2（実 stdio クライアント、使い捨て作業ツリー b24f766、F-POLY のコピー）
- 症状: ruff が `ready` なのに `get_diagnostics` が 0 件（`diagnostics: 0 in 0 files`）を返す。同じ行・同じファイルに対して `apply_code_action`（T-13）は ruff の quickfix を 4〜6 件返す（`E401 Split imports`・`F401 Remove unused import` など）ので、ruff は診断を持っている。
  - 対象: `src/shop/models/lint_plain.py`（`import os, sys\n` の 1 行だけ。BOM も CRLF も無い対照）と `src/shop/models/bom_lint.py`（BOM + CRLF の同じ 1 行）。どちらも 0 件。`min_severity: hint`・`sources` 省略。
  - 応答: `lsp=unavailable`、`lsp_sources=[{pyright: unavailable}, {ruff: ready}]`、`! LSP が利用できないため、構文エラー（Tree-sitter）だけを返しています。…Lint の診断は含まれていません。`。pyright 側は BUG-0037（init_failed）。
  - 期待: ruff が ready なら、ruff の診断（E401・F401）は返る。型 LSP が落ちていることで lint 側の診断まで捨てているように見える（見ただけ）。
  - 影響: 観点 7（BOM 付き CRLF の行 0 を utf-8 を採る ruff に開かせたときの T-08 の診断の列）を、この環境では確かめられない。
- 再現手順: `configure_workspace set_roots`（F-POLY のコピー。`lint_plain.py` を足す）→ 10 秒待つ → `get_diagnostics {paths: ["src/shop/models/lint_plain.py"], min_severity: "hint"}`。台本: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> ro`（`obs2_t08.json` の `n`・`lsp_sources`）。
- 調査はしていない（直す役の仕事）。

## HEAD での再確認（dogfood-2、2026-10-29 15:21、42640ac の使い捨て作業ツリー）
- HEAD でも再現する: lint_plain.py・bom_lint.py ともに get_diagnostics が 0 件のまま（lsp_sources は pyright=unavailable・ruff=ready）。

## 原因
- 6e28093（`on_diagnostics` をサーバー名で書く直し）の後の HEAD（142ce99）でも再現した。実の ruff だけ `ready`、型の LSP は `unavailable` にした契約テスト（`test_real_ruff_ready_with_types_lsp_unavailable`）が `diagnostics: []`。6e28093 は PaaD の diagnostics 表への書き込みの直しで、T-08 が読むのは LSP ハンドルのハブ（`DiagnosticsHub`）なので、この症状には効かない。
- `tools/t08_get_diagnostics.py` が経路（水準）も診断を集めるハンドルも、**型の LSP（`types` 役）の状態 1 つ**で決めていた。`_status_of` は `types` 役の状態だけを `select()` に渡し、`handles = _ready_handles(...) if choice.confidence is EXACT else []` で、型の LSP が `ready` でなければ `ready` な ruff のハンドルがあっても集めなかった。警告（`_degraded_warning`）も役を区別せず「LSP が利用できないため、構文エラー（Tree-sitter）だけを返しています。…Lint の診断は含まれていません」と言うので、ruff が `ready` なのに Lint が無いと断定する応答になっていた（`lsp_sources[]` は役ごとなので応答の中で食い違う）。
- 仕様書 §11.4 の処理規約 3・「精度と降格」表も「`ready` でない→構文エラーだけ」と、役を分けずに書いている（下の「直し方」の挙動が仕様に先行している。仕様書は別の役の未コミットの差分があるので、この票では触らない）。

## 直し方
- 診断を集めるハンドルは、役を問わず `ready` なもの全部にした（`types` が先、`lint` が後）。水準（`conf=`）と `lsp=` は従来どおり型の LSP の状態で決める。型の LSP が `indexing` / `unavailable` / `standby` で lint の LSP が `ready` なら、`conf=structural` のまま ruff の診断（診断ごとの `confidence_level` は `exact`、QuickFix の `code_action_id` も付く）と構文エラーを返す。`_resolution_meta.engine_used` は `ruff+tree-sitter`。
- 「`ready` の合図の直後に落ちた」の再判定は、`ready` な型の LSP が無いことで見る（lint の LSP だけ生きていても、型の LSP が答える経路〔exact〕のままにしない）。
- 警告を実際の経路に合わせた: 型が落ちて Lint が答えているときは「型の LSP が利用できない／起動・索引中のため、型・未定義参照の診断は含まれていません。Lint の LSP（ruff）と構文木（Tree-sitter）の診断を返しています」。どちらも `ready` でないときは従来の文面（構文エラーだけ・Lint も含まれない）。逆の組（型が `ready`、lint が `unavailable` / 起動中）は水準を `exact` のまま、`warnings[]` に「Lint の診断は含まれていません」を足す（`accuracy_warning` は付けない）。
- ツール説明（`SPEC.description`）と先頭の docstring も合わせた。
- テスト（`tests/contract/test_T08_diagnostics.py`）: 実の ruff だけ `ready` で F401 と import の指摘（E401 / I001。ruff 0.17 の既定は I001）が返る・`lsp_sources` が役ごと・ワークスペースのファイルが変わらない（`test_real_ruff_ready_with_types_lsp_unavailable`）。偽 LSP で、型が `unavailable`・lint が `ready`（lint の診断と QuickFix ID が返り、警告が「型の LSP」を言う）、型が起動中・lint が `ready`（完了後に `exact` になる）、型が `ready`・lint が `unavailable`（Lint が無い警告）。

## 確認
- 再現: 契約テスト（実の ruff）が HEAD（142ce99）で `diagnostics: []`、直した後は F401 / I001 を返す（赤→緑）。
- 実機（stdio）: 使い捨てのワークスペース（`import os, sys` の 1 ファイル）で、`LUCIANKE_LSP_BASEDPYRIGHT_CMD` / `LUCIANKE_LSP_PYRIGHT_CMD` を存在しないパスにして型の LSP を `init_failed` にし、`get_diagnostics {paths: [...], min_severity: hint}` を 6 秒間隔で呼んだ。ruff が `ready` になった呼び出しで `lsp=unavailable`・`conf=structural`・`engine_used: ruff+tree-sitter`・ruff の診断 3 件（I001・F401×2）と QuickFix ID が返り、`accuracy_warning` は「型の LSP が利用できないため、型・未定義参照の診断は含まれていません。Lint の LSP（ruff）と構文木（Tree-sitter）の診断を返しています。…」。ruff が `indexing` の間は従来の文面（構文エラーだけ）のまま。
- 範囲外で見たこと: ruff が `ready` になるのは起動（最初の `get_diagnostics`）から約 15 秒後（BUG-0050）。
