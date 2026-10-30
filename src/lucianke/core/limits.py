"""時間・件数・サイズの上限定数（設計書 §6.4）。

仕様書 §12.3・§12.8 ほかの値を **ここにだけ** 置く。他の層はこのモジュールの定数を参照し、数値をベタ書きしない。
単位は名前の接尾辞で示す: ``_MS`` はミリ秒、``_S`` は秒、``_BYTES`` はバイト、``_CHARS`` は文字数。
超過時の挙動（降格して続行 / ``E_TIMEOUT`` に部分結果）は各ツールの ``ToolSpec`` に書く。
"""

from __future__ import annotations

from typing import Final

# --- 共通パラメータ・件数（仕様書 §5） -------------------------------------------------
# §5.2: max_tokens に固定の最小値は置かない（師匠の判断 2026-10-30）。固定下限は前置き量（accuracy_warning・
# head_kv・json 封筒）と無関係に決め打ちするため、実装で前置きが伸びると §5.7（used_tokens_est <= max_tokens）と
# 衝突して溢れる（BUG-0063）。下限は呼び出しごとに動的に決まる: 実際に詰めて前置きすら入らなければ、出口（mcp/fit.py・
# budget.py の 0 件経路）がその呼び出しに必要な max_tokens の実測値を warnings で返して呼び直させる（BUG-0015）。
# 公開 API はどんな正の max_tokens も受ける。これはツール作者が default_max_tokens を 0 や負に設定するのを登録時に
# 弾く絶対最小の床にすぎない（公開の下限ではない。封筒・ヘッダの最小が必ず 1 トークン以上要ることから 1）。
REGISTRY_MIN_DEFAULT_MAX_TOKENS: Final = 1  # ToolSpec.default_max_tokens の登録時の床（作者のミス検出。公開下限ではない）
BUDGET_FLOOR_FOR_PAGING: Final = 200  # 内部用（ページング検証の床）。公開の max_tokens の下限ではない。小刻みな cursor ページングのテストが使う
BATCH_MAX_ITEMS: Final = 20  # §11 T-02 / T-04 / T-05: paths[] / targets[] は 1〜20 件
CANDIDATES_MAX: Final = 10  # §5.3 / §5.8: E_TARGET_AMBIGUOUS の candidates[] の最大件数
SNIPPET_MAX_CHARS: Final = 200  # §6.1 規約 6 / §12.6 / §13.5: snippet・doc・when の最大文字数（超過は「…」）
SEARCH_SIGNATURE_MAX_CHARS: Final = 160  # §6.1 規約 6 / §11 T-03: compact の検索ヒットのシグネチャの最大文字数（超過は「…)」。BUG-0069）

# --- ファイル監視と増分同期（仕様書 §12.4） ---------------------------------------------
WATCH_STEP_MS: Final = 10  # §12.4-2 / S-06: awatch の step。単発の遅延はほぼ 2×step（50 では p95 ≈ 100 ms）
WATCH_DEBOUNCE_MS: Final = 1000  # §12.4-2 / S-06: awatch の debounce。パスが増え続ける間にまとめる最大時間
WATCH_BURST_HINT: Final = 100  # §12.4-4 / S-06: 1 窓がこの件数以上なら静まるまで窓を合算する
WATCH_BURST_QUIET_S: Final = 0.3  # §12.4-4 / S-06: 合算を打ち切る静穏の長さ（秒）
WATCH_BURST_FILES: Final = 500  # §12.4-4: 1 バーストでこの数以上が変わったら差分索引に切り替える
WATCH_EXPECT_TTL_S: Final = 10.0  # 設計書 §7.3.6-8: expect() の有効期間（秒）
MAX_FILE_BYTES: Final = 5 * 1024 * 1024  # §12.4-5: これを超えるファイルは構文木を作らない（設定で変更可）
INDEX_REFLECT_MS: Final = 500  # §12.4-3 / NFR-PERF-3: 単一ファイルの変更が PaaD と LSP に反映されるまで

# --- 構文木キャッシュ（仕様書 §12.8、設計書 ADR-13） --------------------------------------
TREE_CACHE_MAX_FILES: Final = 2000  # §12.8: 構文木キャッシュのファイル数上限（LRU）
TREE_CACHE_MAX_BYTES: Final = 512 * 1024 * 1024  # §12.8: 同、バイト数上限

