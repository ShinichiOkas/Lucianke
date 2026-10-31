# BUG-0058 F-PY・F-TS で T-06・T-07・T-08 のサーバー内 p95 が Fast Path の 50 ms を大きく超える（M1-3 不合格）

見たことだけを書く（調査していない）。2026-10-29 15:34〜15:48、M1-3 の受入計測（acc-M1-3）で観測。要約は `.pair-agent/acceptance/M1-3-latency-20261029-154219.md`。

## 症状
`python bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200`（母集団は成功かつ `is_degraded == false`、サーバー内 `timing_ms`、format=json、ウォームアップは LSP ready と索引ジョブ完了まで待つ）。同じ結果が 2 回出た（run1 = origin/main `5225ec1`、run2 = `605e79b`）。CPU はシステム全体で平均 24〜26%、最大 53.5% / 41.7%（50% 超は run1 の 1 秒だけ）。サーバー内 p95（ms、run1 / run2）:

| ツール | F-PY | F-TS | p50（F-PY / F-TS） | 最大（F-PY / F-TS） |
|---|---|---|---|---|
| T-06 `find_references_and_implementations` | 551 / 454 | 333 / 292 | 11〜12 / 8〜9 | 1296 / 2840（run1） |
| T-07 `query_call_hierarchy` | 842 / 883 | 1024 / 765 | 26〜27 / 188〜263 | 5297〜5540 / 1115（run1）〜836（run2） |
| T-08 `get_diagnostics` | 332 / 333 | 1015 / 937 | 10 / 13〜14 | 592 / 2423 |

- 遅い呼び出しはすべて `lsp_status: ready`（T-07 の F-TS は `indexing` の呼び出しも多い）、`confidence: exact`。p50 は 8〜27 ms（F-TS の T-07 だけ 188〜263 ms）で、尾が p95 を押し上げている。
- 遅い対象の例（run2）: T-06 F-PY `httpx/_models.py#Response.read` 1233 ms、F-TS `source/utils/type-guards.ts#isNetworkError` 2388 ms。T-07 F-PY `httpx/_client.py#Client.get` 5540 ms、`httpx/_models.py#Cookies.get` 4703 ms。T-08 F-PY `tests/__init__.py` 577 ms、F-TS `test/browser.ts` 2415 ms（`test/fetch.ts` 1644 ms）。
- F-POLY（参考、§14.4 の母集団外）でも T-07 の p95 が 401 / 408 ms（最大 5 s）。
- 合格したのは T-02（F-PY 7・25 / F-TS 7・8）、T-03、T-04（F-PY 27 / F-TS 18〜20）、T-05、T-19、T-20（いずれも 50 ms 未満）。

## 再現
`uv run python bench/fetch_fixtures.py` のあと、typescript-language-server が PATH にある状態で `uv run python bench/run_latency.py --fixtures F-PY,F-TS --tools find_references_and_implementations,query_call_hierarchy,get_diagnostics`。

## 追試（2026-10-30・HEAD `dccf78e`・別 PC）— 見たことだけ
`uv run python bench/run_latency.py --fixtures F-PY --tools find_references_and_implementations,query_call_hierarchy,get_diagnostics --calls 200`（各 200 回）。サーバー内 p95（judged = 成功かつ非降格）:

| ツール | p95 | p50 | min | max |
|---|---|---|---|---|
| T-06 `find_references_and_implementations` | 5923 ms | 486 ms | 11 ms | 8329 ms |
| T-07 `query_call_hierarchy` | 5852 ms | 154 ms | 6 ms | 10034 ms |
| T-08 `get_diagnostics` | 2026 ms | - | - | - |

