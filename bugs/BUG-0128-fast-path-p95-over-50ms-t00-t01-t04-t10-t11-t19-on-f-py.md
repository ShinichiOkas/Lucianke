# BUG-0128 14 ツールの計測で T-00・T-01・T-04・T-10・T-11・T-19 も F-PY で p95 50 ms を超える（負荷あり）

見たことだけを書く（調査していない）。2026-10-31 16:03〜16:14、BUG-0058 の直す役が今の main（`bfe71c3`）で `uv run python bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200` を流したときに見た。結果 JSON は直す役の作業ツリーの `bench/results/20261031-160333-windows-latency.json`（push していない）。

## 症状
- 機材: Intel Core Ultra 7 165U（12 コア / 14 論理）/ Windows 11。**ほかの役が並行して走っており、システム全体の CPU は 85〜100%**（計測の記録 `cpu_busy_pct` 84.5）。完全に空いた状態の計測ではない。
- この PC には node が無く、F-TS は typescript-language-server なしで走った（T-04・T-06・T-07・T-08・T-11・T-13 は母集団 0 か skip）。
- サーバー内 `timing_ms` の p95（ms。p50 / p95 / max）。BUG-0058 の T-06・T-07・T-08 以外で 50 ms を超えたもの:

| ツール | F-POLY | F-PY | F-TS |
|---|---|---|---|
| T-00 `configure_workspace`（`action: status`） | 16 / 18 / 34（合格） | **201 / 346 / 503** | **165 / 267 / 370** |
| T-01 `get_repo_map` | 19 / 25 / 36（合格） | **71 / 126 / 245** | **43 / 62 / 258** |
| T-04 `resolve_symbols` | 12 / 26 / 654（合格） | **31 / 77 / 1319** | 母集団 0 |
| T-10 `edit_symbol_body`（dry_run の delete_symbol） | 14 / 20 / 30（合格） | **34 / 67 / 156** | **17 / 51 / 59** |
| T-11 `rename_symbol`（dry_run） | skip（E_LSP_UNAVAILABLE） | **79 / 215 / 490** | skip |
| T-19 `get_project_db_schema` | **51 / 386 / 733** | **73 / 373 / 709** | **33 / 70 / 399** |

- T-19 は引数なしの同じ呼び出しを 200 回繰り返しているのに、F-POLY の 1 回ごとの値が 20 ms 前後の区間と 200〜700 ms の区間を行き来した（最初の約 40 回と 100〜150 回目付近が遅い）。2026-10-29 の受入（M1-3、48 論理の機材）では T-19 は 10〜22 ms だった。
- T-00 の `status` は副作用の無い照会で、F-POLY では 18 ms なのに F-PY・F-TS では p50 が 165〜201 ms。
- stdout の純度・子プロセスの監視は違反なし。

## 再現
上の計測をもう一度流す（ほかの役が走っている状態）。各ツールの 1 回ごとの値は結果 JSON の `runs[].tools.<tool>.raw.samples[].server_ms`。

## 仕様書
§14.4（Fast Path は p95 < 50 ms）、§3.2 NFR-PERF-1、§15.2 M1-3。関連: BUG-0058（T-06・T-07・T-08）、BUG-0059（台本に 5 ツールを足した票）。

## 原因・直し方・確認（2026-10-31）

### 切り分けの方法
- 本番と同じ結線（`RuntimeHub` + `LspPool(on_diagnostics=hub.on_diagnostics)` + `WiredServices` + `Pipeline`、全ツール登録）をプロセス内に組み、
  台本と同じ引数の作り方（`bench/run_latency.py` の `PLANS`・同じ種）で同じ呼び出しを繰り返した。見たのは 3 つ: (1) 区間の時間（関数を包んで計る）、
  (2) 呼び出しの間の全スレッドのスタックを 1 ms ごとに採った標本、(3) `asyncio.to_thread` をその場で呼ぶようにしてイベントループのスレッドだけで
  回した cProfile。
