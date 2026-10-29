# BUG-0027 F-PY（httpx）で get_file_skeleton と search_workspace_symbols のサーバー内 p95 が 50 ms を大きく超える

見たことだけを書く（調査していない）。2026-10-29 14:30 頃、bench/run_latency.py（18b）で観測。CPU 100% の負荷あり（参考値）。

## 症状
`bench/.cache/httpx`（F-PY, SHA b5addb64）を cwd+roots にして、ツールごとに 200 回（シード固定、Tier 1 の .py からランダム）、format=json、逐次に呼んだ `timing_ms`（サーバー内）の p95:
- `get_file_skeleton`: 835 ms（F-POLY では 5 ms）
- `search_workspace_symbols`: 701 ms（F-POLY では 3 ms）
どちらも warm-up（`query_project_db` の files 件数が安定）後。NFR-PERF-1 は p95 < 50 ms。
その前の 20 件の試走（F-POLY）では `get_project_db_schema` が p50 290 ms / p95 445 ms（索引を作っている最中の可能性あり。本走の F-POLY では 12 ms）。

## 再現
`uv run python bench/fetch_fixtures.py F-PY` のあと `uv run python bench/run_latency.py --fixtures F-PY --tools get_file_skeleton,search_workspace_symbols`。

## 原因
調べた結果、原因は 3 層に分かれていた（負荷の影響を除くため、プロセス内でパイプラインを組んで cProfile と `perf_counter_ns` の区間計測で内訳を取った）。

1. **計測の準備待ちの欠陥（bench）**。`bench/run_latency.py` のウォームアップは「`files` の件数が 3 回続けて同じ」で索引の完了と見ていた。索引器は 50 ファイルごとにコミットするので、CPU が飽和して次のバッチが遅いと「50 件で安定」に見え、**索引が途中のまま計測に入っていた**（見えた `warmup.index.files: 50`、実際は 126）。その結果、検索は索引にまだ無いシンボルで 0 件 → Tier 4 の grep（降格応答、約 44 ms）に落ち、索引器と GIL を取り合う間はスケルトンも 100〜200 ms の外れ値が出た。検索はさらに、テスト側のシンボルを既定の `include_tests=false` で探して 0 件になる対象を選んでいた。→ 判定の母集団（降格でない応答）が空 = `no_samples`。
2. **呼び出しごとの作り直し（サーバー）**。骨格（`httpx/_client.py` 2,019 行）は、構文木が `TreeCache` に載っていても、毎回 シンボル抽出 9.5 ms + 骨格の構築 6.4 ms + 予算超過時の縮退の見積もり 13 ms（段ごとに json の `symbols` まで作っていた）+ トークン見積もり 12 回（60 KB の json を 1 回 8〜14 ms。同じ本文を呼び出しの中で何度も、呼び出しをまたいでも数え直す）+ URN の整形/解析（1 回 18〜60 µs を 1 呼び出しで 1 万回超）を払っていた。計 約 50 ms（負荷なし）。
3. **検索の SQL とデータ取得**。(a) T-03 の `info_of` が候補のファイルごとに `file_by_path` を引いていた（ヒットが散らばる検索で SQL が 1 + ファイル数回。`request` で 37 文）。(b) `search_symbols_fts(fuzzy)` の `LEFT JOIN hits h ON h.rid = s.rowid` は、`ORDER BY` が付くと SQLite が `hits`（FTS の一致）を自動索引なしの総なめ（`SCAN h`）にし、シンボル数 × 一致数の比較になった（1,800 シンボル・206 一致で 25 ms。`ORDER BY` を外すと 2.5 ms）。(c) 0 件の grep（`lexical_scan`）が、単語の入っていないファイルでも行表（`LineIndex`）を作っていた（28 ファイルで 1.3 ms ずつ、計 約 40 ms）。

「他の役のテストで CPU が 100%」という前提は、1 のとき外れ値の幅を広げただけで、F-POLY（5 ms・3 ms）との 100 倍の差は 1 と 3(c) が説明する（F-POLY は Tier 4 のファイルが少なく、50 ファイル未満で索引がすぐ終わる）。

## 直し方
- bench: ウォームアップは `configure_workspace {action: "status"}` の `jobs[]` に走っている索引ジョブが無くなるまで待つ。検索の対象は `include_tests: true`（テスト側のシンボルで 0 件にならない）。
- `syntax/memo.py`（新規 `BoundedMemo`・`content_key`）: 内容の指紋（blake2b）をキーにした小さな LRU（件数と重みの上限つき。木は持たない）。
  - `symbols.extract_detailed`: 同じ内容・言語・クエリ・ワークスペース・パスは直近 64 ファイル分を使い回す（構文エラーのある木は覚えない）。
  - `skeleton.build_file_record`: 同じ内容・同じ `symbols`・同じ指定のレコードを使い回す（closure が木を握らないように、描画に要る値だけ取り出した）。縮退段の見積もりは行だけで作り（`want_json=False`）、段の計画は 1 回だけ、選ばれた段の json だけを作る。
  - `core/tokens.TokenEstimator.count`: 1 KB 以上の文字列を（長さ, ハッシュ）で覚える（本文は持たない）。
  - `core/urn.parse` / `format`: `lru_cache`（`Urn` は不変）。`index/queries._modifiers`: `modifiers_json` の解析を `lru_cache`。
