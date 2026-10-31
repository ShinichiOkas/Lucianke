# BUG-0129 F-POLY の T-07 で `LimitTable.__init__`・`LimitTable.get` の呼び出しが何度も 10 秒（予算）かかり、その後 `lsp_status` が `indexing` になる

見たことだけを書く（調査していない）。2026-10-31 16:52 ごろ、BUG-0058 の直す役が `b850152`（BUG-0058 の直しの後）で
`bench/run_latency.py --fixtures poly,F-PY,F-TS --calls 200 --tools find_references_and_implementations,query_call_hierarchy,get_diagnostics`
を流したときに見た（typescript-language-server は bun 経由。ほかの役の負荷で CPU 85〜100%）。

## 症状
- T-07 `query_call_hierarchy`（`direction: incoming`、`depth: 2`）の F-POLY の 200 回のうち、`src/shop/services/limits.py#LimitTable.__init__` と
  `src/shop/services/limits.py#LimitTable.get` の呼び出し（24 回）の値（ms、`i` は呼び出しの番号、括弧は `lsp_status`）:
  `i=2` 938（indexing）、4: 568（indexing）、32: 606（indexing）、**34: 10005、41: 10025、44: 10038、54: 10008、59: 10006**（ready）、
  73: 7636（indexing）、81: 12、93: 7、98: 507（indexing）、106: 463（indexing）、109: 7、123: 487（indexing）、134: 434、139: 559、143: 515、
  146: 11、147: 535、163: 428、188: 5、189: 439（indexing）、199: 7。どれも `confidence_level: exact`・`is_degraded: false` で返った。
- 10 秒は T-07 の 1 問い合わせの LSP の予算（`EXACT_BUDGET_S = 10.0`）と同じ。10 秒の呼び出しは同じ 2 つの対象に 5 回続けて出た（i=34〜59）。
- 同じ計測の `bfe71c3`（直しの前）でも、`PaymentProcessor.__init__`・`LimitTable.get`・`LimitTable.__init__` などで 10 秒の呼び出しが
  多数あった（F-POLY の T-07 の p95 10033 ms）。直しの後は p95 568 ms だが、上の 10 秒は残った。
- 2 回目以降に 5〜12 ms で返る回（PaaD から）と、400〜900 ms で `lsp_status: indexing` の回が交互に出る。
- F-PY・F-TS では T-07 の最大は 1.6 秒・0.65 秒（10 秒の呼び出しは無かった）。

## 再現
上の計測を `--fixtures poly --tools query_call_hierarchy` で流し、結果 JSON の `runs[0].tools.query_call_hierarchy.raw.samples` を対象で絞る。
結果 JSON: 直す役の作業ツリーの外（使い捨ての checkout）の `bench/results/20261031-165213-windows-latency.json`（push していない）。

## 仕様書
§11 T-07、§14.4。関連: BUG-0058、BUG-0091（T-07 の予算）、BUG-0123（予算で打ち切った要求を裏で待つ）。

## 原因・直し方・確認（2026-10-31）

### 原因
1. **10 秒（予算）**: F-POLY の `pyproject.toml` に `[tool.pyright]` が無く、`pyrightconfig.json` も無いので、basedpyright 1.40.2 は設定を親のディレクトリへ
   探しに行き（basedpyright のコードの注: pyproject.toml が見つかっても `[tool.pyright]` の節が無ければ、無いものとして親を探す）、Lucianke のリポジトリの根の
   `pyrightconfig.json`（`include: ["src", "tests"]`、`exclude` に `tests/fixtures`）を採っていた。解析の対象は Lucianke 自身の `src`・`tests` になり、
   F-POLY のファイルは対象外（開いた文書だけ）。`callHierarchy/incomingCalls` は対象のファイルを全部走査して名前の一致する呼び出しを型で確かめるので、
   `get` のようなありふれた名前では Lucianke 自身のコードの `.get(` を一つずつ確かめていた。basedpyright に直接送って確かめた（Lucianke を通さない）:
   リポジトリの中の F-POLY では 120 秒で返らず、リポジトリの `pyrightconfig.json` だけ退けても 38.6 秒で答えは `null`（`[tool.basedpyright]` を採る）、
   F-POLY をリポジトリの外にコピーすると 0.33 秒で呼び元（payment.py の `_validate_limit`）が返った。basedpyright は要求を 1 本ずつしか処理しないので、
   T-07 は 1 問い合わせの予算（10 秒）で切れ、予算の後も裏で答えを待つ要求（BUG-0123）に同じノードの次の問い合わせが合流して、また 10 秒待っていた。