- 分布は**二峰性**。速い群（`tests/...` 配下の末端シンボル）は 6〜40 ms、遅い群（`httpx/...` 本体の中核シンボル: `_client.py`・`_models.py`・`_urls.py`・`_decoders.py` 等）は 5000〜8000 ms。遅延は**対象シンボルのファンイン（被参照数）**に相関して見える。
- 遅い群は `confidence: exact`・`lsp: ready`。`EXACT_BUDGET_S=10.0` のタイムアウトに当たって structural 降格した群が max 側（8000〜10034 ms）。
- **狙い撃ちプローブ（`bench/probe_bug0058.py`。同一対象を各 12 回連続）では遅延が再現しなかった**（ほぼ全て structural・5〜100 ms、計時計装 `lsp_call_hierarchy_slow` は 0 件）。同一対象の反復で LSP/キャッシュが温まり、かつ structural に落ちて計時計装点（exact 経路の `CallEdgeSource`）を通らなかったため。→ 遅延は「**毎回別シンボルの exact 経路コールド解決**」で出るテール問題。
- `a04937c` の計時計装（prime/settle/prepare/calls/sites の段別内訳）は exact 経路に入った呼び出ししか計時しない。段別の主犯（prepare / calls / sites）は**未確定**（この追試では exact のコールドを狙い撃ちでログに残せていない）。

## 優先度
師匠判断（2026-10-30）: 初回分析に数分待てば済む性質なら優先度は高くない。深掘りは後回し。本票は実測の記録として残し、直す役は「毎回別シンボル＋exact 経路強制」で計時計装ログを拾うところから再開する。

## 仕様書
§14.4（Fast Path は p95 < 50 ms）、§3.2 NFR-PERF-1、§15.2 M1-3。

## 原因・直し方・確認（2026-10-31）

### 計測の条件
- 台本: `bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200 --tools find_references_and_implementations,query_call_hierarchy,get_diagnostics`（シード 20261029、母集団は成功かつ非降格、サーバー内 `timing_ms`）。前（`bfe71c3`）と後（`b850152`）を、それぞれ使い捨ての checkout から同じ条件で 2 回ずつ（前 A → 後 A → 前 B → 後 B の順）。
- 機材: Intel Core Ultra 7 165U（12 コア / 14 論理）/ Windows 11。**ほかの役が並行して走っており CPU は 85〜100%**（受入の機材〈48 論理〉より遅く、空いてもいない）。
- この PC には node が無いので、typescript-language-server 4.3.3 / TypeScript 5.6.3 を bun（`bun --bun …/cli.mjs --stdio`、`LUCIANKE_LSP_TYPESCRIPT_LANGUAGE_SERVER_CMD`）で動かし、ky には `bun install --ignore-scripts` で依存を入れた。basedpyright は 1.40.2。
- 14 ツールの全体の計測も前に 1 回流した（`bfe71c3`、TS の LSP なし）。T-06〜T-08 以外も 50 ms を超えたので BUG-0128 に書いた。

### 結果（サーバー内 p95、ms。前 A / 前 B → 後 A / 後 B）
| ツール | F-PY p95 | F-TS p95 | F-POLY p95（参考） | p50（F-PY / F-TS） |
|---|---|---|---|---|
| T-06 `find_references_and_implementations` | 585 / 454 → **75 / 127** | 617 / 591 → **144 / 131** | 5150 / 5067 → **20 / 20** | 24・13 / 18・14 → 13・25 / 12・11 |
| T-07 `query_call_hierarchy` | 540 / 254 → **146 / 147** | 456 / 276 → **108 / 106** | 10033 / 5111 → 568 / 564（BUG-0129） | 42・23 / 140・85 → 22・33 / 37・30 |
| T-08 `get_diagnostics` | 356 / 336 → **269 / 263** | 774 / 717 → 726 / 746 | 2026 / 2011 → **10 / 18** | 30・12 / 22・12 → 7・10 / 10・17 |

後 A / 後 B は `b850152`。その後に入れた 6.（`07b2c06`）・6b.（`0965fec`）は F-TS だけ計り直した（下の「後 C / 後 D」）。

「冷」はその実行で初めての対象（Lucianke も LSP も答えを持っていない）、「温」は 2 回目以降。200 回のうち初めての対象は F-PY 164〜179、F-TS 14〜82、F-POLY 7〜11（台本の対象の数で決まる）。

