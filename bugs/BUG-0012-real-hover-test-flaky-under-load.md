# BUG-0012 test_real_hover_returns_the_type_string が負荷の下で落ちる

- 報告: 2026-10-29 14:31 / 見た人: BUG-0005 の担当（`uv run pytest -q tests/unit` の 3 回目）
- 見たこと:
  - `tests/unit/test_lsp_features.py::test_real_hover_returns_the_type_string`（実の basedpyright）が全体実行の 3 回目で落ちた。1・2 回目は通った。
  - 単独の再実行では通る。失敗時の出力は取れていない。
  - 同時に 20 本ほどの役がテスト・LSP を走らせていた（負荷あり）。
- 再現手順: `uv run pytest -q tests/unit/test_lsp_features.py -k real_hover` を、別のプロセスで pytest を数本走らせながら 10 回。
- 調査はしていない（直す役の仕事）。

## 追記（見たことだけ）
- 2026-10-29 14:36 / 見た人: BUG-0008 の担当。`uv run pytest -q tests/unit tests/contract`（3 分 43 秒、他の役が並行で走行中）で `tests/unit/test_lsp_features.py::test_real_definition_and_references` も `test_real_hover_returns_the_type_string` と一緒に落ちた（実の basedpyright の 2 件）。単独の再実行（同ファイル 34 件）では全件通った。失敗時の出力は取れていない。

## 原因
- 再現: 別プロセスの CPU 負荷（busy loop を 60〜96 本）の下で `-k real_hover` を 10 回回すと 7 回落ちた（負荷なしでは 0 回）。落ち方は毎回同じで、`features.hover` が `LSP_TIMEOUT_HOVER_MS`（3 秒、§12.3）で `TimeoutError`（`call` 3.00 s）。
- 実の basedpyright の `ready` は解析の完了を意味しない。フィクスチャは文書を何も開かずに `wait_ready` するので、`ready` は診断でも `$/progress end` でもなく猶予時間 `ready_fallback_s=1.0` で決まる（設計書 §7.5.2 の合図 (3)）。その時点で basedpyright は何も解析していない。最初の要求（hover）が `didOpen` の直後に解析そのもの（routes.py と import 先、typeshed の読み込み）を払うので、負荷が高いと 3 秒を超える。負荷なしでも hover は約 0.5 秒かかり、余裕は 6 倍しかない。
- 製品の不具合ではない: 要求の期限（3 秒）は仕様で、初回の重さは許容（仕様書 §14.4）。テストが「`ready`＝温まった」と見なして最初の要求を冷間で打っていた。同じフィクスチャ `real` を使う definition・references・callHierarchy・rename・codeAction のテストも、先頭の要求が冷間になる点は同じ（hover が最初に落ちただけ）。

## 直し方
- `tests/unit/test_lsp_features.py` の `real` フィクスチャに `warm_up()` を足した。`ready` の後に本番で使う 2 文書（routes.py・payment.py）を `documents.sync` で開き、`diagnostics.wait_for` で各文書の最初の `publishDiagnostics`（= サーバーがその文書の解析を終えた合図、§7.5.2 の `ready` の本来の定義）を待ってから本番の要求に入る。期限は 60 秒（ここは待つ条件であって要求の期限ではない）。
- 要求の期限（`LSP_TIMEOUT_*`）の水増しや、要求の再試行はしていない。製品コードは変えていない。
- `test_real_code_action_for_a_type_error` は hover で文書を開いて `sent_at_of` を取る手順だが、`warm_up` で開いた版を `sent_at` が指すので、診断は受信済みで `wait_for` はすぐ返る（意味は変わらない）。

## 確認
- 同じ負荷（busy loop 60 本）の下で、`tests/unit/test_lsp_features.py -k real`（8 件。1 回が負荷なしの 24 秒から 60〜85 秒に伸びる）を 4 並列で 20 回: 失敗 0（修正前は `real_hover` 単体で 10 回中 7 回失敗）。
- `uv run pytest -q tests/unit/test_lsp_features.py tests/unit/test_lsp_pool.py` 78 件 green、`ruff check tests/unit/test_lsp_features.py` green。
