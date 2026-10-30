# BUG-0091 冷えた LSP での F-PY の T-07（incoming、depth 3）は、負荷をかけなくても 1 回目が 1 問い合わせの予算 `EXACT_BUDGET_S`（10 秒）や要求の期限（5 秒）に届いて降格する（5 回中 4 回）。降格の警告は使い切った予算ではなく「5000 ms」と書く

- 報告: 2026-10-31 / 見た人: BUG-0080 を直す役
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `c7f2b7e` + BUG-0080 の直し、basedpyright 1.40.2、Windows 11）

F-PY の rig（`tests/contract_fixtures/_rig.open_rig`。本番と同じ結線。索引の完了と basedpyright の ready を待つ）を開き、
`warm_up(rig, ["httpx/_urls.py"])`（最初の診断まで待つ）の後に、T-07 `query_call_hierarchy`
（`URL.copy_with`、`direction=incoming`、`depth=3`、`max_tokens=6000`）を聞いた。rig は毎回新しい（basedpyright も新しいプロセス）。

### 1 回目の depth 3（冷えた LSP）の所要と水準

自分で負荷はかけていない（同じ PC で他の役の試験が走っていたかもしれない）。

| # | 前に聞いたもの | 所要 | 水準 | 降格の理由（所要からの見立て。警告はどれも「5000 ms」） |
|---|---|---|---|---|
| 1 | なし | 10.08 秒 | `heuristic`（降格） | 予算 10 秒 |
| 2 | なし | 8.5 秒 | `exact` | — |
| 3 | なし | 5.2 秒 | `heuristic`（降格） | 要求の期限 5 秒（`LSP_TIMEOUT_CALL_HIERARCHY_MS`） |
| 4 | なし | 10.12 秒 | `heuristic`（降格） | 予算 10 秒 |
| 5 | なし | 5.19 秒 | `heuristic`（降格） | 要求の期限 5 秒 |
| 6 | T-07 depth 1（3.3 秒・`exact`） | 6.94 秒 | `exact` | — |
| 7 | T-06 references（2.1 秒・`exact`） | 8.11 秒 | `exact` | — |

- 何も聞いていない冷えた LSP への 1 回目は、5 回中 4 回が降格した（予算 10 秒で 2 回、callHierarchy の要求 1 つの期限 5 秒で 2 回）。
  要求の期限で切れた分は、`LspHandle` の連続時間切れ（3 回で `unavailable`）に数えられる。
- `tests/contract_fixtures` の T-07 の試験（`warm_up` の後に depth 3）でも、負荷の高い全体テストで同じ降格が起きている（BUG-0074・BUG-0080）。

### 内訳（exact で答えた 8.5 秒の 1 回。BUG-0058 の遅い呼び出しのログ）

| ノード | 合計 | 内訳 |
|---|---|---|
| `URL.copy_with` | 2844 ms | prepare 63・calls 2000・sites 781 |
| `BaseClient._enforce_trailing_slash` | 1328 ms | settle 1281（そのファイルの最初の診断を待つ） |
| `BaseClient.__init__` | 1297 ms | calls 1297 |
| `Proxy.__init__` | 688 ms | settle 344・calls 297 |
| `test_url_set_param_manipulation` | 828 ms | settle 797 |

- 2 回目以降（同じ rig）は `lsp-cache` で 0.05 秒・`exact`。depth 1 は冷えた状態でも 3.3 秒で `exact`。

### 降格したときの警告

- 予算（10 秒）を使い切って降格した応答の `accuracy_warning` は「LSP の応答が 5000 ms 内に得られなかったため構文木で解決しました。…」。
  5000 ms は callHierarchy の要求 1 つの期限（`LSP_TIMEOUT_CALL_HIERARCHY_MS`）で、使い切ったのは 1 問い合わせの予算 10 秒。予算を
  0.3 秒・0 秒にして降格させたときも、同じ「5000 ms」の文言だった。

## 再現手順

1. F-PY の rig を新しく開き、`warm_up(rig, ["httpx/_urls.py"])`。
2. 上の T-07（depth 3）を 1 回聞き、所要と `_resolution_meta.confidence_level` を見る。

