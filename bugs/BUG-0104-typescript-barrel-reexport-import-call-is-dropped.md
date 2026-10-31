# BUG-0104 TS のバレル（`index.ts` の `export { flush } from './helpers'`）を経由した `import { flush } from './lib'` の `flush()` が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0100・BUG-0101 を直す役（Python の再公開をたどる直しの範囲の外を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0100 の直しを載せたコード）

```
web/lib/helpers.ts   export function flush(): number {\n  return 0;\n}
web/lib/index.ts     export { flush } from './helpers';
web/use.ts           import { flush } from './lib';\n\nexport function run(): number {\n  return flush();\n}
```

- `web/use.ts` の `relations` は 0 行（`flush()` の `CALLS` も、import 文の `IMPORTS` も無い）。`_name_calls` も 0 行。
- BUG-0100 の直し（`index/reexports.py`）は Python のファイルの束ねた名前（`_reexports`）だけをたどる。TS / JS のファイルの行は
  `_reexports` に書かれない。

## 利用者（呼ぶ側の AI）として困ること

- `web/lib/helpers.ts#flush` を変える前に T-06 / T-07 で呼び出し元を見ても、バレル（TS のパッケージの公開 API の普通の書き方）経由の
  呼び出し元は出ず、警告も無い（Python の BUG-0100 と同じ形）。

## 原因・直し方・確認（2026-10-31）

BUG-0105 と一緒に直した（どちらも再び公開した名前・`*` のたどり方の穴。BUG-0100 の仕組み〈`index/reexports.py`・`_reexports`・
`_reexport_deps`・`export_changes`〉を広げ、別の仕組みは作っていない）。

### 原因

- 抽出器は `import { flush } from './lib'` の `flush` を import 表で `web/lib/index.ts#flush` に解く。`index.ts` は `flush` を定義しておらず
  （`export { flush } from './helpers'` はシンボルにならない）、関係を整える段が「`symbols` に無い目標」として捨てていた（呼び出しも import
  文の行も）。BUG-0100 の `_reexports` は Python のファイルにしか書かれず、TS / JS のバレルはたどる手がかりが索引に無かった。

### 直し方（決めたこと）

- **TS / JS のモジュールが公開する名前を `_reexports` に書く**（`syntax/imports.js_local_exports`・`js_module_exports`、
  `engine/indexer._js_exports`）: `import`（別の定義を公開する名前の束ね先。`export { a as b } from './m'` → `m#a`、`export * as ns from './m'`
  → モジュール `m`、import した名前の `export { a }`、同じファイルの定義の別名 `export { f as g }`、既定の export〔`export default function main`・
  `export default h`・`export { x as default }`〕は `default` → そのローカルの定義、外部の指定子は `lk://_ext/<pkg>#a`）・`star`
  （`export * from './m'` のワークスペースの先）・`export`（公開する名前。既定の export は入れない）。
- **たどり方は BUG-0100 と同じ**（5 段・循環は結ばない・`P#A.rest` の `A` を置き換えて続ける）。`*` の先は TS / JS の規則（`files.language`
  で見分ける）: `m` が公開する名前（`export`）か、`m` がさらに `export *` で公開する名前。既定の export と export していない定義は公開しない
  （`_` の規則も `__all__` も無い）。`./lib` は今までどおり `./lib/index.ts` などに解く。呼び出し・参照・型の使用・import 文の行・クラスの
  メンバー（`new Calc().run()`）・名前空間 import（`lib.flush()`）・`export * as deep` の `deep.deepGo()` も定義元へ `structural`。
- **編集に追従**: `export_changes` に TS / JS の公開する名前（`export`）の増減を足した（`export` を外す・付けるだけの編集はシンボルの集合が
  変わらないため）。束ね先・`*` の先の変化は BUG-0100 のまま引く。本文・コメントだけの編集では触れない。
- **schema v11**（表は変えない）: v10 からの移行で TS / JS のファイル（と BUG-0105 の Python のファイル）を取り込み直させる。
- 範囲外: バレルの行そのもの（`export { flush } from './helpers'`）の `IMPORTS`（TS の refs.scm は再エクスポートの指定子を import として
  捕まえない。呼び出し元の答えには効かない）、CommonJS の `module.exports`、tsconfig の `paths`。直接の既定の import
  （`import main from './helpers'`）の呼び出しは前から残らない（票 BUG-0109。既定の export の束ね先 `default` は書いたので、直すときにたどれる）。
- 仕様書 T-06 処理規約 9 (a)・T-07 処理規約 11、設計書 内部表の一覧（v11）・§7.6.2（再び公開した名前・import 元の解き直し）。
  理由: バレル（`index.ts`）は TS のパッケージの公開 API の普通の書き方で、TS の LSP が無いとバレル経由の呼び出し元の大半が見えない。

### 確認（BUG-0104・BUG-0105 共通）

修正前（`origin/main` の `src`）と修正後のコードで、それぞれ新しい索引に in-process で索引（LSP なしの `structural`）して `relations`
（CALLS / REFERENCES / IMPORTS / INHERITS / TYPE_USES / IMPLEMENTS）と `_name_calls` を比べた。

F-TS（ky `3541888878`、115 ファイル。バレル `source/index.ts`）:

