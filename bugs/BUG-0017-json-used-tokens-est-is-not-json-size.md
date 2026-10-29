# BUG-0017 format=json の budget.used_tokens_est が応答 JSON の実サイズの約 1/7（max_tokens 2000 に対し JSON 本文は約 14,000 トークン相当）

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状: 期待は仕様書 §5.7「`budget.used_tokens_est` には応答テキスト全体の推定値」と、§5.6-1「`max_tokens` を超えない範囲で詰める」。実際は `format: "json"` の `used_tokens_est`（1884）が、応答の JSON テキストを `TokenEstimator().count()` で数えた値（14192）の約 13%。json で呼ぶと max_tokens を大幅に超える応答が返る。
- 再現手順: `get_file_skeleton {"paths":["src/lucianke/core/urn.py","src/lucianke/core/errors.py"],"max_tokens":2000,"format":"json"}` → `budget: {"max_tokens":2000,"used_tokens_est":1884,"truncated":false}`、`page.returned: 2`、`content[0].text` を `lucianke.mcp.budget.TokenEstimator().count` で数えると 14192（`bench/dogfood/run_dogfood.py 5` の `json_used_vs_recount`）。
- 頻度: 1 件中 1。
- 備考: 仕様書が json の予算を「compact 相当の量」で数えると決めているなら、その旨が書かれていない（§5.7 は応答テキスト全体）。
- 調査はしていない（直す役の仕事）。

## 原因
- パッカーは形式を知らず、レコードを常に `Record.lines`（compact の行）で数えていた（`budget._record_text`）。json は `Record.lines` ではなく `Record.data` を描くので、T-02 のように `data` が入れ子の大きいレコード（symbols[]・urn・range・signature ...）では、描かれる JSON が compact の約 7 倍になる。
- 加えて、封筒（全キー・`_resolution_meta`・`page`・`budget`・`timing_ms`・`warnings`）と `data_static` は予算に数えられていなかった（BUG-0014 と同じ、`reserved_tokens` の渡し忘れ）。
- 仕様書 §5.7 は「応答テキスト全体」、`max_tokens` は「応答全体の上限」（§5.2）。json を compact 相当の量で数える決めはどこにも無かった。

## 直し方
- `Packer.pack` に `record_text`（1 レコードが描かれるときの本文）と `trailer_text`（続きがあるときに増える本文）を受ける引数を足した（既定は従来の compact）。json は `render/json_out.py` の `record_text`（`json.dumps(dict(rec.data))` + 区切りの `,`。レンダラと同じ書き方）と `trailer_text`（`next_cursor` の文字列だけ。json に要約の行は無い）を渡す。`mcp/fit.py` が形式ごとに選ぶ。
- 封筒は BUG-0014 の直しと同じ前置きの計測（0 件のページを json で描いて数える）で予算に入る。`budget.used_tokens_est` は描いた JSON 本文を数え直した値に合わせる。
- `Record.shrink` は compact の行で収める約束なので、json で数えて収まらない縮退版は、`ask`（compact の行の予算）を二分探索して json で収まる最大の縮退版を探す（`Packer._shrink`、最大 16 回。compact は従来どおり 1 回）。json の大きさは compact の行に比例しない固定分（`symbol` の全キー・`request`・`file_sha256` など 1 件約 280 トークン）があるので、比例で縮める呼び直し（初版、4 回）は収束せず、T-05 の json が 1 件も入らない「進まないページ」になった。二分探索にして `Record.resume`（BUG-0016）つきの縮退でも、カーソルの分を差し引いたあとの予算で収まる最大の版が選ばれる。
- 仕様書 §5.7 に「`used_tokens_est` は描いた応答テキスト全体の数え直しと一致する」「json はレコードを `data` の JSON の大きさで数える。同じ `max_tokens` で入る件数は json のほうが少ない。`structuredContent` は写しなので別に数えない」を追記。設計書 §7.2.2 に `fit.py` の手順と形式ごとの見積もりを追記。
- 同じ内容の json は compact の数倍になるので、json で小さい `max_tokens` を指定していた既存テストの予算を、実際の大きさに合わせて上げた（切り詰めが実際に起きる範囲か、全部入る予算にした）: `tests/contract/test_T05_bodies.py`（全文は約 670 トークン、封筒約 150 + 1 件の固定分約 280。切り詰め 300 → 600、カーソルで辿る 260〜450 → 520〜640、3 対象 300 → 580、compact の注記は compact の切り口を自分で読む）、`test_T02_data_files.py`・`test_T03_T04.py`・`test_T06_T07.py`・`test_T08_diagnostics.py`・`test_T19_T20.py`・`test_perf_reuse.py`（json の既定や 200・300 の予算 → 全部入る、または数件ずつのページになる予算）、`tests/unit/test_mcp_budget_cursor.py`（巨大レコードの `data` も大きくした）。
- 回帰テスト: `tests/unit/test_mcp_response_budget.py`（BUG-0014 と共通。json は入れ子の `data` が compact の約 5 倍のレコードで、入る件数が compact の 1/3 以下、全文の数え直し ≤ `max_tokens`、`used_tokens_est` = 数え直し、カーソルで辿って全件、`Packer` の `record_text` / `trailer_text` / `shrink` の呼び直し）。

## 確認
- 再現手順（T-02、2 ファイル、`max_tokens: 2000`、json）: 直す前は申告 1884 に対し本文 14192。直した後は本文を数え直した値が申告と一致し、2000 以下（入るのは 1 ファイルぶん以下になり、残りは `next_cursor`）。
- 残り（別の票）: dot / mermaid（BUG-0053。グラフは `data_static["graph"]` を丸ごと描くので、レコードの詰めでは予算を守れない）。json で前置き・固定部だけで `max_tokens` を超える応答（BUG-0054）。T-02 の json が compact の約 3 倍で、既定の `max_tokens` 2000 では中規模のファイルが 1 件も入らない（BUG-0055）。
- コミット: 605e79b（push 済み。使い捨て作業ツリーで tests/unit と tests/contract は green。残る赤は tree-sitter-go を入れない環境の tests/unit/test_edit_normalize.py の 2 件のみで、この変更の前から同じ）
