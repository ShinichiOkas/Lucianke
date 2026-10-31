# BUG-0113 TS の受け手の注釈 `r: Opts['retry']`（添字の型）・`a: Opts[]`（配列）を `Opts` と読み、`r.limit` を `Opts.limit` に結ぶ

- 報告: 2026-10-31 / 見た人: BUG-0108・BUG-0109 を直す役（F-TS〈ky〉の修正前後の差を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0108・BUG-0109 の直しを載せたコード）

```
types.ts   export interface Retry {\n  limit: number;\n}\n\nexport interface Opts {\n  retry: Retry;\n  limit: string;\n}\n
use.ts     import type { Opts } from './types';\n\nexport function f(r: Opts['retry'], a: Opts[]): number {\n  return r.limit.length + a.length;\n}\n
```

- `use.ts#f` の `relations` に `REFERENCES` → `types.ts#Opts.limit`（4 行目の `r.limit`）がある。`r` の型は `Opts['retry']`（= `Retry`）で、
  `Retry.limit` ではなく `Opts.limit` に `structural` で結ばれている。
- `a.length`（`a: Opts[]`）は `_member_refs` に受け手のクラス `types.ts#Opts`・名前 `length` の行として残る（`relations` にも
  `_name_calls` にも無い）。
- F-TS（ky）: `source/core/Ky.ts` の `#calculateDelay(retry: InternalOptions['retry'], …)` の `retry.delay(...)`・`retry.jitter`・
  `retry.backoffLimit` が、受け手 `InternalOptions`（型の別名）のメンバーとして探されている（BUG-0108 の直しの後は、構成の中身が
  分からないので `_name_calls` に入る）。

## 利用者（呼ぶ側の AI）として困ること

- T-06 で `Opts.limit` の参照を見ると、実際は `Retry.limit` を読んでいる箇所が `structural`（確かな行）として混ざる。`Retry.limit` の
  参照からはその箇所が抜ける。

## 原因・直し方・確認（2026-10-31）

BUG-0114 と一緒に直した（受け手の型が型リテラルのメンバーを指すため）。

### 原因

- `syntax/relations._Extractor._js_ann_name` が TS の型注釈を深さ優先でたどり、最初の `type_identifier` の綴りを受け手の型にしていた
  （先頭の名前に縮める）。`Opts['retry']`（`lookup_type`）も `Opts[]`（`array_type`）も `Opts`、`Error & {…} | undefined` も `Error`、
  `{ a: Foo }` も `Foo`、`(x: Foo) => Bar` も `Foo`、`Partial<Options>` は `Partial`、`express.Request` は `Request`（同名の標準の
  グローバル）になった。使っていたのはパラメータ・変数・クラスのフィールド・getter の戻り値の注釈（`_scan_js` / `_js_params`）の 4 か所。
  `await p`（`p: Promise<T>`）の値は `p` の型（`Promise`）のまま、`x as Foo` は `x` の型だった（`_js_value_type`）。
- 別ファイルの型・同じファイルのインターフェースの属性の連鎖 `o.retry.limit` は、`o.retry` の型を分からないとしていた（BUG-0079 の
  決まり。メンバーの型注釈を索引のどこにも書いていなかった）。

### 直し方（決めたこと）

- 注釈を `_TsType`（名前・添字の道・標準の配列・`Promise` の中身）に読む（`_ts_type`。先頭の名前に縮めない）:
  - 名前の型 `Foo`・`ns.Foo`（`ns` は名前空間・`import * as ns`。`express.Request` は `lk://_ext/express#Request`）。総称の型 `Foo<T>` は `Foo`。
    型引数（`T`。囲みの別名・インターフェース・クラス・関数・メソッドの `<T>`）は分からない。
  - 添字の型 `Opts['retry']`（鍵が識別子の形の文字列リテラルのとき。`Opts['a']['b']` も）は、メンバー `Opts.retry` の型（受け手の URN は
    `<Opts>.retry`、`r.limit` の先は `<Opts>.retry.limit`）。鍵が文字列でない添字（`Opts[keyof Opts]`・`Opts[number]`）は分からない。
  - 配列 `Opts[]`・タプル `[A, B]` は標準の `Array`、`readonly Opts[]` は `ReadonlyArray`、`Array<Opts>` は `Array`（`xs.map()` は
    `lk://_ext/globals#Array.map`。要素の型 `Opts` のメンバーにしない。`xs[0].x` は今までどおり型の分からない受け手）。
  - `null` / `undefined` / `void` / `never` を除いて構成が 1 つの合併（`Foo | null | undefined`）はその構成。2 つ以上は分からない。
    交差・型リテラル・関数型・条件型・`keyof`・`typeof`・`string` なども分からない（名前の一致）。
  - メンバーを持ち越す標準の型（`Partial<Foo>` など。同じファイル・import に同名があれば素通りしない）は `Foo`、`Promise<T>` /
    `PromiseLike<T>` は `Promise`（`await` した値は `T`）、`Awaited<Promise<T>>` は `T`。`x as Foo` の値は `Foo`（`as const` は今までどおり）。
  - 注釈があるときは注釈が決める（前は注釈に `type_identifier` が無ければ値 `new Foo()` の型を使っていた）。