- 負荷の影響は、14 論理 CPU を Python の空回し 12〜16 本で埋め（システム全体で CPU 100%）、**同じプロセスで直した版と直す前の版を交互に**呼んで比べた
  （他の役の負荷は時々で変わるので、別々の時刻の p95 は比べていない）。

### 原因（ツールごと）
- **T-19（負荷と無関係に見えた二峰性）**: `COUNT(*)` の上限（0.1 秒）を守るのに、表ごとに `threading.Timer` を作って start・cancel・join していた
  （1 回の呼び出しで 10 本のスレッドを起こして終わりを待つ）。CPU が埋まると、スレッドの起動・終わりの待ちのたびに OS の順番待ちになり、
  同じ呼び出しが 20 ms 前後と数百 ms を行き来した（遅い区間は他の役の負荷の波と重なる）。キャッシュの失効・書き手との競合・GC・前景の譲り
  （`core/foreground.py`）ではない（台本と同じ順の 13 ツールをプロセス内で流しても、CPU が空いていれば T-19 は 200 回で p50 16 / p95 22 / max 52 ms。
  遅い呼び出しの間の他のスレッドの標本にも、索引・書き手・GC は出ない。CPU を埋めたときだけ再現する）。`_read_schema` だけを F-PY の索引で交互に 60 回:

  | | 静か p50 / p95 | CPU を 16 本で埋めて p50 / p95 |
  |---|---|---|
  | 表ごとの Timer（以前） | 13.8 / 17.3 | 121.9 / 1028.4 |
  | Timer なし | 8.2 / 11.0 | 16.0 / 20.2 |
- **T-00 `status`（F-POLY 18 ms に対し F-PY・F-TS 165〜201 ms）**: 時間のほとんどは Git の状態（`dulwich` の `porcelain.status`）。作業ツリーを歩き、
  設定のスタックを 3 回組み（Windows では PATH から `git.exe` を探す）、`.gitignore`・索引・パックを読む（1 回で Python の関数呼び出し約 9 万・ファイルを
  開く 40 回・`stat` 約 150 回・`scandir` 17 回）。F-POLY は `tests/fixtures/poly` が Git のルートでないので 0 ms。区間（F-PY、プロセス内、静か）:
  `_git_info` 38〜63 ms・言語別ファイル数とキャッシュ統計 2〜6 ms・LSP の状態 0.1 ms・ジョブ 0 ms（LSP への問い合わせ・python_env の解決・ファイルの
  数え上げではない）。`_git_info` だけ: 静か p50 29.8 / p95 41.6 ms → CPU を 16 本で埋めて 132.9 / 212.0 ms（票の 165〜201 / 267〜346 ms と同じ桁）。
- **T-01**: 静かでも p50 35 / p95 71 ms と枠に近く、負荷で約 2 倍。cProfile: ファイルごとに全シンボル（F-PY で 1 回 687 件）を `SymbolRecord` にしてから
  数件を選び、親は別の文で読み直し、予算に合わせて件数を減らす `reselect`（F-PY で 1 回 39 回）のたびに選び直していた（`str(urn)` が 1 回 4,000 回以上）。
  概観の入口のモジュールのために `pyproject.toml` を 2 回読んでいた。
- **T-04**: 静かなプロセス内で p50 15〜16 / p95 28〜75 ms。尾は exact の持ち上げ（LSP の hover・definition）の冷えた対象と負荷。手元では、タグクエリの
  ファイル（`queries/<言語>/tags.scm`、パッケージに同梱）を呼び出しのたびに開いて読んでいた（T-10・T-11 も同じ）。
- **T-10**: 静かなプロセス内で p50 15〜17 / p95 26〜29 ms（合格）。小さな I/O（ファイルの読み・`sha256`・`realpath`・LSP の起動コマンドを探す `stat`）と、
  書き換えた後の構文の確かめの解析。負荷で約 2 倍になるだけで、尾を作る処理は無かった。
