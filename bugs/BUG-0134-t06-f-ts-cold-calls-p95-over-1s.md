# BUG-0134 F-TS の T-06 の、その実行で初めての対象（冷）の p95 が 1 秒を超える

見たことだけを書く（調査していない）。2026-10-31 18:07〜、BUG-0058 の直す役が `3c015c9`（M1-3 の判定を冷・温に分けた形。仕様書 §17 OD-23 (b)）で
`bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200` を流したときに見た。typescript-language-server 4.3.3 / TypeScript 5.6.3 は bun 経由
（この PC に node が無い）、ky には `bun install --ignore-scripts` で依存を入れた。CPU の `cpu_busy_pct` は F-TS 25.7。結果 JSON は直す役の作業ツリーの
`bench/results/20261031-180657-windows-latency.json`（push していない）。

## 症状
- T-06 `find_references_and_implementations`（`query_type: references`）の F-TS: **冷 n=14 p50 169 / p95 1234 / max 1234 ms**（1 秒以上で不合格）、
  温 n=186 p50 11 / p95 22 ms（合格）。同じ計測の F-PY の冷は p95 48 ms、F-POLY の冷は p95 21 ms。
- 冷の値（ms、`i` は呼び出しの番号）: `source/utils/delay.ts#delay` 1234（i=3）、`source/utils/type-guards.ts#isKyError` 994（i=44）、
  `source/utils/timeout.ts#timeout` 546、`source/utils/type-guards.ts#isNetworkError` 543、`test/helpers/with-performance.ts#withPerformance` 458、
  `source/utils/type-guards.ts#isForceRetryError` 272、それ以外は 47〜171。
- BUG-0058 の計測でも F-TS の冷の p95 は 1358 / 1276（`b850152`）・2137（`07b2c06`）・1032（`0965fec`）ms で、冷の対象は 14〜15 件しかないので、
  最も遅い 1 件で p95 が決まる。

## 再現
上の計測を `--fixtures F-TS --tools find_references_and_implementations` で流し、結果 JSON の
`runs[0].tools.find_references_and_implementations.raw.samples` を `first == true` で絞る。

## 仕様書
§14.4（冷は p95 < 1 秒）、§3.2 NFR-PERF-1、§15.2 M1-3、§11 T-06。関連: BUG-0058、BUG-0047（tsconfig の外のファイルを開いてから聞く）。

## 原因・直し方・確認（2026-10-31）

### 原因
ky を相手に、票の冷えた対象（`delay`・`isKyError`・`withPerformance`・`scriptedFetch`・`isForceRetryError`・`timeout`・`isNetworkError`・
`newHookValue`）を新しいサーバーで順に 1 回ずつ聞き、T-06 の各段の所要のログ（`lsp_exact_slow`）を見た。tsserver を bun と node
（`nodejs-wheel-binaries` 22.12.0 を一時的に入れて `node cli.mjs --stdio`）の両方で走らせて比べた。

| 対象 | bun（全体 / references / sites） | node（全体 / references / sites） |
|---|---|---|
| `delay` | 859 / 703 / 63 | 750 / 594 / 78 |
| `isKyError` | 859 / 156 / **687** | 859 / 125 / **703** |
| `withPerformance` | 90 / — / — | 1516 / **1485** / 0 |
| `isForceRetryError` | 563 / 47 / **484** | 844 / 375 / **438** |
| `timeout` | 1500 / **1454** / 0 | 735 / **688** / 0 |

1. **tsserver の references そのもの（LSP の側。Lucianke では縮められない）**: T-06 は対象の名前を含むファイルを先に開かせる（BUG-0047）。
   新しく開いたファイルで tsserver は推定プロジェクトを組み直し、次の references で型の検査器を作り直すので、その回だけ 0.6〜1.5 秒
   かかる。どの対象の呼び出しで組み直しが起きるかは、それまでに開いたファイルで決まる（`delay`・`timeout` のように名前が `retry: {delay}`・
   `timeout:` などのキーとして多くのテストに出る対象で起きやすい）。node でも同じ程度で、bun 由来の遅さではない（重い回が入れ替わるだけ）。
2. **参照箇所の分類（Lucianke の側）**: 参照元のファイルを構文木にかけて `extract_relations` で囲み・使用種別を引く（BUG-0058 の 2.）。
   内容ごとに覚えるが、大きなテストファイルの初回は 1 ファイル 150〜300 ms（`test/hooks.ts` 8063 行で 285 ms、`test/main.ts` 160 ms、
   `test/retry.ts` 145 ms。負荷の下で約 2 倍）で、それらを参照元に持つ最初の対象（`isKyError`）で 0.5〜0.7 秒になっていた。
   references の答えを待ってから分類を始めていた。