## 困ること

- 呼ぶ側の AI がセッションの最初に聞く「このメソッドを誰が呼ぶか（depth 3）」が、60 ファイルほどのリポジトリでも、負荷を
  かけなくても 5 回中 4 回 `structural`（同名メソッドの区別が落ちる）になる。聞き直せば `exact` になるが、AI には聞き直す
  理由が分からない（警告は「5000 ms 内に得られなかった」と言い、使い切った予算も、聞き直せば exact になることも書かない）。

## 原因・直し方・確認（2026-10-31）

### 原因

1. **警告が切れた上限を取り違えていた。** T-07 の exact 経路の時間切れは、1 問い合わせの予算（`EXACT_BUDGET_S` 10 秒。
   `CallEdgeSource` がワーカースレッドで `fut.result(timeout=残り)`）でも、要求 1 つの期限（`LSP_TIMEOUT_CALL_HIERARCHY_MS`
   5000 ms。`features._call`）でも同じ素の `TimeoutError` だった。ハンドラーは `degraded_choice` → `selector.on_lsp_timeout` で
   表の定数 `LSP_TIMEOUT_MS[CALL_HIERARCHY]`（5000）の雛形を入れるので、予算で切れても「5000 ms 内に得られなかった」と書いた。
   例外にどの上限が切れたかの情報が無く、どこまで答えたか・次にすることも書いていなかった。
2. **予算に届くと、答えた分も捨てていた。** 木の途中で切れると木ごと `structural` にし、LSP がそれまでに答えたノードも書き戻さ
   なかった。呼び直しも最初から聞き直すので、負荷が続くと呼び直しも予算に届いた（直す前の計測: 1 回目 10.1 秒で降格 → 呼び直し
   10.1 秒でまた降格。別の回は呼び直しが exact でも 8.8 秒）。
3. **冷えた LSP での時間の内訳**（直す前のコード。F-PY、`URL.copy_with` incoming depth 3、子を求めるノード 29、1 回目）:
   - LSP の要求 58 本（ノードごとに `prepareCallHierarchy` + `incomingCalls`）: 軽い負荷で計 2.1〜2.5 秒（根の `incomingCalls`
     0.8〜1.1 秒、`BaseClient.__init__` 0.55〜0.6 秒、`Proxy.__init__` 0.2 秒、残り 50 本ほどは 1 本 16 ms 前後）。負荷が
     あると計 3.5〜4.8 秒（根だけで 1.6〜3.9 秒）。
   - **最初の診断の待ち**（settle）: ノードの定義ファイルごとに 5 ファイル（`httpx/_client.py` 0.6〜1.0 秒、
     `tests/models/test_url.py` 0.65〜1.1 秒、`httpx/_config.py` 0.2〜0.5 秒、`tests/test_asgi.py` 0.1〜0.2 秒）。計 1.2〜2.7 秒
     で、所要の 3 割前後。目印（PaaD の exact 行を次から使ってよいか）を決めるためだけの待ちで、答えの中身は変わらない
     （下の「確認」の木の比較）。待つ間 basedpyright はそのファイルを検査していて、要求も遅れる。
   - 呼び出し箇所の構文木（sites）: 根で 0.36〜1.2 秒（呼び元のファイルを 1 つずつ）。
   - 合計: 軽い負荷 4.6〜5.2 秒、負荷あり 6.6〜9.4 秒。重い負荷で 10 秒の予算に届いた（この PC では他の役の試験が並行して
     走っており、同じ要求の所要が時間帯で 2 倍ほど変わる）。

### 直し方