- **T-11**: 尾は LSP（basedpyright）の rename の所要（プロセス内 50 回の区間: rename 5〜1120 ms、冷えた対象で 50〜500 ms）。手元の処理は `_analyse`
  （書き換えの前と後の内容を解析してシンボルを比べる）2〜80 ms で、書き換える前の内容は抽出の覚えに当たるのに毎回解析し直していた。

### 直し方（決めたこと）
- T-19: 表ごとの `Timer` をやめ、プロセスで 1 本を共有する見張り（`_Interrupter`）にした。期限を登録するだけで、見張りを起こすのはいま眠っている期限より
  早い期限のときだけ。`interrupt()` は錠の中で、まだ登録されている期限にだけ送る（`disarm` の後に届かない）。`7925923`
- T-00: Git の状態を、前に調べてからルートの下が変わっていなければ覚えから返す（`_git_state`）。鍵は監視が届けたイベントの数（`Watcher.generation`。
  `.git/` の中・除外で落とすものも数え、このサーバー自身の書き込みは編集トランザクションが書いた後の `expect` / `forget` でイベントより先に数える）と
  `.git/HEAD`・`.git/index` の更新時刻と大きさ。監視が動いていない（`polling`・停止後）とき・`.git` がディレクトリでない（リンクされた作業ツリー・
  サブモジュール。実体がルートの外で監視に入らない）ときは毎回調べる。覚えは 10 秒（`GIT_STATE_MAX_AGE_S`）で切れる（監視がイベントを取りこぼす
  場所・ルートの外の設定の変化でも、それより古い答えは返さない）。索引の版（`data_version`）は鍵に入れない（入れると初回の索引の間に進み具合を見る
  `status` のたびに調べ直す。BUG-0112 の場面）。言語別ファイル数・キャッシュ統計は束の読み取りプールの接続を借り（空かなければ自分で開く）、
  Git と 1 つのスレッドで調べる。`dd6ebaf`・`30de119`
- T-01: 選ぶのに要る列だけ（`queries.symbol_outline`。urn・修飾名・種別・修飾子・行・親を 1 文で）読んで選び、全部の列の `SymbolRecord` は表示の候補
  （上位 `max_symbols_per_file` 件）だけ `queries.symbols_by_urns` で引く。選びはファイルごとに 1 度だけ作り（`_ranked_pool`）、`reselect` は上位 k 件を
  取るだけ（`_select_top`）。入口のモジュールは 2 つの概観で 1 度だけ求める（`_overview.entry_modules`）。`24fba31`・`ec5f2ce`
- 共通（T-04・T-10・T-11）: `load_query_source` を言語ごとに 1 度だけ読む（`functools.cache`）。`ae26ef4`
- T-11: `_symbols_of` は抽出の覚え（`symbols.remembered`。構文エラーの無い木の結果だけ）に当たる内容を解析し直さない。`cc9e57e`
- T-10: 構文の確かめ（`edit/syntax_check._analyze`）は同じ内容・言語の解析を覚えて使い回す（32 件・内容の合計 16 MiB、期限で打ち切った解析は覚えない）。
  前の内容は、同じファイルの前の編集の後の内容（続けて編集する）か前の呼び出しの前の内容であることが多い。`4eb054b`
- 覚え（T-00）は「同じ状態なら同じ答え」の照会だけに使った。T-01・T-19 は索引の変化を確かめる安い鍵が無い（`data_version` を進めない書き込みがある）
  ので覚えず、1 回の処理を軽くした。

### 確認
プロセス内（F-PY、ms）。「CPU 100%」の列は同じプロセスで前の版と後の版を交互に呼んだもの。「静か」の列の T-00・T-01 は別々の時刻の 100 回:

| ツール | 静か p50 / p95（前 → 後） | CPU 100%（空回し 16 本）p50 / p95（前 → 後） |
|---|---|---|
| T-00 `status` | 36 / 48 → **3 / 4** | 76.6 / 112.1 → **4.4 / 4.8**（覚えを切った回〈毎回 Git を調べる〉と交互に 80 回ずつ） |
| T-19（`_read_schema`） | 19.1 / 39.8 → **11.7 / 17.1** | 31.4 / 1306.6 → **16.7 / 19.0**（交互に 60 回ずつ） |
| T-01（cProfile、1 回の平均） | 57.3 → 43.5 → **26.4 ms**（プロファイラなし p50 / p95 35 / 71 → 21 / 27） | — |

`uv run python bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200`（サーバー内 `timing_ms`、ms。p50 / p95 / max）。票の計測（`bfe71c3`、
`cpu_busy_pct` 84.5）と比べる。この PC には node が無く、F-TS の TS の LSP の項目（T-04・T-11）は母集団 0 か skip:

| ツール | F-POLY 票 → 後 | F-PY 票 → 後 | F-TS 票 → 後 |
|---|---|---|---|
| T-00 `configure_workspace` | 16 / 18 / 34 → 4 / 7 / 13 | 201 / 346 / 503 → **1 / 3 / 5** | 165 / 267 / 370 → **1 / 1 / 2** |
| T-01 `get_repo_map` | 19 / 25 / 36 → 7 / 10 / 12 | 71 / 126 / 245 → **18 / 24 / 75** | 43 / 62 / 258 → **10 / 12 / 62** |
| T-04 `resolve_symbols` | 12 / 26 / 654 → 4 / 9 / 102 | 31 / 77 / 1319 → **11 / 26 / 85** | 母集団 0 |
| T-10 `edit_symbol_body` | 14 / 20 / 30 → 5 / 7 / 9 | 34 / 67 / 156 → **10 / 20 / 27** | 17 / 51 / 59 → **5 / 18 / 24** |
| T-11 `rename_symbol` | skip → 25 / **89** / 139 | 79 / 215 / 490 → 27 / **57** / 255 | skip |
| T-19 `get_project_db_schema` | 51 / 386 / 733 → **4 / 7 / 8** | 73 / 373 / 709 → **8 / 13 / 15** | 33 / 70 / 399 → **7 / 13 / 17** |

- 「後」の全体の計測（`bench/results/20261031-172851-windows-latency.json`、作業ツリーは `ec5f2ce` に `cc9e57e` の変更が入った状態）は、他の役の負荷が
  軽い時間帯だった（`cpu_busy_pct` F-POLY 25.8・F-PY 21.3・F-TS 20.2）。票の計測（84.5）と負荷が違うので、上の表の前後は負荷の差を含む。
  負荷の下の確かめは、同じ 6 ツールを CPU を空回し 12 本で埋めて流した（下）。
- 負荷の下（`cc9e57e`、14 論理 CPU を空回し 12 本で埋めて `--tools` で 6 ツール、`cpu_busy_pct` F-POLY 85.4・F-PY 85.5・F-TS 85.4。票と同じ程度の負荷。
  `bench/results/20261031-173950-windows-latency.json`。T-10 の構文の確かめの覚え〈`4eb054b`〉の前）:

  | ツール | F-POLY | F-PY | F-TS |
  |---|---|---|---|
  | T-00 | 8 / 8 / 9 | **2 / 3 / 69** | 2 / 3 / 4 |
  | T-01 | 13 / 14 / 15 | **34 / 36 / 202** | 21 / 23 / 268 |
  | T-04 | 7 / **66** / 362 | 32 / **71** / 201 | 母集団 0 |
  | T-10 | 8 / 11 / 66 | 22 / **55** / 130 | 11 / 36 / 83 |
  | T-11 | 90 / **298** / 7648 | 154 / **236** / 2966 | skip |
  | T-19 | 7 / 8 / 9 | 17 / 19 / 69 | 16 / 17 / 18 |

  T-00・T-19 は負荷の下でも 50 ms を大きく下回り、T-01 も枠に入った。T-04・T-10（F-PY）・T-11 は負荷の下で超える（「残ったこと」）。
