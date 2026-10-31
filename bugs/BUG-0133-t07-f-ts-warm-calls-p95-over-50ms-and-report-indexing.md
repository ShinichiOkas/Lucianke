# BUG-0133 F-TS の T-07 の 2 回目以降（温）が p95 101 ms で 50 ms を超え、温の呼び出しの 4 割が `lsp_status: indexing` と答える

見たことだけを書く（調査していない）。2026-10-31 18:07〜、BUG-0058 の直す役が `3c015c9`（M1-3 の判定を冷・温に分けた形。仕様書 §17 OD-23 (b)）で
`bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200` を流したときに見た。typescript-language-server 4.3.3 / TypeScript 5.6.3 は bun 経由
（この PC に node が無い）、ky には `bun install --ignore-scripts` で依存を入れた。CPU の `cpu_busy_pct` は F-TS 25.7。結果 JSON は直す役の作業ツリーの
`bench/results/20261031-180657-windows-latency.json`（push していない）。

## 症状
- T-07 `query_call_hierarchy`（`direction: incoming`、`depth: 2`）の F-TS: 冷 n=13 p50 51 / p95 710 ms（< 2 秒で合格）、**温 n=174 p50 29 / p95 101 / max 647 ms**
  （50 ms 以上で不合格）。同じ計測の F-PY の温は p50 6 / p95 9 ms、F-POLY の温は p50 6 / p95 25 ms。
- 温の 174 回のうち **74 回が `lsp_status: indexing`・`confidence_level: exact`**、100 回が `ready`・`exact`。TS の LSP は計測の間ずっと動いていた
  （同じ実行の T-06・T-08 は `ready`）。
- 温の呼び出しの対象ごとの中央値（ms）: `test/resilience.ts#client` 68（最大 579）、`test/resilience.ts#observe` 46、
  `source/utils/is-network-error.ts#isRawNetworkError` 40、`test/request-body-serialization.ts#assertBytes` 37（最大 647）、
  `source/utils/type-guards.ts#isResponseSizeError` 35 …… `source/utils/merge.ts#newHookValue` 10、`source/utils/timeout.ts#timeout` 9、
  `source/core/Ky.ts#cloneInitHookOptions` 7。同じ対象を何度聞いても速くならない対象と、7〜10 ms で返る対象がある。
- BUG-0058 の計測（`b850152`・`0965fec`）でも F-TS の T-07 の温は p95 73〜115 ms、`indexing` の回があった。

## 再現
上の計測を `--fixtures F-TS --tools query_call_hierarchy` で流し、結果 JSON の `runs[0].tools.query_call_hierarchy.raw.samples` を `first == false` で絞る。

## 仕様書
§14.4（温は p95 < 50 ms）、§3.2 NFR-PERF-1、§15.2 M1-3、§11 T-07。関連: BUG-0058、BUG-0129（F-POLY の `__init__` で同じ `indexing` の取り違え）。

## 原因・直し方・確認（2026-10-31）

### 原因
ky を相手に、T-06 → T-07 の順で同じ対象を 3 回ずつ聞き、結果と `paad.sqlite` の `relations` を見て確かめた（typescript-language-server 4.3.3 /
TypeScript 5.6.3 は bun 経由。TypeScript の `typescript.js` の `provideCallHierarchyIncomingCalls` / `getReferences` も読んだ）。

