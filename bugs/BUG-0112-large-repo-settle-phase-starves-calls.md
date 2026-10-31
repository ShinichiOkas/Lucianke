# BUG-0112 大きなワークスペースの初回索引で、取り込みの後に十数分 DB に書かないまま CPU を使い続け、その間の呼び出しが 2 分以上待たされる

- 報告: 2026-10-31 / 見た人: 親（能力測定の基準〈Mnemo の分析〉で Lucianke を Mnemo に向けたとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`main` = `8505b8d` 相当のソースから `uv run --project <lucianke> python -P -m lucianke`、cwd = 対象のワークスペース〈Python 約 2,000 本・.gitignore 適用後〉、`LUCIANKE_CACHE_DIR` は空から）

- 10:47 頃 `configure_workspace set_roots` → `jobs: job_0001 reindex running 0%`。以後 5 秒ごとに `configure_workspace status` を呼んだ。
- `paad.sqlite` の最終書き込み 10:50:15（225.5 MB、`-wal` 21.4 MB）。以後 11:04 まで書き込みなし（13 分以上）。
- サーバーのプロセス（`python -P -m lucianke`）: 起動から 17 分で CPU 1,024 秒（ほぼ 1 コアを使い切り）、作業セット 1,930 MB。
- サーバーのログ（`tool_call` の `timing_ms`）: `configure_workspace`（status）が 10:50 までは 1,411〜3,659 ms、10:52 以降は **119,180 / 152,739 / 130,175 / 132,641 / 139,299 / 137,721 ms**。
- 比較: 同じ版で Django 5.1（Python 2,788 本）は索引 337 秒で終わり、この症状は見えなかった。

## 利用者（呼ぶ側の AI）として困ること

- 大きなリポジトリでは、索引の途中で数分間どの呼び出しにも答えが返らない（BUG-0070 の「索引中でも答える」が効かない）。
- 索引がいつ終わるかが分からない（進みの表示が止まって見える）。

## 追加の観測（親から、2026-10-31）

- 票の実測と同じ実行で、Mnemo のワークスペースに向けたサーバーが 02:16:25 UTC（起動から約 29 分）に黙って終わった。stdout が閉じ、クライアントは EOF を受けた。サーバーのログにエラー・例外の行は無く、終わる前の最後の 6 件の `tool_call` の `timing_ms` は 93,463 / 57,724 / 54,313 / 48,747 / 37,662 / 65,270。Windows のイベントログ（Application / System）にその時刻の異常終了・資源の枯渇の記録は無い。17 分の時点で作業セット 1.9 GB、約 1 コアを使い続けていた。
- 外部の MCP クライアント（所有者のコーディングエージェント ray）が Lucianke のリポジトリ自身（約 800 ファイルのつもり）に `configure_workspace`（索引）を呼び、ray の 1 呼び出しの既定の上限 120 秒を超えた。多くのクライアントの既定は 60〜120 秒。受け入れ: Lucianke 自身・Mnemo のどちらでも、索引のせいで `configure_workspace set_roots` / `status` を含むどの呼び出しも数秒を超えて待たされない。索引は背景で走り、進みは正直に出す。

## 原因・直し方・確認（2026-10-31）

### 原因（実測で確かめた）

後段（関係の整え・PageRank）ではなかった。**ファイルごとの取り込み（構文解析と関係の抽出）が、圧縮済みの JS（1 行 数百 KB〜数 MB）で 1 ファイル数分〜数時間かかっていた**。走査はファイルを順に解析するので、その間 DB に書かない（票の「13 分以上書き込みなし」）。解析のスレッドは純 Python で CPU を使い続けて GIL を握り、`status`（dulwich で Git の状態を調べる小さな I/O の連続）は I/O のたびに GIL を待たされて（convoy）2 分以上かかった。測ったのは Mnemo の能力測定の基準のワークスペース（`d419cf0b` の写し・4,222 ファイル、うち `vendor/plotly/plotly.min.js` 4.56 MB・`.../vis-network.min.js` 689 KB・`.../katex.min.js` 272 KB）。