| ツール・フィクスチャ | 冷 p95（前 A / 前 B → 後 A / 後 B） | 温 p95（前 A / 前 B → 後 A / 後 B） |
|---|---|---|
| T-06 F-PY | 609 / 572 → 90 / 152 | 34 / 17 → **8 / 16** |
| T-06 F-TS | 3223 / 2165 → 1358 / 1276 | 32 / 28 → **20 / 20** |
| T-07 F-PY | 594 / 277 → 160 / 167 | 66 / 38 → **9 / 14** |
| T-07 F-TS | 1439 / 855 → 653 / 782 | 434 / 273 → 107 / 73 |
| T-08 F-PY | 574 / 566 → 571 / 564 | 81 / 24 → **14 / 23** |
| T-08 F-TS | 769 / 741 → 740 / 771 | 1025 / 236 → 283 / 695 → 後 C（`07b2c06`）208 → 後 D（`0965fec`）**18** |

温まった答えは、T-07 の F-TS（後 D で p50 30・p95 115 ms）と T-07 の F-POLY（BUG-0129）を除いて 50 ms 未満になった（T-08 の F-TS は後 D で 18 ms）。後 D の F-TS の全体の p95 は T-06 130・T-07 123・T-08 718 ms。冷えた答えの残りは「原因」の 7.（LSP の側の下限）。

### 原因（各段の所要のログ〈この票で足した `lsp_exact_slow`〉と basedpyright・tsls の実装を読んで確かめた）
1. **T-06・T-07 が対象ごとに文書を開いて最初の診断を待っていた**（`await_first_diagnostic`。BUG-0012 の「起動直後の部分的な答え」を避けるため）。冷えた対象のたびに、その文書の basedpyright の型検査と、診断の無い文書では「空の最初の診断は仮かもしれない」の静止時間 300 ms を待っていた。basedpyright が診断を出さない文書（F-POLY の `receipt.py`）では毎回 5 秒（`FIRST_DIAGNOSTIC_WAIT_S`）。起動直後の部分的な答えは LSP 全体の状態で、文書ごとの状態ではない（T-07 の `_settle_path` の注にも同じことが書いてある）。
2. **T-06 の参照箇所の分類を問い合わせごとにやり直していた**。LSP の答えの参照元のファイルを毎回構文木にかけて `extract_relations` で囲み・使用種別を引いており、1. を除いた後の冷えた T-06 の所要の大半（参照元が数ファイルで 100〜200 ms）がこれだった。ファイルを 1 つずつ順に待っていた。
3. **T-08 が ruff の空の診断にも 300 ms の静止を待っていた**。ruff は文書を開くとその場で検査して 1 度で出し切る（S-04: 11 ms）のに、basedpyright 用の「最初の空は仮かもしれない」を全サーバーに当てていた。F-PY の T-08 の冷えた文書の 313〜329 ms はすべてこれ。
4. **T-08 が同じ内容の文書で、届かない診断を毎回上限（2 秒）まで待っていた**（F-POLY の `receipt.py` は basedpyright が診断を出さない）。
5. **0 件の exact の答えを覚えていなかった**（T-06 の参照・T-07 の木）。参照・呼び出し元の無いシンボル（テスト関数など）は同じ問い合わせのたびに LSP に聞き直していた。
6. **T-08 が tsls の診断の出し直しのたびに書き込みを積み、その書き込みを待っていた**。typescript-language-server はどの文書を開いても・要求のたびに、開いている文書の診断を全部求め直し、問題のある文書の診断を毎回出し直す。
6b. **T-08 が呼び出しのたびに QuickFix を問い合わせ直していた**（`include_code_actions` の既定は true）。問題のある文書の全診断について LSP に `codeAction` を送るので、内容の変わらない文書でも呼び出しのたびに 0.7〜2.4 秒（ky の `test/hooks.ts`。6. を直した後も残った）。
7. 残り（Lucianke の側では縮められない下限）:
   - **tsls の診断の遅延**: 文書を開いてから診断を求めるまで 300〜800 ms 待つ（行数 / 20 を 300〜800 に丸めた `requestDiagnostic` の遅延。4.3.3）。F-TS の冷えた T-08 の p95 740 ms はこれ。
   - **basedpyright の空の診断の仮・本物の区別が無い**（S-04）ので、問題の無い文書の最初の T-08 は 300 ms 待つ。basedpyright は codeAction・definition・didChange の後 250 ms 解析を遅らせる（`_userActivityBackoffTimeInMs`）ので、T-08 の既定（`include_code_actions: true`）で直前に codeAction を送っていると、次に開いた文書の診断はさらに遅れる。F-PY の冷えた T-08 の 550〜580 ms（小さな `__init__.py` など）はこの 2 つ。
   - **LSP の計算**: basedpyright の参照・呼び出し階層の走査（冷えた T-06 F-TS の 1.3 秒は tsserver が `test/` を開いてプロジェクトに入れる分を含む）。T-07 の深さ 2 は「1 + 呼び出し元の数」の要求を 1 本ずつ送る（`queue` の段。basedpyright は要求を 1 本ずつしか処理しない）。