- クラスのフィールド・getter の注釈が名前の型に読めない（型リテラル・合併・交差）ときは、メンバー自身の型 `<クラス>['name']` にする
  （`this.opts.z` の `opts: { z: number }` は `Server.opts.z`）。
- 属性の連鎖（TS / JS だけ）: `a` の型が別ファイルの型・同じファイルのインターフェースなら、`a.b` の型をメンバーの型 `<型>.b` にする
  （URN は `<型>.b.name`）。同じファイルのクラスは今までどおり属性の型で決め、決まらなければ分からない。Python は変えていない。
- 抽出器は型注釈つきのメンバー（`property_signature`・`public_field_definition`）の型の構成も `_type_aliases` に書く（`alias_urn` がメンバー。
  別名と同じ規則。型リテラルは `member`〈BUG-0114 のシンボル〉、配列は `lk://_ext/globals#Array`、添字の型は `type` の `<B>.k`）。中身の
  分からない構成しか無いメンバー（`limit: number`）は書かない。別名の構成の配列・添字の型も同じく読む（前は `opaque`）。
- 整え（`index/inherited.py`）: 目標がシンボル（型リテラルの入れ子のメンバー）ならそのまま。無ければ `_receiver_class` が「クラスの部分」が
  メンバー（`Property` / `Field` / `Method`）でも `_member_refs` に退避し、`MemberResolver` がメンバーの型の構成を `lookup_alias` で探す
  （メソッド・構成の無いメンバーは名前だけ）。「クラスの部分」がシンボルでない（`Options.hooks` の `hooks` が基底の別名から継承した
  メンバー）ときは、いちばん長い受け手になれるシンボルの途中（`Options`）を `class_urn` にし、`name` を点つきの道（`hooks.before`）に
  する（`_lookup_path` が 1 段ずつ型の分かるメンバーをたどる。決まらなければ名前だけで、`_name_calls` の名前は道の最後）。別名の構成・
  変数の型の先がメンバー・シンボルでない道でも同じくたどる（`_in_type`）。型の分からない受け手を確かな行にしない決まり
  （BUG-0079・BUG-0085）はそのまま: どこかで決まらなければ名前の一致に任せる。
- 編集に追従: 結び直す範囲の再帰 CTE は、整えるファイルの型の分かるメンバーも種に入れ、`_type_aliases` を逆にたどった先のメンバーの
  持ち主（`symbols.parent_urn`）も足す（メンバーの型だけの編集はシンボルの集合が変わらず import している側を取り込み直さないので、
  道の受け手 `o.hooks.before` は持ち主 `KyOptions` → 派生 `Options` から結び直す）。
- schema v13（表は変えない。v12 からの移行で TS / JS のファイルを取り込み直させる）。`tests/unit/test_index_schema.py`・
  `tests/unit/test_index_core.py`（版 13）も。仕様書 T-06 処理規約 9 (a)・T-07 処理規約 11、設計書 内部表の一覧・§7.6.2 に理由つきで
  書いた。理由: 誤った確かな行は取りこぼしより悪い（`Opts['retry']` の `r.limit` が `Opts.limit` に、`errors: Error[]` の `errors.push()` が
  `Error.push` に `structural` で結ばれていた）。

### 確認

修正前（`a96a9d1` の `src`）と修正後のコードで、それぞれ新しい索引に in-process で索引（LSP なしの `structural`）して比べた。

F-TS（ky `3541888878`）:

```
symbols:   533 → 683（+150。すべて型リテラルのメンバーの Property。BUG-0114）
relations: 9,561 → 10,006（新しい箇所 454・消えた箇所 9・先が変わった箇所 29。すべて structural）
  新しい 454: 型リテラルのメンバー 127（this.#options.timeout → InternalOptions.timeout、server.url → ExtendedHttpTestServer.url 49、
    ForceRetryError の options.code → ForceRetryOptions.code、#calculateDelay の retry.delay / jitter / backoffLimit → RetryOptions.*、
    options.retry → KyOptions.retry など）、外部の型のメンバー 327（配列の受け手の Array.push / length / map など 297、Record 19、
    http.Server 6、express.Express 5）
  先が変わった 29: 配列の受け手 27（errors: Error[] の errors.push() が Error.push → Array.push、chunks: Uint8Array[] の Uint8Array.push →
    Array.push、Buffer.push → Array.push など）、this.#options.headers が Options.headers → InternalOptions.headers 2（別名自身のメンバー）
  消えた 9: Partial<Options> の受け手の lk://_ext/globals#Partial.* 6・Awaited<…> の Awaited.* 1・Error & {value?} | undefined の Error.* 2
    （どれも注釈の先頭の名前に縮めた誤った外部のメンバー。今は名前の一致）
  囲みの定義が変わった（先は同じ）109: 型リテラルのメンバーの中の型の使用（ResponsePromise.json の中の TYPE_USES など）
_name_calls: 10,949 → 10,682（消えた 276: push 174・length 39 など型が分かった受け手、増えた 9: 上の Partial / Awaited / 交差の受け手）
_type_aliases: 207 → 276（メンバーの型の構成 69）
_member_refs: 893 → 772（型リテラルのメンバーがシンボルになり、直接結ぶ行が増えた）
走査の時間: 5 回ずつ交互に測って中央値 10.7 秒 → 9.8 秒（ほかの役と同じ PC で負荷が揺れ、差は雑音の範囲）
```

- 票の箇所: `source/core/Ky.ts` の `#calculateDelay(retry: InternalOptions['retry'], …)` の `retry.delay(...)`・`retry.jitter`・`retry.backoffLimit` は
  `InternalOptions.retry`（型 `NormalizedRetryOptions` = 写像型 `& Pick<MutableRetryOptions, 'shouldRetry'>`）の構成から
  `source/types/retry.ts#RetryOptions.delay` / `jitter` / `backoffLimit` に結ぶ（前は受け手を `InternalOptions` と読んで名前だけ）。
- 票の小さなワークスペース（`types.ts` / `use.ts`）: `r.limit` → `types.ts#Retry.limit`（`Opts.limit` の行は無い）、`a.length` →
  `lk://_ext/globals#Array.length`（`_member_refs` に `Opts.length` は残らない）、`r.limit.length` は名前だけ（`limit: number`）。
- F-PY（httpx `b5addb64f0`）: シンボル 1,451・relations 9,232・_name_calls 2,598 で、書き出した表がバイト列まで同じ（Python の
  受け手の読み方は変えていない）。

テスト（決定的。フィクスチャ `tests/fixtures/reexports/web/members/` を足した。バイト列のまま `-text`。README に各箇所の意図）:

- `tests/unit/test_syntax_relations.py::test_ts_receiver_annotations_are_read_precisely`（抽出器の段: 添字の型・配列・タプル・
  `readonly`・`Array<T>`・`null` の合併・2 つの合併・交差・型リテラル・`Promise` と `await`・`Partial`・`express.Request`・`Opts['a']['b']`・
  鍵が文字列でない添字・型引数・`as`）。
- `tests/unit/test_engine_indexer_reexports.py`（足した 4 件）: `test_ts_indexed_access_and_array_receivers`（`r.limit` は `Retry.limit`・
  `Opts.*` に結ばない・`Array` / `ReadonlyArray`・入れ子・`await`・型の分からないメンバー・`Opts[keyof Opts]`・`Opts['missing']`・
  `T['retry']` は名前だけ）、`test_ts_type_literal_members_are_symbols_and_bind`（BUG-0114）、`test_ts_member_type_edits_rebind_receivers`
  （別ファイルの型の `limit` を消す / 戻す、継承したメンバーの道 `o.hooks.before`、シンボルの集合が変わらないメンバーの型だけの編集で
  道の受け手が結び直る〈持ち主をたどる CTE を外すと赤になることを確かめた〉、メンバーの型の編集）、`test_migration_12_to_13_reingests_ts_js_files`。
- 契約 `tests/contract/test_T06_T07_reexports.py::test_t02_t03_t04_t06_ts_type_literal_members`（`Retry.limit` の参照に `r.limit`・`got.limit`、
  `Opts.limit` の参照に `r.limit` が出ない）。
- `uv run ruff check`（引数なし）: クリーン。
- 構文・シンボル・骨格・import・関係・索引器・再公開・名前の呼び出し・スキーマ・index_core・T-02・T-03・T-04・T-06・T-07 の単体と契約
  （51 ファイル）: リベース前 `1034 passed, 7 skipped`、`origin/main` `0725f00`（BUG-0110 を含む）の上で `1036 passed, 7 skipped`
  （skip は TS の LSP が無い PC での exact の試験）。
- `tests/contract_fixtures`: リベースの前も後も `45 passed, 6 skipped`（skip は TS の LSP が無い PC での F-TS の exact の事後条件と、F-TS には当てはまらない
  試験。F-TS の structural の事後条件は満たす）。
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の不具合（見たことだけの票）: **BUG-0117**（Python の受け手の注釈 `x: A | B`・`Union[A, B]` を `A` と読み、`x.run()` を
  `A.run` に `structural` で結ぶ）。