- **警告（必須）**
  - `lsp/features.py`: 要求の期限切れを `LspRequestTimeout`（`TimeoutError` の派生。`method`・実際に渡した `timeout_ms`・
    `server`）で上げる。握る側はそのまま。
  - `engine/selector.py`: `on_lsp_timeout(..., timeout_ms=)`。`on_lsp_failure` は例外の `timeout_ms` を渡す（T-06 などの
    `lsp_timeout` の雛形も、表の定数ではなく実際に切れた期限を書く。期限を持たない時間切れは今までどおり表の値）。
  - `tools/_lsp_exact.py`: 予算切れは `ExactBudgetExceeded`。`CallEdgeSource` が最初に LSP に聞くのをやめた理由を
    `ExactCut`（予算 / 要求の期限 / LSP が使えない、経過秒、ノード、要求、期限の値）に残し、`degradation_warning`
    （`accuracy_warning`、日本語）と `cut_note`（`warnings[]` の 1 行、英語）を作る。書くのは、切れたのが 1 問い合わせの予算か
    要求の期限か・その実際の値（要求ならその要求とノード）・exact で答えたノードの数と全部 exact の深さ・次にすること
    （LSP は動いているので呼び直す。答えた分は書き戻したので、呼び直すと残りだけを LSP に聞く）。例（実測、予算切れ）:
    「LSP（basedpyright）の呼び出し階層に使う 1 問い合わせの予算 10 秒（この問い合わせで LSP に使える時間の合計）を使い切り
    ました（BaseClient を聞いている途中）。子を求めた 29 ノードのうち 22 ノードは LSP の答え（exact）で、depth 2 までは
    すべて exact です。残り 7 ノード（…）の呼び出し元は構文木（relations）で補いました（…）。LSP は動いています（…）。
    LSP が答えた分は PaaD に書き戻したので、同じ問い合わせを呼び直すと残りのノードだけを LSP に聞き、exact で答えきれる見込み
    です。」／`warnings[]`: `exact LSP answers for 22/29 expanded nodes (complete to depth 2); the 10 s exact budget for one
    query ran out after 10.0 s; the callers of 7 node(s) (...) come from the syntax index (conf=structural,
    nodes[].children_confidence_level); the LSP is up and warmer now and the answered nodes are saved - call again to finish
    the rest exact`。要求の期限なら「LSP（basedpyright）が callHierarchy/incomingCalls の要求に、要求 1 つの期限 5000 ms 内に
    答えませんでした（URL.copy_with。問い合わせの開始から 5.1 秒の時点。1 問い合わせの予算は 10 秒）」。
  - T-07 のハンドラー: 時間切れはこの文面を使う（セレクターの雛形は要求の期限しか知らない）。索引中・利用不可は今までどおり
    セレクターの雛形。
- **答えた分は exact のまま（部分 exact）**
  - 木を深さごとに聞く（`_expand` を幅優先にし、1 段ぶんのノードを `CallEdgeSource.prefetch` にまとめて渡す。木の形の規則と
    表示の前順は同じ）。予算を使い切る・要求の期限で切れる・LSP が使えなくなると、以降のノードは LSP に聞かず子を
    `relations` の行で補う（そのノードの子は `structural`、処理規約 9 の名前の判定も補った分にだけ使う）。根の子が取れない
    ときだけ木ごと `structural`。補ったノードには json で `children_confidence_level: "structural"`、compact で
    `- children: from the syntax index (conf=structural; the LSP did not answer)`。応答は `mixed: true`・ノードごとの
    `confidence_level`。
  - 答えた分は木ごと降格したときも書き戻す（`flush`）。呼び直すと、目印のあるノードは PaaD から、残りだけを LSP に聞く。
  - 予算で打ち切った要求は取り消す（今までどおり連続時間切れには数えない）。ノード固有の失敗（そのノードの項目が無いなど）は
    そのノードだけ補い、他のノードは聞き続ける（以前は木ごと降格）。
  - 信用しない答え（根の 0 件）は書き戻さない（同じ問い合わせの他のノードを書き戻すときに「0 件」の目印を置かないように）。
