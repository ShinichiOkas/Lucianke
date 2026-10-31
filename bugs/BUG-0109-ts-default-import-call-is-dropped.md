# BUG-0109 TS の既定の import（`import main from './helpers'`）の `main()` が、`relations` にも `_name_calls` にも残らない（import 文の行も無い）

- 報告: 2026-10-31 / 見た人: BUG-0104・BUG-0105 を直す役（バレルの既定の export の再公開を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0104 の直しを載せたコード）

```
helpers.ts        export default function main(): number {\n  return 1;\n}\n
use_default.ts    import main from './helpers';\n\nexport function run(): number {\n  return main();\n}\n
```

- `symbols` に `helpers.ts#main`（Function）がある。
- `use_default.ts` の `relations` は 0 行（`main()` の `CALLS` も、import 文の `IMPORTS` も無い）。`_name_calls` も 0 行。
- 名前つきの再公開 `export { default as mainFn } from './helpers'` を経由した `import { mainFn } from './lib'` の `mainFn()` は、BUG-0104 の直しで
  `helpers.ts#main` に `structural` で結ばれる（`tests/fixtures/reexports/web/use.ts`）。直接の既定の import だけが残らない。
- F-TS（ky）のテストは `import ky from '../source/index.js'`（`source/index.ts` の `export default ky`）で本体を使っている。

## 利用者（呼ぶ側の AI）として困ること

- 既定の export の関数・クラスを変える前に T-06 / T-07 で呼び出し元を見ても、既定の import で使っている呼び出し元は出ず、警告も無い。

## 原因・直し方・確認（2026-10-31）

BUG-0108 と一緒に直した（どちらも TS の `structural` の名前解決の穴）。

### 原因

- 索引器の import 表の名前 → URN（`engine/indexer._relations.urn_of`）が、ES の既定 import（`import main from './helpers'` は
  `imported_name_map` で `(helpers.ts, "default")`）を `None` にしていた。`main()` の呼び出し・参照・型の使用・import 文の行の候補が
  どれも先を持たず `relations` に入らなかった（受け手の属性でもないので `_name_calls` にも入らない）。BUG-0104 で `_reexports` に既定の
  export の束ね先（`default` → `helpers.ts#main`）が書かれていたが、抽出器が `helpers.ts#default` を作らないのでたどられなかった
  （バレルの `export { default as mainFn }` 経由だけが結ばれていた）。

### 直し方（決めたこと）

- `urn_of` は既定 import を `helpers.ts#default` にする。整え（`index/reexports.redirect_reexports`）が BUG-0100 / BUG-0104 と同じ
  たどり方で既定の export の定義まで書き換える: `export default function main`・`export default class C`・`export default h`
  （同じファイルの定義）・`export { x as default }`・バレルの `export { default } from './m'`・`export { default as foo } from './m'`・
  `import main, { other } from`・`import * as m` の `m.default()`。呼び出し・参照・型の使用（`w: Widget`）・構築とメソッド
  （`new Widget().spin()`・`w.spin()`）・import 文の行すべて `structural`。
- 無名の既定の export（`export default () => …`・`export default { … }`。既定の export が無いモジュールも）はシンボルが無いので、
  たどった先が `P#default` のまま残る TS / JS のモジュールなら、import 文の行・呼び出し・参照・型の使用の先をモジュール `P`
  （ファイルの URN）にする（`ReexportResolver.anonymous_default`。黙って捨てない。T-07 では `anon.ts (module)` のノード。
  `INHERITS` / `IMPLEMENTS` は今までどおり消す）。無名のオブジェクトのメンバー `cfg.level` は名前だけ（`_name_calls`。BUG-0094 の
  規則のまま）。
  - 決めた理由: 「呼び出し箇所を未解決として報告」の手段に `_name_calls` は使えない（名前の一致の候補はメソッド・クラスの
    メンバーで、素の `anon()` を同名のメソッドに当てうる）。モジュールを指す行は T-07 で「そのモジュールの既定の export を呼ぶ」と
    見え、Python のモジュールの読み（BUG-0093）と同じ形。