# --- LSP プール（仕様書 §12.3） ---------------------------------------------------------
LSP_TIMEOUT_DEFINITION_MS: Final = 3000  # §12.3: 要求のタイムアウト（定義）
LSP_TIMEOUT_REFERENCES_MS: Final = 3000  # §12.3: 同（参照）
LSP_TIMEOUT_HOVER_MS: Final = 3000  # §12.3: 同（ホバー）
LSP_TIMEOUT_CALL_HIERARCHY_MS: Final = 5000  # §12.3: 同（callHierarchy）
LSP_TIMEOUT_CODE_ACTION_MS: Final = 5000  # §12.3: 同（codeAction）
LSP_TIMEOUT_RENAME_MS: Final = 10000  # §12.3: 同（rename）
LSP_RESTART_BACKOFF_S: Final = (1, 2, 4)  # §12.3: クラッシュ時の再起動の指数バックオフ（秒）
LSP_RESTART_MAX: Final = 3  # §12.3: 再起動の最大回数。超えたら unavailable
LSP_OPEN_DOCS_MAX: Final = 200  # §12.3: didOpen 済み文書の LRU 上限。超えた分は didClose
LSP_IDLE_SHUTDOWN_S: Final = 1800  # §12.3: 要求が無い LSP を shutdown して standby に戻すまで（30 分）

# --- 編集（仕様書 §5.9、第 10 章、§13.4） ------------------------------------------------
EDIT_LOCK_WAIT_MS: Final = 5000  # §5.9: ワークスペースの書き込みロックの待ち上限。超過は E_TIMEOUT
DIAGNOSTICS_WAIT_DEFAULT_MS: Final = 2000  # §10.7 / T-10: diagnostics_wait_ms の既定
DIAGNOSTICS_WAIT_MAX_MS: Final = 10000  # §10.7 / T-10: 同、最大
DIRECT_REFERRERS_MAX: Final = 20  # §10.7 / T-10 手順 3: 診断差分の対象にする直接の参照元ファイルの上限
EDIT_MAX_FILES: Final = 500  # §13.4: 1 トランザクションで変更できるファイル数の上限
NEW_CODE_MAX_BYTES: Final = 1024 * 1024  # §12.8: new_code の長さ上限（超過は E_INVALID_ARGUMENT）
JOURNAL_KEEP: Final = 50  # §4.5: ジャーナルを残す直近の編集トランザクション数

# --- クエリ・検索（仕様書 T-03、T-20、§13.2、§13.6） --------------------------------------
QUERY_TIMEOUT_DEFAULT_MS: Final = 300  # T-20: timeout_ms の既定
QUERY_TIMEOUT_MAX_MS: Final = 5000  # T-20: 同、最大（範囲 50〜5000）
QUERY_ROWS_DEFAULT: Final = 200  # T-20: limit の既定
QUERY_ROWS_MAX: Final = 2000  # T-20: 同、最大
QUERY_LENGTH_MAX: Final = 64 * 1024  # §12.8: query の長さ上限（超過は E_INVALID_ARGUMENT）
QUERY_CTE_DEPTH_MAX: Final = 10000  # §13.2: 再帰 CTE の深さ上限
GREP_MATCHES_MAX: Final = 10000  # T-20 手順 3: temp_grep_matches のマッチ上限
REGEX_SCAN_MS: Final = 200  # T-03 手順 1: regex 走査の上限。超過は部分結果つき E_TIMEOUT
REGEX_PATTERN_MAX_CHARS: Final = 1000  # 設計書 §9.4（仕様書 §13.6）: 正規表現パターンの長さ上限
SELECTOR_EVAL_MS: Final = 1000  # §13.6: XPath / JSONPath / jq の評価時間上限
DEGRADED_GREP_MS: Final = 2000  # 親の指示（設計書 §6.4）: 降格時の単語境界 grep の時間上限
STRUCTURAL_SEARCH_MS: Final = 5000  # 親の指示（設計書 §6.4）: 構文パターン検索の時間上限

# --- 並行・要求・終了（仕様書 §12.5、§12.8、設計書 §6.1） -------------------------------
READER_POOL_SIZE: Final = 8  # 設計書 ADR-7 / §6.1: SQLite 読み取り接続プール・読み取りスレッドプールの既定数
MAX_CONCURRENT_READS: Final = 16  # §12.8: 同時実行の読み取り要求の上限。超過は FIFO で待機
READ_REQUEST_TIMEOUT_MS: Final = 10000  # §12.8: 1 要求の処理時間（読み取り）
EDIT_REQUEST_TIMEOUT_MS: Final = 30000  # §12.8: 同（編集）
SHUTDOWN_GRACE_MS: Final = 500  # §12.5: 終了時に子プロセスへ猶予を与える時間。超えたら強制終了

# --- Git（仕様書 §9.3） ------------------------------------------------------------------
CO_CHANGE_WINDOW: Final = 500  # §9.3: git_co_changes.window_commits の既定（直近コミット数）
