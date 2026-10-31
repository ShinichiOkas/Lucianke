# BUG-0108 TS の型の別名（`type Options = Base & {...}`）を注釈にした受け手の `o.retry` が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0104・BUG-0105 を直す役（F-TS〈ky〉の修正前後の差を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0104 の直しを載せたコード）

```
types.ts      export interface Base {\n  retry: number;\n}\n\nexport type Options = Base & { extra: string };\n
use.ts        import type { Options } from './types';\n\nexport function f(o: Options): number {\n  return o.retry;\n}\n
untyped.ts    export function g(o: any): number {\n  return o.retry;\n}\n
```

- `symbols`: `types.ts#Base`（Interface）・`types.ts#Base.retry`（Property）・`types.ts#Options`（Type）。
- `use.ts#f` の `relations` は `TYPE_USES` → `types.ts#Options` の 1 行だけ。`o.retry`（4 行目）の `REFERENCES` は無く、`_name_calls` にも無い。
- 注釈の無い `untyped.ts` の `o.retry` は `_name_calls` に `('retry', 2, 'r')` で残る（名前の一致の候補になる）。
- F-TS（ky）: BUG-0104 の前は `test/resilience.ts:111` の `options.retry`（`Options` をバレル `source/index.ts` から import）が `_name_calls` に
  2 行あった。BUG-0104 の後は `Options` が定義元 `source/types/options.ts#Options` に解けて、2 行とも `relations` にも `_name_calls` にも無い。

## 利用者（呼ぶ側の AI）として困ること

- `Base.retry` を変える前に T-06 で参照を見ても、型の別名の受け手からの読みは出ず、警告も無い（注釈が無い方が名前の一致で見える）。

## 原因・直し方・確認（2026-10-31）

BUG-0109 と一緒に直した（どちらも TS の `structural` の名前解決の穴）。

### 原因

- 抽出器は `o: Options` の `o.retry` を `types.ts#Options.retry` まで決める（受け手の型は注釈の名前）。`Options` は型の別名
  （`kind = 'Type'`）で、関係を整える段の退避（`index/inherited.park_member_refs` の `_receiver_class`）はクラス・インターフェース・
  モジュール直下の変数しか受け手にしないので、`symbols` に無い目標として消していた（名前だけの行にもしない）。別名が何でできて
  いるか（`Base & {...}`）は索引のどこにも無かった。同じファイルの別名を注釈にした受け手は、逆に型の分からない受け手として
  名前だけにしていた（`_name_ref` が `Type` を型として返さない）。

### 直し方（決めたこと）

- 抽出器が TS の `type A = …`（シンボルになったもの）の構成を内部表 `_type_aliases`（schema v12）に書く
  （`syntax/relations._Extractor.type_aliases`）: 値の名前の型・交差・合併を書いた順に平らにし（括弧の中の同じ演算子も）、`op` は
  `and`（別名そのもの・交差）/ `or`（合併）。構成は `type`（型の URN。同じファイルのクラス・インターフェース・別名・列挙、import
  した名前、標準のグローバル）・`member`（別名自身の型リテラルのメンバー `<別名>.<名前>`）・`opaque`（関数型・条件型・写像型・
  添字のシグネチャ・`string`・別名の型引数・合併の中の交差など）。`null` / `undefined` / `void` / `never` は書かない。総称の型
  `Base<T>` は `Base`、メンバーを持ち越す標準の型 `Partial` / `Required` / `Readonly` / `NonNullable` / `Pick` / `Omit` は第 1 型引数
  （同じファイル・import に同名があれば素通りしない）。同じファイルの別名を注釈にした受け手も `<別名>.name` にする。
- 整え: 目標の「クラスの部分」が別名なら `_member_refs` に退避し、`MemberResolver.lookup_alias` が構成から探す:
  - `and`: 書いた順に最初にメンバーを持つ構成（クラス・インターフェースは C3 の順、別名はさらにその構成）。別名自身の型リテラルの
    メンバーは索引にシンボルがあればそれに結び、無ければ結ばない（`inline`。別の型の同名にも当てない）。どれにも無ければ外部の型の
    構成のメンバー（`& express.Express` → `lk://_ext/express#Express.get`）、中身の分からない構成があれば名前だけ（`_name_calls`）。
  - `or`: メンバーを持つ構成がちょうど 1 つで、中身の分からない構成（外部の型を含む）が無いときだけ `structural`。2 つ以上・中身の
    分からない構成があれば名前だけ（名前の一致が `heuristic` / 曖昧を決める）。
  - 構成が書かれていない（Python の別名・v12 より前の取り込み）・循環・深すぎる（8 段）は名前だけ。
  - インターフェースが別名を継承している（`interface Options extends KyOptions`）ときも、C3 の線形化の途中の別名を同じく構成から
    探す（分からなければ今までどおりそこで止め、「結ばなかった」と T-06 / T-07 が書く）。決めの「受け手の型が名前で指すインター
    フェース」はもともと C3 で扱っていた（変えていない）。
- 構成の型がバレル・既定の import の名前（`index.ts#Base`・`m.ts#default`）なら、`redirect_reexports` が `_type_aliases.target_urn` も
  定義元に書き換える（依存も `_reexport_deps` に覚える）。
- 編集に追従: 結び直す範囲の再帰 CTE（`resolve_member_refs`）に、整えるファイルの別名と、構成にそのクラス（派生クラス）・別名を
  持つ別名（`_type_aliases.target_urn` を逆にたどる）を足した。別名の構成を変える・構成のインターフェースのメンバーを足す / 消す
  （別のファイル）で、import し直さずに受け手が結び直る。名前だけの行も同じ位置で書き直す（BUG-0094 の変数の受け手と同じ）。
- schema v12: `_type_aliases`（索引 3 つ）を足し、v11 からの移行で TS / JS のファイルを取り込み直させる。
- 仕様書 T-06 処理規約 9 (a)・T-07 処理規約 11、設計書 内部表の一覧（v12）・§7.6.2 に理由つきで書いた。理由: 型の別名は TS の型の
  普通の書き方で、注釈を書いた受け手の読みが行にも名前の一致にも残らず、注釈の無い受け手より見えなくなっていた。
- 範囲外: TS の型リテラルのメンバーはシンボルにしていない（`symbols.py` の決まり。票 BUG-0114）ので、別名自身のメンバーの読みは
  結ぶ先が無い。受け手の注釈の読み方（`Opts['retry']` を `Opts` と読む。票 BUG-0113）は変えていない。

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