- **冷えたときの時間**（予算・期限の既定値は変えていない）
  - 最初の診断を待つのは根のファイルだけにした（T-06 と同じ）。この合図は「LSP がワークスペースの解析を済ませた」こと
    （BUG-0012・20e の起動直後の部分的な答え）で、LSP 全体の状態なので 1 度で足りる。根の子をその合図の後に聞いたときだけ、
    木のノードに目印を置く（根の子が PaaD から来てまだ確かめていなければ、ノードのファイルで確かめる）。→ settle 1.2〜2.7 秒が
    ほぼ 0 に。
  - 深いノードの項目は親の答え（incoming の `from` / outgoing の `to`）をそのまま使い、`prepareCallHierarchy` を送らない
    （索引のシンボルと同じファイル・同じ名前の位置のときだけ。違えば今までどおり prepare）。要求 58 本 → 31 本。ノードの
    ファイルを開かない（`didOpen` しない）ので、basedpyright がそのファイルを検査しに行かない。
  - 呼び出し箇所の構文木はファイルごとに並行（要求の枠の外。`SiteIndex` は同じファイルを 1 度だけ読む）。
  - LSP への要求は 1 本ずつ（`EXACT_LANES = 1`）。2 本にした比較は下の「確認」。basedpyright は要求を順に処理するので、重ねても
    LSP の仕事は減らず、待ち行列の分だけ要求の期限（その時間切れは連続 3 回で LSP を落とす）に近づく。
  - 予算 10 秒・要求の期限 5000 ms（仕様書 §12.3）は変えていない。根の `incomingCalls` が冷えた LSP で 5 秒を超え木ごと
    降格する回（票の 5 回中 2 回。途中の版の計測でも 5 回中 1 回。根の要求は直す前と同じ）は残る。T-07 だけ要求の期限を予算の残りに揃える案は、
    仕様（§12.3 の要求の期限）を変えるので入れていない（判断材料: その回は根の答えも捨て、時間切れが連続 3 回で LSP を落とす
    数えにも入る）。
- 仕様書 T-07 処理規約 2・`nodes[]`・精度と降格、設計書 §7.5.6 の T-06・T-07 の exact 経路に、上の規則と理由を書いた。

### 確認

計測の台本（scratchpad、リポジトリには置いていない）: F-PY の rig を毎回新しく開き（basedpyright も新しいプロセス）、`warm_up(rig,
["httpx/_urls.py"])` の後に票と同じ T-07（`URL.copy_with`、incoming、depth 3、`max_tokens=6000`）を 1 回、続けて同じ問い合わせを
もう 1 回（呼び直し）。直す前のコード（`git archive HEAD src` を `PYTHONPATH` で先に置く）と直した後のコードを **交互に** 流した
（同じ PC で他の役の試験が走っていて、同じ要求の所要が時間帯で 2 倍ほど変わるため、続けて 5 回ずつではなく 1 回ずつ交互）。

| 系列 | コード | 1 回目の所要（秒） | 降格 | 呼び直し |
|---|---|---|---|---|
| 1（負荷が重く、時間帯で変わる） | 直す前 | 8.92・9.44・**10.08**・6.62・5.16 | 1/5（木ごと structural、警告「5000 ms」） | 降格した回は 8.83 秒（最初から聞き直し）。他は 0.03〜0.06 秒（lsp-cache） |
| 1 | 直した後 | **10.06**・**10.08**・6.00・4.31・3.42 | 2/5（どちらも部分 exact: 29 ノード中 22 ノードが exact、depth 2 まで全て exact。警告は予算 10 秒） | 部分 exact の回は 3.52・4.45 秒で exact に答えきった（残り 7 ノードだけ聞いた）。他は 0.03〜0.05 秒 |
| 2（負荷が軽い） | 直す前 | 3.77・4.19・5.08・4.56・3.81（平均 4.28） | 0/5 | 0.03〜0.05 秒 |
| 2 | 直した後 | 3.55・3.31・3.42・3.92・3.34（平均 3.51） | 0/5 | 0.03〜0.05 秒 |

- 1 回目の LSP の要求: 直す前 58 本（prepare 29 + incomingCalls 29）→ 直した後 31 本（prepare 2 + incomingCalls 29）。最初の診断の
  待ち: 直す前 5 ファイル・計 1.2〜2.7 秒 → 直した後 根のファイルだけ・0〜16 ms。呼び直しが lsp-cache（0.03〜0.05 秒）なので、
  根のファイルの合図だけでも木のノードに目印が置かれている。