### 直し方（決めたこと）
- 2: references を送ってから答えを待つ間に、索引で対象を参照しているファイル（`queries.referencing_paths`。参照の多い順に 16 件）の分類を
  始める（`SiteIndex.start`。後の `of` が同じ分類を待つ）。答えは変わらない（分類は内容だけで決まり、内容ごとに覚える）。prime（開かせる
  ファイルを探す）の前に始めると、分類が GIL を持つので prime が遅れて要求を送るのが遅れた（`isKyError` の prime 31 → 625 ms）ので、
  要求を送る直前に始める。`d45d42d`
- 1 は直していない。採らなかったこと:
  - 起動のとき・最初の問い合わせで tsconfig の外のファイルを全部開かせる（組み直しを 1 回にまとめる）: 最初の呼び出しが数秒になり、
    `didOpen` の上限（200）を超えるリポジトリでは閉じたファイルでまた組み直す。ベンチの冷えた値を、測らない最初の呼び出しに寄せるだけになる。
  - 開かせるファイルを「キーやメンバーとしてしか名前が出ないファイル」で絞る: 名前空間の import（`ns.delay`）・三項演算子の `? delay :` など
    を字句で見分けられず、exact の答えから参照が黙って落ちる。
  - 索引器の抽出の結果（解決つき）を分類の覚えに流用する: 解決の有無で同じ位置の候補が変わりうるので、覚えの有無で答えが変わる。

### 確認
- probe（bun の tsls、上と同じ順の 8 対象、サーバー内 ms）: `isKyError` 859〜894 → 685、`isForceRetryError` 563〜887 → 457。`delay` 656・
  `timeout` 1459 は tsserver の references（1.）。
- 試験: T-06・T-07 の関係する試験 169 件と見張り 4 本が green（分類を先に始めても答えは同じ）。
- ベンチ（BUG-0133 の確認と同じ 9 回。条件・順・負荷はそちら）。F-TS の T-06 の冷（n=14。p95 はいちばん遅い 1 件で決まる）の上位 3 件と
  中央値（ms）:

  | 回 | 冷 p95 | 2・3 番目 | 冷の中央値 | 温 p95 |
  |---|---|---|---|---|
  | 前 B | 1623（`delay`） | 805 `withPerformance`・691 `isNetworkError` | 245 | 19 |
  | 前 C | 1394（`delay`） | 714 `scriptedFetch`・667 `withPerformance` | 222 | 26 |
  | 前 D | 1350（`delay`） | 771 `withPerformance`・652 `timeout` | 266 | 19 |
  | 後 A | 1512（`delay`） | 810 `withinTwoSeconds`・615 `timeout` | 168 | 23 |
  | 後 B | 2483（`delay`） | 1607 `scriptedFetch`・622 `timeout` | 230 | 23 |
  | 後 C | 1729（`delay`） | 600 `timeout`・533 `scriptedFetch` | 254 | 20 |
  | 後 D | 1132（`withPerformance`） | 638 `timeout`・542 `delay` | 228 | 20 |
  | 最後 A | 1426（`withPerformance`） | 818 `timeout`・606 `delay` | 214 | 21 |
  | 最後 B | 1095（`withPerformance`） | 767 `timeout`・550 `delay` | 176 | 20 |

  - いちばん遅い 1 件はどの回も tsserver が組み直す回（1.）で、p95 は前後とも 1.1〜2.5 秒（**不合格のまま**）。2 番目以下は前後とも
    0.5〜0.8 秒（後 B の `scriptedFetch` 1607 を除く）。`isKyError` の冷は票の 994 ms → 後の回で 354〜498 ms（上位 4 件に入った後 C・D・最後 B）。F-PY の T-06 は前後とも合格
    （冷 p95 82〜196・温 p95 7〜21）。

### 残ったこと
- 1.（tsserver が開かせたファイルでプロジェクトを組み直す回の 0.6〜1.5 秒）。冷えた対象が 14 件しか無いので、p95 はいちばん遅い 1 件で
  決まり、組み直しが計測の区間に入ると 1 秒を超える。node でも同じなので、受入の機材（48 論理 CPU・空き）での値を見て判断する。