2. **`lsp_status: indexing`**: basedpyright の incomingCalls は名前で呼び出しを探すので、`__init__` の項目に聞くとコンストラクタの呼び出し（`LimitTable()`）を
   1 つも返さない（直接送って 0.14 秒で `null`）。索引には構文木の `CALLS` があるので、T-07 は「LSP が 0 件と答えたが索引には呼び出しがある」を
   「LSP が解析中」と取り違えて、その部分を構文木で補い `lsp_status: indexing` と報告していた（`LimitTable.__init__`・`PaymentProcessor.__init__`）。

### 直し方（決めたこと）
- 1: F-POLY に `pyrightconfig.json`（`typeCheckingMode` だけ）を置いて、basedpyright の設定の根をフィクスチャに止めた。F-POLY は独立したプロジェクトの
  見本で、リポジトリの中にあるのはこのリポジトリの都合なので、フィクスチャの側で直す（`pyproject.toml` に `[tool.pyright]` を足すと TOML の骨格の
  試験の見本〈`tool.ruff` だけの暗黙の表〉が変わるので、別のファイルにした）。生成元 `tests/fixtures/make_fpoly.py` にも足し、F-POLY の全ファイルの表
  （`tests/unit/test_workspace_languages.py`）に足した。`test_T06_T07_lsp.py` の `real`（リポジトリの外への複製）は、import の根の検出を通す元の意図の
  とおり `pyrightconfig.json` を複製しない。`3f5a14b`・`c51f7bb`
- 2: T-07 の Python の `__init__` / `__new__` の incoming は、持ち主のクラスの名前の位置で `prepareCallHierarchy` を送る（`_lsp_exact._constructor_owner`）。
  クラスの項目に聞くと、コンストラクタの呼び元が返る（モジュール直下の呼び出しは、他のノードと同じく囲みの関数が無いので木のノードにならない）。`c51f7bb`
- 一般の使い方で同じこと（ワークスペースの根に basedpyright の設定が無く、親に設定がある）が起きうる件は、見たことだけを BUG-0132 に書いた（直していない）。

### 確認
`bench/run_latency.py --fixtures poly --calls 200 --tools find_references_and_implementations,query_call_hierarchy,get_diagnostics`（typescript-language-server は bun
経由。サーバー内 p50 / p95 / max、ms。前は BUG-0058 の直しの後の `b850152` の 2 回、後は `c51f7bb` の 2 回）:

| ツール | 前（b850152） | 後（c51f7bb） |
|---|---|---|
| T-07 `query_call_hierarchy` | 7 / 568 / 10038、7 / 564 / 10026（5 秒以上 6 回・5 回、`indexing` 13 回ずつ） | **6 / 19 / 223、6 / 18 / 219**（5 秒以上 0 回、`indexing` 0 回） |
| T-06 `find_references_and_implementations` | 6 / 20 / 146、5 / 20 / 144 | 6 / 17 / 26、6 / 15 / 28 |
| T-08 `get_diagnostics` | 4 / 10 / 2003、6 / 18 / 2032 | 4 / 7 / 475、4 / 9 / 481 |

- `LimitTable.get`・`LimitTable.__init__` の最大は 219〜223 ms（冷えた最初の 1 回）。T-07 の 200 回は `ready`・`exact` 183 回と、TS の LSP が使えない
  `web/src/app.ts` の構造の 17 回だけになった（T-06 の `indexing` の 1 回は `web/src/app.ts#checkout`。TS の LSP の起動の直後）。
- 試験: `tests/contract/test_T06_T07_lsp.py` に、実の basedpyright で `LimitTable.__init__` の incoming が `exact`・`ready` で `test_charge_ok` を返す試験を足した
  （直しの前は赤）。F-POLY を使う unit 1161 件・contract 820 件、T-06・T-07 の関係する試験 123 件と見張り 4 本（`test_import_graph`・
  `test_server_launch_guard`・`test_stdout_purity`・`test_leak_guard`）が green。生成元から作った木はフィクスチャとバイト単位で一致。

### 残ったこと
- BUG-0132（一般の使い方で親の設定を採る件。見たことだけ）。
- T-06 の `__init__` の参照（basedpyright の references もコンストラクタの呼び出しを `__init__` の参照として返さないかもしれない）は確かめていない。