1. **位置の変換が行の長さに比例（主因）**: `core/textpos.LineIndex.convert_col` が変換のたびに行全体を切り出し（`line_bytes`）、非 ASCII の行では先頭からデコードしていた。関係の抽出は参照ごとに `offset_to_position` を呼ぶので、参照の数 × 行の長さ。cProfile: `vis-network.min.js` 1 ファイルの取り込み 106 秒のうち 97 秒が `offset_to_position`（`bytes.decode` 60 秒 / 20 万回・`line_bytes` 13 秒）。`plotly.min.js` は faulthandler のスタックが 7 分以上 `textpos.py:177`（`len(raw[:b].decode(...))`）のまま終わらなかった（数時間の見込み）。走査はこのファイル（並びの終わりの `vendor/`）で止まり、整えに届かなかった。
2. **祖先の走査が深さの 2 乗**: `syntax/relations` の `_in_skipped_context`・`_binding`・`_variable_type`・`_class_node` が参照ごとに根まで `Node.parent` をたどっていた（tree-sitter の親は根から下りて探す）。1 を直した後の `plotly.min.js` 120 秒（cProfile 下）のうち 36 + 17 + 8 + 2 秒。
3. **囲みの定義の掃引が 参照の数 × 定義の数**: `relations._Extractor.run` が、前の定義が終わった後に次の定義を積むので、並んだ定義が積み上がったまま、参照ごとに全部を絞り込んでいた。1 行の JS の合成で、関数 1,500 本 3.4 秒・3,000 本 14.4 秒（4.2 倍）・24,000 本（1.2 MB）で 3 分を超える。
4. **背景が前景に譲らない**: 背景の解析・抽出は GIL を握り続け、ツールの呼び出しに譲る仕組みが無かった。直す前の Lucianke 自身（814 ファイル。圧縮済みの JS は無い）でも、走査の間の `status` は 0.34〜5.66 秒。
- 整え・PageRank は原因ではなかった: 1〜3 を直した後の Mnemo で、整え 9.3 秒（1 ジョブ）・PageRank 8.8 秒。ただ 1 ジョブの整えは書き手をふさぐので割った（直し方 6）。

### 直し方