- T-10 の構文の確かめは、編集の前の内容の解析を覚えて使い回す（`4eb054b`。cProfile で 1 回 16.3 → 13.8 ms）。上の負荷の下の計測には入っていない。
- 試験: `tests/unit/test_t19_count_interrupter_bug0128.py`（期限を過ぎた count は止まり `max(rowid)` に落ちる・期限内に終わった後に `interrupt` が
  届かない・呼び出しのたびにスレッドを起こさない）、`tests/unit/test_t00_git_state_memo_bug0128.py`（変わらなければ使い回す・イベント／ステージ／
  束の作り直しで調べ直す・自分の書き込みが `generation` を進める・監視が動いていない／`.git` がファイルなら毎回調べる・期限で切れる・本物の監視で
  新しいファイルが `dirty_files` に出る）、`tests/unit/test_t11_symbols_memo_bug0128.py`・`test_edit_syntax_check_memo_bug0128.py`（同じ内容は 1 度だけ
  解析・壊す編集は覚えからでも失敗・期限で打ち切った解析は覚えない）。関係する試験 `tests/contract/test_T00_configure.py`・
  `test_T01_repo_map.py`・`test_T10_edit_body.py`・`test_T10_insert.py`・`test_T11_rename.py`・`test_T11_degrade.py`・`test_T11_rename_real.py`・
  `test_T19_T20.py`、`tests/unit/test_edit_syntax_check.py`・`test_edit_transaction.py`、
  `tests/contract_fixtures/test_fx_db_T19_T20.py`・`test_fx_read_T00_T08.py`（T-01）、`tests/unit/test_workspace_watcher.py`・
  `test_tools_polling_watcher.py`・`test_tools_runtime.py`・`test_tools_repo_map_private_parents.py`・`test_tools_overview.py`・
  `test_mcp_budget_monotone.py`・`test_tools_index_building.py`・`test_syntax_*`、見張り 4 本（`test_import_graph`・`test_server_launch_guard`・
  `test_stdout_purity`・`test_leak_guard`）と `test_schema_budget` が green。`uv run ruff check` クリーン。全体テストは流していない（親が統合後に流す）。

### 残ったこと
- **T-11 の p95（空いた時間帯 F-POLY 89・F-PY 57 ms、負荷の下 298・236 ms）**: 尾は LSP の rename の冷えた所要（basedpyright がワークスペースの参照を
  型で確かめながら走査する。負荷の下では LSP のプロセスの CPU も減る）。
  BUG-0058 の T-06・T-07 と同じ性質で、判定の形は OD-23 に従う。手元の処理で残るのは、書き換えた後の内容の解析（`_analyse` の後ろ半分。
  定義の名前に触れない書き換えのファイルは前のシンボルから位置をずらして作れる見込み。未着手）。
- **T-04**（負荷の下で p95 66〜71 ms）: 尾は exact の持ち上げの LSP の冷えた hover・definition と、負荷で LSP のプロセスの CPU が減る分。空いた時間帯は
  p95 9〜26 ms。
- **T-10**（負荷の下の F-PY で p95 55 ms）: 尾を作る処理は無く、負荷の倍率（約 2.5 倍）。空いた時間帯は p95 7〜20 ms。手元の処理で残るのは、書き換えた後の
  内容の構文の確かめの解析・2 回のファイルの読み・`sha256`・差分の作成など、小さなものの積み重ね。
- 「後」の全体の計測（14 ツール）は負荷の軽い時間帯の 1 回だけ。負荷の下の全体（T-06〜T-08 を含む）は流していない。
- 見たこと（直していない。BUG-0131 に票）: パイプラインが呼び出しのたびに `data_version` を書き手の 1 本の読み取り接続でイベントループの上で読む。
  `LspPool.status` が呼び出しのたびに LSP の起動コマンドを探して `stat` する（T-10 の cProfile で 1 回約 1 ms）。