- 編集に追従: 既定の export の束ね先の変化は `export_changes` の束ねた名前 `default` の変化として引く（たどるときに
  `(helpers.ts, default)` が `_reexport_deps` に入る）。別の定義に変える・無名に名前を付ける・外す、どれも import している側を
  取り込み直す。本文だけの編集では触れない。
- schema v12（BUG-0108 の `_type_aliases` と一緒）: v11 からの移行で TS / JS のファイルを取り込み直させる。
- 仕様書 T-06 処理規約 9 (a)・T-07 処理規約 11、設計書 内部表の一覧（v12）・§7.6.2 に理由つきで書いた。理由: 既定の import は
  TS / JS の普通の書き方で、LSP が無いと呼び出し元が import 文の行ごと消えていた。
- 同じ経路で、Python の `from x import default`（`default` という名前の定義）も結ばれるようになった（前は同じく `None`）。

### 確認（BUG-0108・BUG-0109 共通）

修正前（`origin/main` `50b712a` の `src`）と修正後のコードで、それぞれ新しい索引に in-process で索引（LSP なしの `structural`）して
`relations`（全種別）と `_name_calls` を比べた。

F-TS（ky `3541888878`）:

```
relations: 8,143 → 9,561（増えた 1,418・消えた 0。同じ位置の先が変わった行 0。すべて structural）
  BUG-0109（既定の import）1,367: CALLS 710・REFERENCES 617・IMPORTS 40
    source/index.ts#ky（`export default ky`。テストの `import ky from '../source/index.js'`）1,348: ky(...) の CALLS 696・
    ky.get(...) などの受け手の読み REFERENCES 617・import 文の行 35
    source/utils/delay.ts#delay・timeout.ts#timeout・is-network-error.ts#isRawNetworkError（`export default async function …`）19:
    例 source/core/Ky.ts:1244 timeout(...) → source/utils/timeout.ts#timeout、test/errors.ts:211 isRawNetworkError(...) →
    source/utils/is-network-error.ts#isRawNetworkError、source/core/Ky.ts:1138 delay(...) → source/utils/delay.ts#delay
  BUG-0108（型の別名の受け手）51:
    lk://_ext/express#Express.get / post / put / all 44（test/browser.ts の `server: ExtendedHttpTestServer`、
    `type ExtendedHttpTestServer = { http; url; … } & express.Express`。型リテラルに無いメンバーは外部の構成のメンバー）
    source/types/options.ts#Options.signal / headers 7（source/core/Ky.ts の `this.#options.signal`、`#options: InternalOptions`、
    `type InternalOptions = Omit<Options, …> & {…}` → `Options` のメンバー）
_name_calls: 10,935 → 10,949（消えた 3・増えた 17）
  消えた 3: 同じファイルの型リテラルの別名の受け手のメンバー（source/utils/timeout.ts:28 `options.timeout`〈`type TimeoutOptions =
    { timeout; fetch }`〉・test/request-body-serialization.ts:25-26 `received.bytes` / `received.contentLength`）。前は同じファイルの
    別名を型の分からない受け手として名前だけにしていた。別名自身のメンバー（シンボルが無い）と分かったので行を作らない
  増えた 17: 前は行も名前だけの行も無く捨てていた別名の受け手で、構成の中身が分からないもの（`this.#options.body` 6・`duplex` 1 は
    `Options` の基底 `RequestOptions` が写像型 `{[Key in …]?: RequestInit[Key]}`、`#calculateDelay` の `retry.delay` / `jitter` /
    `backoffLimit` 6 は注釈 `InternalOptions['retry']` を `InternalOptions` と読んだもの〈票 BUG-0113〉、ほか replace 2・length・size・trim）