### 直し方（決めたこと）
- 1: `DiagnosticsHub.analysed`（起動・再起動の後に `publishDiagnostics` を 1 通でも受けたか。`ready` は猶予時間でも立つので別に持つ）が立っていれば、`await_first_diagnostic` は開かず待たずに `True`（目印も置いてよい）。立つ前（起動直後）は今までどおり対象の文書で待つ。`7e66c64`
- 2: 分類の結果を `(ワークスペース, ファイル, 内容の sha256)` ごとにプロセスの中で覚えて問い合わせをまたいで使い回す（`_lsp_exact.SITES`、LRU 1024 ファイル。分類は内容だけで決まる）。T-06 は参照元のファイルを並行に分類する。`b850152`
- 3: `LspServerDef.provisional_empty`（既定 True）を ruff だけ False にし、`DiagnosticsHub.is_provisional` が見る。basedpyright（解析の途中で未検査の文書に空を出す）と tsls（診断の種類ごとに分けて出す）は残す。`b850152`
- 4: `DiagnosticsHub.gave_up`（同じ `min_version`・`sent_at` の待ちが時間切れで終わり、その後も揃っていない）なら T-08 は 0 ms で確かめるだけにし、warnings に「前の呼び出しで待っても届かなかった・待ち直していない」と書く。届けば・内容が変われば・閉じれば・起動し直せば元どおり待つ。`6600a02`
- 5: 索引にも参照・呼び出しが無い 0 件の答えも、解析の後なら目印を置く（読み戻した exact 行が 0 件のときだけ一致する。前の答えの exact 行が残っていれば一致せず覚えない）。`7e66c64`
- 6: 前の受信と同じ文書の内容（その後 LSP に何も送っていない）への、同じ版・同じ診断の再送は書き込まない（前の書き込みが済んでいて失敗していないときだけ。`DiagnosticsHub._is_resend`、送信時刻は `DocumentSync.sent_at_of`）。`07b2c06`
- 6b: QuickFix を `(サーバー, ハンドル, 起動の時刻, ファイル, 内容の sha256, 診断)` ごとに覚えて問い合わせ直さない（`t08._QuickFixCache`、LRU 4096。LSP が起動し直したら使わない。ID は台帳が同じものを返す）。`0965fec`
- 計時: T-06 の exact 経路（prime / settle / references / sites / write / confirm / marks）と T-08 の診断待ち（sync / wait、開いたか・seq・件数）にも各段の所要のログを足し、T-07 の `_ask` と同じ部品 `log_slow_spans`（event `lsp_exact_slow`、0.5 秒以上の呼び出しだけ）にまとめた。
- 基準: 冷えた exact は LSP そのものの所要が下限で 50 ms に入らない（7.）。50 ms に入れるには全シンボルの答えを先に取るしかないが、目印はどのファイルが変わっても外れる（`files_fingerprint`）ので、編集の続く使い方では効かずベンチの数字を作るだけになる。冷えた対象に構文木の答え（降格）を返して数字を通す案も、AI に推測を渡すだけなので採らない。仕様書 §17 に **OD-23**（冷えた exact と温まった答えに分けて判定する。推奨つき）を置き、§14.4 から参照した（`b732cfc`。決まるまで判定は今のまま）。