1. **モジュール直下の呼び元を捨てていた**（`indexing` の 74 回と、`test/resilience.ts#observe` などの遅さ）。tsserver は名前の無い関数
   （`test('x', async t => { observe(...) })` のコールバック）の中の呼び出しの呼び元を、ソースファイルの項目（`SymbolKind.File`、選択範囲は
   ファイルの先頭）で返す。T-07 はその位置で索引のシンボルを引き、見つからないので「索引に無い呼び元」として捨てていた。
   - テストの補助関数（`observe`・`assertBytes`・`withinTwoSeconds`・`createLargeBlob` など、`test(...)` の中からだけ呼ばれるもの）は
     LSP の答えが 0 件になる。索引には構文木の `CALLS`（呼び元はファイルの URN）があるので、BUG-0129 と同じ形の食い違い
     （「LSP は 0 件と答えたが索引にはある」→「LSP が解析中」）で、木ごと構文木に降格して `lsp_status: indexing` と答えていた。
     降格した答えは覚えないので、呼ぶたびに LSP に聞き直していた（温でも 70〜130 ms）。T-06 が先に同じ対象の exact 行を書いていると、
     構文木の答えの水準が `exact` になり（`relations(CALLS)`・`is_degraded: false`）、ベンチの母集団に入って `indexing`・`exact` と数えられた。
   - ファイルの先頭の行に定義があると、ファイルの先頭の位置で引いてその定義を呼び元と取り違えていた（試験の小さなファイルで `test` 関数が
     `observe` の呼び元になった）。basedpyright もモジュール直下の呼び出しをモジュールの項目（`SymbolKind.Module`・先頭）で返す。
2. **tsserver の callHierarchy は 1 つのプロジェクトしか探さない**（`source/` の型ガードなどの遅さ）。`provideCallHierarchyIncomingCalls` は
   `getFileAndProject` の既定のプロジェクト（`tsconfig.json` の `include: ["source"]`）の中だけを探し、`references`（`getReferences`）は
   開いた別のプロジェクト（`test/` の推定プロジェクト・`tsconfig.test.json`）もまたぐ。T-07 が `test/` を開かせても（BUG-0047 の prime）、
   `test/` の呼び元は答えに出ない。T-06 は同じ対象の `test/` の呼び出しを exact で書くので、T-07 が読み戻す exact 行と LSP の答えが
   食い違って目印を置けず（`isTimeoutError`・`isHTTPError` など）、温でも毎回 LSP に聞き直していた（60〜640 ms）。
   `tests/contract/test_T07_ts_outside_tsconfig.py` の「tsconfig の外の呼び元 `check` が出る」試験も、bun の tsls では直しの前から赤
   （60 秒ずっと `indexing` の構文木）だった。
3. 呼び出し階層の項目が無いノード（関数でない値を持つ変数。`test/resilience.ts` の `bodyCases` は配列で、その中のオブジェクトの
   メソッドから `client` を呼ぶ）は「LSP がこのノードに答えられない」で構文木で補い、覚えないので、温でも毎回 `prepareCallHierarchy` を
   送り直していた（`client` の温 100〜400 ms）。

### 直し方（決めたこと）
- 1: 呼び元の項目がファイル・モジュールそのもの（`_is_module_item`: `SymbolKind.File`、または `Module` で選択範囲が先頭）か、索引の
  シンボルに当たらないときは、構文木の索引・T-06 と同じく呼び出し箇所ごとの囲みの定義（`SiteInfo.source_urn`）、無ければファイルの URN
  （T-07 の `(module)` のノード。構文木の木と同じ）に結ぶ。索引に無いファイルの呼び元だけは今までどおり捨てて警告に数える。`ee7586d`
- 2: tsserver 系の incoming は、呼び出し階層に加えて `references`（`includeDeclaration`）を聞き、構文木で呼び出し（`CALLS`）と分かる箇所の
  うち呼び出し階層に無いものを足す（`_reference_call_edges`。呼び元は 1 と同じく箇所の囲み）。行は T-06 の行と同じ形になるので、
  T-06 の後でも目印が置ける。basedpyright は解析の対象と開いた文書をすべて探すので足さない（`d45d42d` で tsserver 系に絞った）。`ee7586d`
- 3: tsserver 系で呼び出し階層の項目が無いノードは、`references` の呼び出し箇所だけで答える（`bodyCases` は 0 件の exact。覚える）。`ee7586d`
- 採らなかったこと: 「LSP が 0 件・索引にはある」の降格の判定を緩める（BUG-0129 と同じく、0 件は取り違えの合図で、判定は正しかった）。
  T-06 の exact 行を T-07 の答えに混ぜる（どの問い合わせが書いた行か分からず、古い行を exact で出しうる）。