1. `LineIndex`: 256 バイト（`SHAPE_MIN_BYTES`）以上の行は、行の形（ASCII か・複数バイト文字の先頭の位置と余分なバイトの累計・4 バイト文字の位置）を 1 回だけ調べて覚え、変換を二分探索で答える。短い行・UTF-8 として不正な行は今までの変換。答えは同じ（長い行のすべての単位の組・行末の先・文字の途中・サロゲートペアの途中・BOM・不正なバイトで今までの変換と比べる試験）。
2. 祖先の走査をノードごとに覚える（`_up`: 最も近い囲みのスコープ〈関数・クラス・根〉と、読み飛ばす文の中か。`_scopes` で外へたどる）。
3. 掃引で、次の定義を積む前に、その定義より前に終わった定義を降ろす（以後のどの位置も囲まない。囲みの集合は同じ）。
4. **前景の優先**（新しい `core/foreground.py`）: パイプラインがツールの呼び出しを `FOREGROUND.active()` で囲む。背景の索引（走査のタスクと、そこから `asyncio.to_thread` で起こした解析のスレッド。`background()` の印が文脈で伝わる）は区切りごと（ファイルの解析の前・解析の 16 KiB の塊ごと〈眠った分は解析の期限を延ばす〉・抽出の繰り返しの 1 回ごと・整えのジョブの間・PageRank の反復ごと・差分の解き直しの 1 ファイルごと）に短く眠って GIL を譲る。1 回に譲るのは 2 秒（`MAX_YIELD_S`）まで、前景が続いていれば背景は 0.1 秒（`RUN_SLICE_S`）進んでからまた譲る（`status` を絶え間なく呼び続けるクライアントでも索引は止まらない）。呼び出しが始まって 10 秒（`PRIORITY_WINDOW_S`）を過ぎたら譲らない（LSP を待つ T-06 などで索引を止め続けない）。書き手のジョブの中では譲らない（前景の書き込みがその後ろで待っているかもしれない）。印の無い文脈（ツールが同じ抽出を前景で呼ぶ）では何もしない。
5. **進みの段**: 全ファイルの後の段を `ScanProgress.phase` / `JobStatus.phase` に出す（`settling relations k/n`・`re-resolving importers`・`ranking symbols`・`settling files ingested ahead`）。読むツールの警告は `index building: 4221/4222 files (99%), settling relations 12/170; results may be incomplete - call again when done`、T-00 の `jobs[]` に `phase`（compact の `jobs:` 行は `job_0001 reindex running 97% (settling relations 12/170)`）。進みは ファイル 5〜95・整え 95〜98・PageRank 99・終わり 100。
6. 整えを `SETTLE_BATCH_FILES`（50）ファイルずつの短い書き込みのジョブに割る: (1) ファイルごとの整え（`_settle_files`）を全バッチ → (2) 退避した行を基底から結ぶ（`inherited.resolve_member_refs`）を全バッチ。割らない整えと同じ索引になる（`inherited_calls`・`reexports`〈TS のバレル・既定の import・型の別名を含む〉・`poly` を 1 ファイルずつのジョブで比べる試験。Mnemo でも件数が同じ: files 4,221・symbols 75,937・relations 420,924・`_name_calls` 289,287・`_member_refs` 4,678）。1 ファイルの取り込み（`index_file` / `remove`）は今までどおり 1 トランザクションで整える。
7. **黙って落ちないように**（29 分で黙って終わった観測への備え）: 起動時に `faulthandler` を `<log_dir>/crash.log` に向け（アクセス違反・abort・スタックの溢れで全スレッドの Python のスタックを書く）、`threading.excepthook` / `sys.excepthook` で拾われなかった例外をログに、`atexit` で `process exiting` をログに残す。`asyncio` のロガーにもログのハンドラを付ける（拾われなかったタスクの例外が stderr だけに出ていた）。29 分で終わった原因そのものは、この PC では再現しなかった（直した後の版はどの実行も最後まで走った。下の確認）。
- 仕様書 §4.3.1（索引の重さで呼び出しを数秒を超えて待たせない・進みの段）・T-00 の `jobs[]`、設計書 §6.1（前景の優先）・§7.6.2（走査の段・整えのバッチ・長い行と深い入れ子）に理由つきで書いた。

### 確認