### 確認
- 試験: `tests/unit/test_lsp_diagnostics_hub_bug0058.py`（`analysed` の立ち下がり、`gave_up` の条件、T-06/T-07 の最初の診断の待ちが解析の後は文書を開かないこと、ruff の空の診断は静止時間を待たないこと、同じ内容への同じ診断の再送は書かないこと）、`tests/contract/test_T08_diagnostics.py` に QuickFix を問い合わせ直さないこと。関係する試験 `tests/contract/test_T06_T07_exact_marks.py`・`test_T06_T07_exact_budget.py`・`test_T06_T07_lsp.py`・`test_T06_T07_lsp_postconditions.py`・`test_T08_diagnostics.py`・`test_T13_two_servers.py`・`test_T13_code_action.py`・`test_T10_edit_body.py`、`tests/unit/test_lsp_exact_degrade.py`・`test_lsp_pool.py`・`test_lsp_documents_router.py`・`test_edit_transaction.py` と見張り 4 本（`test_import_graph`・`test_server_launch_guard`・`test_stdout_purity`・`test_leak_guard`）が green。全体テストは流していない（親が統合後に流す）。
- 計測は上の表。M1-3（今の判定: 全呼び出しの p95 < 50 ms）は **まだ不合格**。温まった答えは T-07 の F-TS・F-POLY（BUG-0129）を除いて 50 ms 未満になった。

### 残ったこと
- 判定の形は OD-23（師匠の判断待ち。推奨は (b)）。
- T-07 の F-TS の温まった答えが p50 30・p95 73〜115 ms（F-PY は 9〜14 ms）。PaaD から答えているはずの 2 回目以降で何に時間を使っているかは未確認。
- F-POLY の T-07 の `LimitTable.__init__`・`LimitTable.get` の 10 秒（BUG-0129 に票）。
- 恒久の改善は LSP 3.17 の pull 診断（`textDocument/diagnostic`。basedpyright は対応、tsls 4.3.3 は未対応）。空の診断の仮・本物の区別と tsls の遅延が要らなくなる。
- 14 ツールの全体で T-00・T-01・T-04・T-10・T-11・T-19 も 50 ms を超えた（BUG-0128 に票）。

## 新しい判定の形での計測（2026-10-31・OD-23 (b) の決定の後）

仕様書 §17 OD-23 を (b) に決め、§3.2 NFR-PERF-1・§14.4・§15.2 M1-3 を「LSP の exact 経路（T-06・T-07・T-08）は冷 p95 < 1 秒〈深さ 2 以上の T-07 は < 2 秒〉と
温 p95 < 50 ms の両方」に直し、`bench/run_latency.py` が冷・温に分けて判定するようにした（`3c015c9`）。その形で 14 ツール × 3 フィクスチャを 1 回流した
（`3c015c9`、typescript-language-server は bun 経由、`cpu_busy_pct` F-POLY 19.5・F-PY 21.1・F-TS 25.7。結果 JSON は作業ツリーの
`bench/results/20261031-180657-windows-latency.json`、push していない）。台本の判定は **不合格**。

| ツール | F-POLY | F-PY | F-TS |
|---|---|---|---|
| T-06（冷 p95 / 温 p95、ms） | 21 / 7 合格 | 48 / 8 合格 | **1234** / 22 **不合格**（冷 n=14） |
| T-07（冷 p95 / 温 p95、深さ 2） | 248 / 25 合格 | 116 / 9 合格 | 710 / **101** **不合格**（温 n=174） |
| T-08（冷 p95 / 温 p95） | 491 / 10 合格 | 533 / 11 合格 | 732 / 13 合格 |
| それ以外の 11 ツール（全呼び出しの p95 < 50 ms） | 全部合格 | T-11 `rename_symbol` 62 ms のほか合格 | T-11 66 ms のほか合格（T-13 は 49 ms） |

- 冷・温の件数（冷 / 温）: T-06 F-POLY 10 / 177・F-PY 164 / 36・F-TS 14 / 186、T-07 11 / 172・179 / 21・13 / 174、T-08 7 / 163・54 / 146・81 / 119。
- 不合格の 2 件は見たことだけを票にした（直していない）: BUG-0133（F-TS の T-07 の温 p95 101 ms・温の 174 回のうち 74 回が `lsp_status: indexing`）、
  BUG-0134（F-TS の T-06 の冷 p95 1234 ms。冷の対象は 14 件で、最も遅い `delay` の 1 件で決まる）。T-11 の 62〜66 ms は BUG-0128 の「残ったこと」。