_type_aliases: 207 行。走査の時間 4.3 秒 → 4.6 秒（5 回ずつの中央値。行が 17% 増えた分）
```

- 票 BUG-0108 の `test/resilience.ts:111` の `options.retry`: `options: Options`（`interface Options extends KyOptions, RequestOptions`）の
  `retry` は `type KyOptions = { …; retry?: …; … }` の型リテラルのメンバーで、シンボルが無いので結ぶ先が無い（行も名前だけの行も
  作らない。別の型の同名に当てない）。TS の型リテラルのメンバーがシンボルにならないことは票 BUG-0114 に書いた。

F-PY（httpx `b5addb64f0`）: relations 9,232 → 9,232・_name_calls 2,598 → 2,598（変わらない）。

Lucianke 自身（`50b712a` のスナップショット + 新しいフィクスチャ）: relations 96,441 → 96,469（増えた 28 はすべて新しいフィクスチャ
`tests/fixtures/reexports/web/defaults`・`web/aliases`）、_name_calls 23,585 → 23,585（入れ替わった 4 も新しいフィクスチャだけ）。

テスト（決定的。フィクスチャ `tests/fixtures/reexports/web/defaults/`・`web/aliases/` を足した。バイト列のまま `-text`。README に各箇所の意図）:

- `tests/unit/test_engine_indexer_reexports.py`（足した 7 件）: 既定の import の書き方すべて（`export default function`・`class`・
  `export default h`・`export { x as default }`・混ぜた import・バレルの `export { default } from` / `export { default as Widget } from`・
  `import * as m` の `m.default()`・型の使用・注釈つきの受け手・import 文の行）、無名の既定の export はモジュールへ（バレルの転送も。
  `#default` は残らない）、既定の export の編集で結び直す（別の定義に変える・無名に名前を付ける・戻す）、型の別名の受け手
  （別名 = クラス・交差と型リテラルのメンバー・合併〈1 つ / 2 つ〉・`null` / `undefined` の混ざった合併・総称の別名・`Partial`・
  別名の別名・別名を継承したインターフェース・同じファイルの別名・関数型と添字のシグネチャは名前の一致）、`_type_aliases` の行、
  別名・構成の編集で結び直す（別名の構成を変える・構成の型のメンバーを消す / 戻す・合併の持つ構成が 1 つになると名前だけの行が
  `structural` の行に変わる）、v11 → v12 の移行。
- `tests/contract/test_T06_T07_reexports.py`（足した 2 件）: T-06 の `fn.ts#main` の参照に既定の import・バレル・`m.default()`・import 文の行、
  T-07 の `useDefaults` の outgoing に既定の export の定義と無名の既定の export のモジュールのノード、T-06 の `Base.retry` の参照に
  別名の受け手の読み（`e.shared` は出ない）。
- `tests/unit/test_index_schema.py`・`tests/unit/test_index_core.py`: `_type_aliases` とその索引、版 12、古い DB の模擬で落とす表。
- 直す前のコードでは足した試験 7 件（単体）が赤。
- `uv run ruff check`（引数なし）: クリーン。
- 関係・import・索引器・再公開・名前の呼び出し・継承・受け手・取り込み・スキーマ・T-03 / T-04・T-06・T-07 の単体と契約（39 ファイル）:
  `664 passed, 6 skipped`（skip は TS の LSP が無い PC での exact の試験）。リベース後の数は下の「リベース後」に書いた。
- `tests/contract_fixtures`: 下の「リベース後」に書いた。
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の不具合（見たことだけの票）: **BUG-0113**（TS の受け手の注釈 `Opts['retry']`〈添字の型〉・`Opts[]` を `Opts` と読み、
  `r.limit` を `Opts.limit` に結ぶ）、**BUG-0114**（TS の型リテラルの別名のメンバー〈`type KyOptions = { retry?: … }` の `retry`〉が
  シンボルにならず、T-06 の対象にできない・読みを結べない）。

リベース後（`origin/main` `bc2ffa6`〈BUG-0107 を含む〉の上）: 関係・import・索引器・再公開・名前の呼び出し・継承・受け手・取り込み・スキーマ・T-03 / T-04・T-06・T-07 の単体と契約（40 ファイル）`677 passed, 6 skipped`、`tests/contract_fixtures` `45 passed, 6 skipped`（skip は TS の LSP が無い PC での F-TS の exact の事後条件と、F-TS には当てはまらない試験。F-TS の structural の事後条件は満たす）。