- **1 ファイルの取り込み**（`Indexer._prepare`、この PC）: `vis-network.min.js` 102.5 → 3.5 秒、`katex.min.js` 3.7 → 1.4 秒、`plotly.min.js` 7 分を過ぎても終わらず → 46 秒、合成の 1 行の JS（関数 24,000・1.2 MB）3 分超 → 9.9 秒。関係・シンボルの件数は直す前と同じ（vis-network 314 / 1,972 など）。
- **Mnemo の写しの全体の索引**（in-process、各段の時間）: 直す前は取り込み 90% まで 423 秒、そこから `plotly.min.js` で止まり、15 分を過ぎても整えに届かなかった（打ち切った）。直した後は 249 秒（取り込み 228 秒・整え 170 ジョブ 約 6 秒・PageRank 6 秒）。ほかの役が CPU を使っていた時間帯は 304〜426 秒。Django 5.1 の票の 337 秒と同じ水準。件数は整えを割らない版と同じ（files 4,221・symbols 75,937・relations 420,924）。
- **メモリ**（作業セット、in-process）: 取り込み 90% で 812 MB → 終わり際（`plotly.min.js` の照合と PageRank）に最大 1,911 MB → 終わって 1,385 MB、構文木のキャッシュ（1,497 ファイル・見積もりの上限 512 MB）を捨てると 1,063 MB。票の 1.9 GB と同じ水準で、伸び続けはしない。実プロセスでも最大 1,974 MB で最後まで走った。大きな `*.min.js` の照合の結果（70 万件）が山を作る（直し方の照合の範囲割りで、一度に持つ一致を範囲ごとにした）。
- **stdio の実プロセス・Lucianke 自身**（814 ファイル、`status` を 1〜2 秒ごと）: 直す前 最悪 5.66 秒（16 回）、直した後 最悪 0.79 秒（38 回）、索引の全体は 60 秒で変わらない。
- **stdio の実プロセス・Mnemo の写し**（Git の作業ツリーにした写し。`status` は dulwich で Git の状態を調べる。3〜5 秒ごとに `status` と検索）: 直す前は票のとおり 119〜152 秒。直した後（前景の優先の上限 0.5 秒の版・照合の範囲割りの前）: 89 回 最悪 5.47 秒・平均 1.85 秒（最悪は `plotly.min.js` の 1 回の照合の間）、検索 最悪 4.35 秒、索引の全体 450 秒（`status` の間は索引が譲るので in-process より長い）。ほかの役が CPU を使い、何もしていないときの `status` が 2.2〜3.0 秒の時間帯では、120 回 最悪 7.56 秒・平均 3.6 秒。その後、上限を 2 秒にし（1 回の呼び出しが背景の割り込みを受けない）、大きなソースの照合を範囲に割った（4.5 MB の照合が 1 回で GIL を数秒握っていた）。
- **新しい試験**: `tests/e2e/test_index_responsive_stdio.py`（tmp に Python 250 本と圧縮した JS 約 1 MB の Git の作業ツリーを作り、実プロセスの初回の索引の間 0.25 秒ごとに `status` を呼び、すべて 5 秒以内・`progress` が減らず 100 で終わる・途中の検索に進みの警告）: 直す前のソース（origin/main）では 300 秒で索引が終わらず（94%）・`status` 最悪 8.79 秒（80 回）で落ちる、直した後は 64 秒で通る。`tests/unit/test_engine_indexer_responsive.py`（わざと遅くした整えの途中でも書き込みのジョブ・読み取りが 1 バッチ分の遅れで答え、`building()` とジョブが `settling relations k/n` を出す / 割った整えが割らない整えと同じ索引〈3 フィクスチャ〉/ 前景の間は次のファイルを解析しない）、`tests/unit/test_core_foreground.py`（窓・印の無い文脈・スレッドとタスクの待ち・`to_thread` への印の伝わり・絶え間ない前景でも背景が進む）、`tests/unit/test_core_textpos.py` 2 件（長い行の答えが今までと同じ / 2 MB の行で 10 万回の変換が 10 秒以内〈実測 0.3〜0.9 秒、直す前は 100 秒超〉）、`tests/unit/test_syntax_relations.py` 1 件（並んだ関数 4,000・入れ子 300 で 15 秒以内・呼び出し元が正しい。直す前のソースでは 159.8 秒で落ちる、直した後 4.2 秒）、`tests/unit/test_syntax_parsers.py` 2 件（範囲に割った照合の一致の集合が全体と同じ。JS・Python）。
- 対象の試験（indexer・engine・relations・name-calls・reexports・inherited・schema・index_core・T-00・T-06・T-07・`tests/contract_fixtures`・新しい e2e と `test_wired_index_stdio`）: 1,206 件が通り 15 件は skip。`uv run ruff check` はきれい。
- 見つけた別の不具合は票 BUG-0118（走査が入れ子の Git の作業ツリーに降りる）・BUG-0119（圧縮済みの JS を構文解析してシンボルを数千作る）に。