### 確認
- 試験: `tests/contract/test_T07_ts_outside_tsconfig.py` に、名前の無いコールバックの中だけで呼ばれる関数（と変数の中のオブジェクトの
  メソッド）の incoming が `ready`・`exact` で `(module)` のノードと変数のノードを返し、2 回目は PaaD から答える試験を足した（直しの前は
  赤: 先頭の行の `test` 関数を呼び元と取り違えていた）。既存の「tsconfig の外の `check`」の試験に、各ファイルの `(module)` のノード
  （`source/`・`test/`・別の tsconfig の `test-d/`）が exact で出ることを足した（直しの前は bun の tsls で赤）。
  `tests/contract/test_T06_T07_lsp.py` の `__init__` の incoming は、`routes.py` の `(module)` のノードも出る形に直した（構文木の木と同じ）。
  T-06・T-07 の関係する試験 169 件・144 件と見張り 4 本が green。
- probe（ky、T-06 → T-07 の順に 3 回、サーバー内 ms）: 2 回目以降の `observe` 114〜134 → 6〜11（`indexing` → `ready`）、
  `withinTwoSeconds` 89〜294（`indexing`・structural）→ 6〜9、`isTimeoutError` 62〜643 → 7、`client` 113〜405 → 15〜19。
- ベンチ: `bench/run_latency.py --fixtures F-TS,F-PY --calls 200 --tools find_references_and_implementations,query_call_hierarchy,rename_symbol`
  （シード 20261029、サーバー内 ms。typescript-language-server は bun 経由）。前は直しの前の `a3df079` を使い捨ての作業ツリーに展開したもの、後は
  この作業ツリー（後 A・B は `13535ff`、後 C・D は `ebb56da`、最後 A・B は `d3c676e`。T-07 の中身は `d45d42d` から同じ）。前 B → 後 A → 前 C →
  後 B、後 C → 前 D → 後 D、最後 A → 最後 B の順。ほかの役も走っていた（`cpu_busy_pct` は前 30.0 / 42.3 / 16.0、後 33.8 / 42.0 / 19.4 / 19.2 /
  29.3 / 19.7）。

  | F-TS の T-07（深さ 2） | 前 B / C / D | 後 A / B / C / D / 最後 A / 最後 B |
  |---|---|---|
  | 冷 p95（基準 < 2 秒） | 638 / 1089 / 636（n=13） | 835 / 751 / 799 / 735 / 1037 / 749（n=14） |
  | 温 p95（基準 < 50 ms） | **82 / 117 / 89**（n=174） | **14 / 15 / 11 / 11 / 13 / 14**（n=186） |
  | 温の p50 | 26（前 D） | 8（後 C・D） |
  | 200 回のうち `lsp_status: indexing` | 91 / 91 / 91（温の 74 回は `indexing`・`exact`、12 回は `indexing`・structural） | **0**（温 186 回すべて `ready`・`exact`） |

  - 冷の件数が 13 → 14 になったのは、前は `test/resilience.ts#scriptedFetch` の初回が `indexing`・structural で母集団の外だったため。
  - F-PY の T-07 は変わらない（温 p95 8〜9 → 8〜11、冷 p95 130〜152 → 120〜166）。
  - 判定（最後 A・B）: F-TS・F-PY とも T-07 は合格（冷・温とも）。

### 残ったこと
- T-07 の json の形で、呼び出し箇所の多いノードがある木（`client` の木は辺が 49 本）では、既定の `max_tokens` 2000 で
  ノードが 1 件も返らない件を、見たことだけ BUG-0136 に書いた（直していない。compact の形は 2 行で返る）。
- TS の incoming の冷えた要求は references の分だけ増えた（冷 p95 は前後とも 0.6〜1.1 秒で、基準の 2 秒の内）。