- 系列 1 の 1 組目・2 組目は、同じ根の `incomingCalls`（両方のコードで同じ要求）が直す前 1.6〜1.7 秒・直した後 3.1〜3.3 秒で、
  直した後の番に負荷が重かった。降格の回数は負荷で決まり、5 回ずつでは比べられない。比べられるのは、予算に届いたときの答え
  （直す前: 木ごと structural・「5000 ms」の警告・呼び直しも最初から / 直した後: depth 2 まで exact・予算 10 秒の警告・呼び直しは
  残り 7 ノードだけで exact）と、負荷が同じときの所要（系列 2 で 2 割ほど短い）。
- 要求を 2 本ずつ（`EXACT_LANES = 2`）にした比較（交互に 4 回ずつ、負荷が軽い）: 1 本ずつ 3.33〜3.95 秒、2 本ずつ 3.09〜3.70 秒で
  差は無く、要求ごとの所要の合計は 2.6〜3.2 秒 → 4.1〜5.2 秒に増えた（basedpyright が順に処理し、待ち行列の分だけ各要求が
  延びる）。1 本ずつのままにした。
- 答えの中身は変わらない: 期限を延ばして（`patient_lsp`、切らずに）同じ F-PY で、直す前と直した後の木を比べた。
  `URL.copy_with` incoming depth 3（48 ノード・56 辺）と `Client.send` outgoing depth 2（23 ノード・27 辺）が、ノード（深さ・名前・
  呼び出し箇所・when・水準・循環）も辺も一致（項目の使い回し・根のファイルだけの待ち・幅優先で木が変わらない）。
- 決定的な試験（`tests/contract/test_T07_exact_budget.py`、偽 LSP で遅れを決める）5 本: 遅れなしは全部 exact／caller の
  incomingCalls が予算（3 秒に縮めた）に届く → caller は exact、その子は structural（`mixed`・`confidence_breakdown`
  `{exact: 1, structural: 2}`・`children_confidence_level`・compact の印）、警告は「予算 3 秒…を使い切りました」で「内に答え
  ませんでした」「5000 ms」を含まず、2 ノード中 1・depth 1・呼び直すことを書く。取り消した要求は連続時間切れに数えない。呼び直すと
  exact で答えきる／caller が要求の期限（1500 ms に縮めた）に届く → 警告は `callHierarchy/incomingCalls`・1500 ms・caller・予算
  10 秒で、「使い切りました」を含まない／根が予算に届く・根が要求の期限に届く → 木ごと structural、警告はそれぞれの上限の値と
  「木全体を構文木」・呼び直すこと。セレクターの試験: `LspRequestTimeout(…, 120000)` は「120000 ms」を書き、期限を持たない
  時間切れは表の値。
- 実の basedpyright と偽 LSP の T-06・T-07 の契約テスト一式と関係する単体テスト（`tests/contract/test_T06_T07*.py`・
  `test_T06_ts_outside_tsconfig.py`・`test_T07_*.py`・`test_revision_reads.py`・`test_unsupported_tools.py`、
  `tests/unit/test_engine_selector*.py`・`test_lsp_exact_degrade.py`・`test_lsp_features.py`・`test_lsp_prime.py`・
  `test_mcp_graph.py`・`test_tools_index_building.py`）: 476 passed, 6 skipped（skip は TypeScript の LSP が入っていない 6 件）。
- `tests/contract_fixtures`（`uv run pytest -q -p no:cacheprovider -rfsw tests/contract_fixtures`）: 45 passed, 6 skipped（671 秒、
  計測と同じ時間帯）。skip は F-TS の 5 件（TypeScript の LSP が無い: T-04#4・T-06#1・T-07#2・T-11#2-3・T-13#1-3）と、F-TS には
  当てはまらない BUG-0079 の試験 1 件。F-PY の exact の事後条件（T-07#2 を含む）は通った。T-07 の depth 3（事後条件 1・3・4・6）は
  どちらの経路（exact・部分 exact・structural）で答えても同じ検査。
- `uv run ruff check`（引数なし）: All checks passed。

### 確認（追記: 載せ直した後）