- `index/queries.files_by_paths`（新規）: 500 件ごとの 1 問い合わせ。T-03 の `info_of` はこれを使う。
- `index/queries.search_symbols_fts(fuzzy)`: FTS の一致（`bm25` つき）と名前の部分一致（`bm25` なし、FTS の一致は `NOT IN` で除く）を `UNION ALL` で足してから並べる。旧い結合と同じ行を同じ順で返す（972 通りで差 0。単体テストに旧 SQL を基準として残した）。
- `syntax/heuristic.lexical_scan`: 単語がファイルに入っていなければ行表を作らずに戻る。
- テスト（時間の閾値は使わない）: `tests/contract/test_perf_reuse.py`（同じファイルの T-02 を繰り返すと Tree-sitter の解析・シンボル抽出・骨格の構築が各 1 回、覚えた結果は作り直した結果と同じ〈compact・json・縮退する小さな `max_tokens`〉、内容が変われば作り直す、構文エラーのある木は覚えない）、`tests/unit/test_perf_memo.py`（URN の使い回し・長い文字列を 1 回しか数えない・上限）、`tests/unit/test_perf_search.py`（ヒットが 40 ファイルに散らばっても SQL が 3 文以下、`files_by_paths` が 1 文・500 件ごと、旧い結合との結果の同一性、単語が無いと行表を作らない）。

## 確認
測定は負荷の揺れを避けるため、いずれも負荷が低いとき（`cpu_busy_pct` 19〜22%）の値。F-PY（httpx b5addb64）、プロセス内でパイプラインを組んだ 1 回あたりの経過時間（修正前 = 330a052 のソース、修正後 = 本コミット）。

| 対象 | 修正前 | 修正後 |
|---|---|---|
| `get_file_skeleton` `httpx/_client.py`（2,019 行）2 回目以降 | 49 ms | 2.7 ms |
| 同 60 ファイルを順に 3 周目の p95 | 45 ms | 2.8 ms |
| 同 1 回目（構文木なし）`_client.py` | 60 ms | 44 ms |
| `search_workspace_symbols` `request`（206 件）2 回目以降 | 37 ms | 12 ms |
| 同 `Client`（84 件） | 17.7 ms | 7.5 ms |
| 同 80 件の名前を順に 3 周目の p95 | 12.1 ms | 6.5 ms |

`bench/run_latency.py --fixtures F-PY --tools get_file_skeleton,search_workspace_symbols --calls 50`（実プロセス・stdio、直したウォームアップ。サーバー内 `timing_ms`）:
- 修正前（330a052 のソース）: 骨格 p95 10 ms / 検索 p95 8 ms（cpu 19%）。直す前のウォームアップでは 骨格 p95 74〜117 ms、検索は `no_samples`（全件 lexical_fallback、44 ms）。票の元の観測（骨格 835 ms・検索 701 ms）は CPU 飽和 + 索引の途中での計測。
- 修正後: 骨格 p95 13 ms / 検索 p95 9 ms（cpu 22%）。負荷 77% のときでも 骨格 p95 15 ms / 検索 p95 18 ms（どちらも判定は pass、`ready_ms` は索引完了待ちで 15 s 前後）。
- 注意: 50 回では p95 は上位 3 件で決まるので揺れる（`p99` の 29〜72 ms は索引器とは無関係の外れ値）。200 回の本走は M1-3 の台本に任せる。

`tests/unit` 全体・`tests/contract/test_T02*`・`test_T03_T04`・`test_T05_bodies`（BUG-0016 の作業中の変更を除く）・`tests/unit/test_import_graph.py` が green（`tests/unit/test_index_rank.py::test_repo_map_files_without_ranks_returns_zero_scores` は 330a052 でもこの変更なしで失敗している別件）。

## 残り（見たことだけ）
- 索引の初回走査の間（F-PY で quiet 時 4 s、負荷時 15 s）は、索引器とツールが GIL を取り合い、骨格・検索が数十〜百数十 ms になる。定常状態では出ない（NFR-PERF-1 は定常）。
- `fuzzy` の名前の部分一致（`instr(lower(name), lower(:q))`）は索引を使えず全シンボルの走査（1,800 シンボルで 0.5〜0.7 ms）。シンボル数が 10 万のワークスペースでは数十 ms になりうる（FTS5 の trigram か名前の索引列が要る）。
- 構文木の無い 1 回目の骨格は、解析 9 ms + 抽出 9.5 ms + 構築 6 ms が残る（2,000 行で約 44 ms）。`_collect_raises`（本体の総なめ）と `extract_detailed` の `SymbolRecord` の組み立てが次の候補。