```
relations: 7,598 → 8,143（増えた 545・消えた 0。同じ位置の先が変わった行 0）
  増えた: CALLS 184・REFERENCES 182・TYPE_USES 94・IMPORTS 84・INHERITS 1（すべて structural・ワークスペースの先）
    先の上位: source/utils/type-guards.ts 126・source/errors/HTTPError.ts 123・source/errors/TimeoutError.ts 65・source/utils/merge.ts 62・
    source/types/options.ts 39・source/errors/ResponseSizeError.ts 34
    元の上位: test/hooks.ts 98・test/retry.ts 77・test/main.ts 73・test/http-error.ts 51・test/errors.ts 43
    例: test/errors.ts:146 isKyError(...) → source/utils/type-guards.ts#isKyError、test/errors.ts:125 new TimeoutError(...) →
    source/errors/TimeoutError.ts#TimeoutError、test/context.ts:38 isHTTPError(...) → source/utils/type-guards.ts#isHTTPError
_name_calls: 11,040 → 10,935（消えた 105・増えた 0）
  14 は同じ位置の structural の行になった（error.maxResponseSize → source/errors/ResponseSizeError.ts#ResponseSizeError.maxResponseSize など。
  受け手の型がバレル経由で分かるようになった）
  91 は受け手の型が分かって行が無くなったもの: 組み込み型の名前（push 29・at 22・length 14・filter 6・slice 2・entries・toString・get。
  名前の一致はもともと結ばない）、外部の基底 Error から継承した message 11・cause 2（定義元から直接 import したときと同じ扱い）、
  型の別名を注釈にした受け手の options.retry 2（直接 import しても同じく残らない。票 BUG-0108）
_reexports: TS の行 export 158・import 45・star 4
```

Lucianke 自身（`19922de` のスナップショット 805 ファイル。新しいフィクスチャを含む）:

```
relations: 96,355 → 96,394（増えた 39・消えた 0）。増えたのは tests/fixtures/reexports/web/use.ts 26・star_use.py 13 だけ
_name_calls: 23,584 → 23,578（消えた 6 はすべて同じ位置の行になった。star_use.py の Widget().spin()・ITEM.spin()・TokenEstimator().count()）
Lucianke 自身のコードに import する側の `from x import *` と TS のバレルは無い（`*` は reexports フィクスチャの pkg/__init__.py だけ）。
dynamic_all は pkg/dyn.py の 1 件だけ。走査の時間 31.2 秒 → 31.7 秒・索引の大きさ 49.5 MB → 49.5 MB（差は測れない）
```

F-PY（httpx `b5addb64f0`）: relations 9,232 → 9,232・_name_calls 2,598 → 2,598（変わらない。`*` は再び公開する側の `__init__.py` だけ）。

テスト（決定的。フィクスチャ `tests/fixtures/reexports` に足した。バイト列のまま `-text`。README に各箇所の意図）:

- `tests/unit/test_engine_indexer_reexports.py`（足した 12 件）: TS のバレル（1 段・別名・`export *`・`export * as deep` の `deep.deepGo()`・2 段・
  ディレクトリの `index.ts`・名前空間 import・クラスの構築とメソッド・3 段・同じファイルの定義の別名・既定の export の明示の再公開・
  型の使用・import 文の行）、`export *` は既定の export と export していない関数を公開しない・循環、`_reexports` の TS の行、編集で結び直す
  （バレルの再公開の先を変える・`export *` を消す／戻す・終わりのファイルの `export` を外す・`deepGo` を消す／戻す・本文だけの編集では
  取り込み直さない）、import する側の `*`（リテラルの `__all__`・`__all__` なし・さらに再び公開する `pkg`・クラスの構築とメソッド・
  変数の型・基底・組み込み名はそのまま）、公開しない名前は行を作らず型の分からない受け手は名前だけ、リテラルでない `__all__` は
  `heuristic`（リテラルにすると `structural`）、`*` の先の編集で結び直す（関数を足す・消す・`__all__` に足す）、v10 → v11 の移行。
  v9 → v10 の移行の試験は、続く v11 が TS を取り込み直させるので期待を直した。
- `tests/contract/test_T06_T07_reexports.py`（足した 2 件）: T-06 の `helpers.ts#flush`・`deepGo`（3 つの書き方）、T-07 の `add` の
  incoming・`run` の outgoing（バレルの無い先・循環・既定の export・export していない関数は出ない）、import する側の `*` の `flush`・
  `public` は `structural`・`dyn_fn` は `heuristic`・`hidden` は出ない。
- `tests/unit/test_syntax_imports.py`（足した 16 件）: `module_all_dynamic`、`js_local_exports`（宣言・`export { … }`・既定の export）、
  `js_module_exports` の行（外部の指定子・外部の `export *` は書かない）。
- `tests/unit/test_index_core.py`: スキーマの版の見張りを 11 に。
- 直す前のコードでは足した試験のうち 15 件が赤（`test_ts_barrel_star_skips_default_unexported_and_cycles` は直す前も行が無いので緑）。
- `uv run ruff check`（引数なし）: クリーン。
- 関係・import・索引器・再公開・名前の呼び出し・継承・受け手・スキーマ・T-04・T-06・T-07 の単体と契約（38 ファイル）: `619 passed, 6 skipped`（skip は TS の LSP が無い PC での exact の試験）
- `tests/contract_fixtures`: `45 passed, 6 skipped`（skip は TS の LSP が無い PC での F-TS の exact の事後条件 5 件と、F-TS には当てはまらない BUG-0079 の試験 1 件。F-TS の structural の事後条件は満たす）
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の不具合（見たことだけの票）: **BUG-0108**（TS の型の別名を注釈にした受け手の `o.retry` が `relations` にも `_name_calls` にも
  残らない）、**BUG-0109**（TS の既定の import `import main from './helpers'` の `main()` が残らない）。