- `98e67ea`（BUG-0090）に載せ直した。BUG-0090 は `lsp/features.py` の `_call`（エラー応答を `restarting` / `lsp_error` に分ける）と
  セレクター（`restarting` の警告）を変えていたので、`_call` は両方（期限切れは `LspRequestTimeout`、エラー応答は BUG-0090 の分け方）
  にした。T-07 は `restarting`（LSP を起動し直している）も LSP に聞くのをやめる理由に入れ（`_STOP_REASONS`）、その後は
  「起動（再起動）・索引の完了後に呼び直す」と書く。根の子でそれが起きたときはセレクターの BUG-0090 の雛形のまま。
- 載せ直した後: T-06・T-07 の契約テスト一式と関係する単体テスト（上の一覧に `tests/unit/test_lsp_pool.py` を足した）: 562 passed,
  6 skipped（skip は TypeScript の LSP が無い 6 件）。`tests/contract_fixtures`: 45 passed, 6 skipped（288 秒。skip は上と同じ 6 件）。
  `uv run ruff check`: All checks passed。

### 見つけた別の不具合

- なし。

### 追記（2026-10-31）: 根の要求は予算の残りまで待ち、予算の内の遅れは連続時間切れに数えない

上の「直し方」で入れなかった案（T-07 の根の要求を予算の残りに揃える）を、親の判断で入れた（判断の理由: 冷えたサーバーが最初の
問い合わせで遅いだけで LSP をセッションの間 `unavailable` にするのは、呼ぶ側の AI にとって最初の答えが数秒遅いことよりずっと悪い）。

- T-07 の根の要求（根の子を求める prepare と incoming / outgoing）は、要求 1 つの期限 5000 ms ではなく 1 問い合わせの予算
  （10 秒）の残りを期限にする（`CallEdgeSource._root_limits` → `features.prepare_call_hierarchy` / `incoming_calls` /
  `outgoing_calls` の `timeout_ms`）。深いノードの要求は 5000 ms のまま。
- 連続時間切れ（3 回で `unavailable`）に数えるのは、要求 1 つの期限以上待っても答えなかった要求だけ（`features._call` の
  `strike`）。予算の内に遅れて答えた要求は `record_ok`、予算の残りが期限より短くて切れた要求・予算で取り消した要求は数えない。
  根の段だけループ側の打ち切りを 0.5 秒遅らせ（`_ROOT_GRACE_S`）、根の要求の時間切れ（数えるかの判断つき）を先に起こす。
  根が予算の内に答えなければ、警告は予算（「…予算 10 秒…を使い切りました（URL.copy_with を聞いている途中）」）。
- T-06 を確かめた: references の要求 1 つ（期限 3000 ms）で答え、1 問い合わせの予算が無い。「予算の無いツールの期限切れ」なので
  今までどおり数える（変えていない）。
- 仕様書 §12.3 の「要求のタイムアウト」と T-07 処理規約 2、設計書 §7.5.2（要求のタイムアウトと数え）・§7.5.6 に理由つきで書いた。
- 試験（`tests/contract/test_T07_exact_budget.py`、偽 LSP）: 製品の既定で根が 7 秒で答える → exact・数えない／根が要求の期限
  （1000 ms に縮めた）を超えて予算（4 秒）の内に答えるのが 3 回続く → 3 回とも exact・LSP は ready・数え 0（以前の作りなら
  3 回とも期限切れで LSP を落としていた）／根が予算（3 秒）の内に答えず期限（1500 ms）以上待った → 数え 1・木ごと structural・
  警告は予算 3 秒／予算（3 秒）が期限（5000 ms）より短くて根が切れた → 数え 0（前からある試験）。`test_T06_T07_lsp.py` の
  答えない偽サーバーの T-07 は、根が予算まで待つので予算を 2 秒にし、警告が予算を書くことを確かめるようにした。
- 確認: T-06・T-07 の契約テスト一式とセレクター・LSP の単体テスト（`test_engine_selector*.py`・`test_lsp_exact_degrade.py`・
  `test_lsp_features.py`・`test_lsp_prime.py`・`test_lsp_pool.py`）: 467 passed, 6 skipped（skip は TypeScript の LSP が無い 6 件）。
  `uv run ruff check`: All checks passed。
