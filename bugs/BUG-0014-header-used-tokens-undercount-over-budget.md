# BUG-0014 ヘッダの ~used が TokenEstimator の数え直しより常に 28〜30 トークン小さく、max_tokens を超える応答がある

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状: 応答テキスト全体を `lucianke.mcp.budget.TokenEstimator().count()` で数え直すと、ヘッダの `~used/max tok`（json は `budget.used_tokens_est`）より常に 27〜30 大きい。期待は仕様書 §5.7「`used_tokens_est` には応答テキスト全体（ヘッダ・トレーラ含む）」、§5.6-4「ヘッダ・警告行・トレーラも予算に含む」。
- 再現手順（`get_file_skeleton`、`bench/dogfood/run_dogfood.py 3` が自動で測る）:
  - `paths` = 小さめの `.py` 15 本（`bench/dogfood/out/obs3.json` の `files`）、`max_tokens: 500`、カーソルで全ページ取得: 13 ページ中 4 ページが数え直しで 500 超（508・502・526・501）。ヘッダの値は 28 小さい（例: ヘッダ `~435/500`、数え直し 462）。
  - 同じ paths で `max_tokens: 200`: 2 ページとも数え直し 213 > 200（ヘッダ `~185/200`）。
  - `max_tokens: 2000`: 4 ページ中 0 ページが超過だが、全ページでヘッダが 28 小さい（`~1950` と 1978 など）。
  - 単純な `get_symbol_bodies`（1 件、トレーラ無し）でも `~199/3000tok`、数え直し 229。
- 頻度: 全応答（約 40 ページ中 40）で差あり。超過は max_tokens が小さいときに出る（500: 4/13、200: 2/2）。
- 調査はしていない（直す役の仕事）。

## 原因
- パイプラインが `Packer.pack(...)` に `reserved_tokens` を渡していなかった（既定の 0）。パッカーの契約は「ヘッダ・`head_kv`・呼び手が出す警告の分は呼び手が数えて `reserved_tokens` で渡す」（`Packer` の docstring、設計書 §7.2.2 の `reserved = est(header) + est(head_kv) + ...`）。トレーラとレコードはパッカーが数えていたので、差は応答ごとに一定の 1 行ぶん（ヘッダ約 28〜30 トークン。降格の警告行や警告があればその分も）で、ページが増えても広がらなかった。`tests/unit/test_mcp_render.py::test_render_output_within_budget_when_packed` は「0 件のページで描いた前置きを数えて reserved に渡す」形を試していたが、パイプラインにその処理が無かった。
- `_render_failure` の部分結果（`E_TIMEOUT`）も同じ（エラーのヘッダ・メッセージ・候補が数えられていなかった）。

## 直し方
- 新規 `src/lucianke/mcp/fit.py`（`pack_and_render`）。パイプラインの成功経路と部分結果の経路が、パッカーとレンダラの呼び出しをここ 1 か所に通す。
  1. 前置きを数える: レコード 0 件のページを同じレンダラで実際に描き（`~used` の桁数は最大の `max_tokens`、`timing_ms` も最大桁）、全文を数えて `reserved_tokens` にする。降格の警告行は残りの全レコードから集計した精度で出す（入ったレコードだけの集計より警告が短いことはない）。
  2. 詰める（`reserved_tokens` つきでパッカーを呼ぶ）。
  3. 描いた全文を `TokenEstimator` で数え直し、`max_tokens` を超えていれば（`returned` の桁数など見積もりの誤差）超過分を `reserved_tokens` に足して詰め直す（上限 8 回）。
  4. ヘッダの `~used` を数え直した値に置き換えて描き直し、値が動かなくなるまで繰り返す（数字は 1 文字ずつ数えるので、桁数が変わらなければ 1 回で収束）。数え直しは `timing_ms` を最大桁数（5 桁）にした全文で行う。json の `timing_ms` は全文に入るので、実際の処理時間で数えると同じ入力でも呼び出しごとに `used_tokens_est` が 1 トークン前後ゆれる（`tests/contract/test_perf_reuse.py` の「2 回目以降は 1 回目と同じ応答」が赤くなった）。
- 結果: compact の `~used`（= `Page.used_tokens_est`）は描いた全文の数え直しと**一致**（誤差 0）、json は `timing_ms` の桁数ぶん（最大 4 トークン）だけ上（実際の全文以上の上限で、処理時間によらず同じ値）。全文は `max_tokens` 以下。
- 仕様書 §5.7 と設計書 §7.2.2 に書き足した。`tests/unit/test_mcp_pipeline.py` の `FakePacker` は json 用の追加引数を受ける形にした。
- 回帰テスト: `tests/unit/test_mcp_response_budget.py`（新規 68 件）。compact と json × `max_tokens` 200・400・2000 × レコード 0・1・40 件 × 通常・`head_kv` と警告つき・降格の警告行つき。全文の数え直し ≤ `max_tokens`、ヘッダの `~used` = 数え直し、カーソルで辿ると重複も欠落も無くどのページも予算内、`E_TIMEOUT` の部分結果も予算内。

## 確認
- 直した後は、回帰テストの全ケース（compact × 3 予算 × 3 件数 × 3 形）でヘッダの `~used` = 全文の数え直し、全文 ≤ `max_tokens`（票の観察: 直す前は常に 28〜30 小さく、500 で 4/13 ページ・200 で 2/2 ページが超過）。
- 契約テストの予算を直した（正しい直しの帰結: ヘッダ・警告・封筒も予算に入るので、同じ `max_tokens` で入る量が減る）: `test_T02_data_files.py`（compact の最小の予算 200 → 230。ヘッダ・警告の約 30 トークン）。json の予算の調整は BUG-0017 に記載。
- 残り（別の票）: json で前置き・固定部だけで `max_tokens` を超える応答（BUG-0054）。dot / mermaid（BUG-0053）。
- コミット: 605e79b（push 済み。使い捨て作業ツリーで tests/unit と tests/contract は green。残る赤は tree-sitter-go を入れない環境の tests/unit/test_edit_normalize.py の 2 件のみで、この変更の前から同じ）
