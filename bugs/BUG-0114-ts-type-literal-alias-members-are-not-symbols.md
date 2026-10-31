# BUG-0114 TS の型リテラルの別名のメンバー（`type KyOptions = { retry?: … }` の `retry`）がシンボルにならず、T-06 の対象にできない・読みを結べない

- 報告: 2026-10-31 / 見た人: BUG-0108・BUG-0109 を直す役（F-TS〈ky〉の票 BUG-0108 の箇所を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（F-TS〈ky `3541888878`〉を `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0108・BUG-0109 の直しを載せたコード）

- `source/types/options.ts` の `export type KyOptions = { … retry?: RetryOptions | number; … }`（45 行目から）: `symbols` に
  `options.ts#KyOptions`（Type）はあるが、`KyOptions.retry` などのメンバーの行は無い。同じファイルの `export interface Options extends
  KyOptions, RequestOptions { method?: …; headers?: …; … }` のメンバーは `Options.method`（Property）などとしてある。
- `test/resilience.ts:111` の `options.retry`（`options: Options`）は、`Options` の基底の別名 `KyOptions` の型リテラルのメンバーに当たり、
  結ぶ先のシンボルが無いので `relations` にも `_name_calls` にも行が無い（BUG-0108 の直しの決め: 別名自身のメンバーは、シンボルが
  あれば結び、無ければ結ばない）。
- ky の型のほとんどは `export type X = { … }` の形（`KyOptions`・`Hooks`・`RetryOptions`・`BeforeRetryState` など。
  `source/types/` の `export type … = {` は 20 以上）。

## 利用者（呼ぶ側の AI）として困ること

- `KyOptions.retry` の意味を変える前に、T-04 / T-06 で `KyOptions.retry` を対象にできない（シンボルが無い）。同じ形の `interface` の
  メンバーなら参照が出るのに、`type` で書いたメンバーは読みの箇所が見えず、警告も無い。

## 原因・直し方・確認（2026-10-31）

BUG-0113 と一緒に直した（どちらも TS の型のメンバーの `structural` の解決。BUG-0113 の受け手の型が型リテラルのメンバーを指すため）。

### 原因

- `syntax/symbols._Ctx._classify_js` が `property_signature` / `method_signature` を、親が `interface_body`（持ち主がインターフェース）の
  ときだけシンボルにしていた（「型リテラルのメンバー」は意図して捨てていた）。`type KyOptions = { retry?: … }` の `retry` は
  `object_type` の子なので捨てられ、`_type_aliases` の `member`（`KyOptions.retry`）はどのシンボルも指さなかった。BUG-0108 の整えは
  シンボルの無い別名自身のメンバーを `inline`（結ばない）にしていたので、`options.retry` は行にも名前だけの行にもならなかった。

### 直し方（決めたこと）

- 型リテラルのメンバーをシンボルにする: 型の別名の値・メンバーの型注釈（インターフェース・型リテラルのプロパティ、クラスのフィールド、
  getter の戻り値）から、括弧・合併・交差・メンバーを持ち越す標準の型（`Partial` / `Required` / `Readonly` / `NonNullable` / `Pick` /
  `Omit`）の第 1 型引数だけを通って着く型リテラルのメンバー（`_Ctx._type_member_position`）。プロパティは `Property`、メソッドの
  シグネチャは `Method`、修飾子は `pub`。修飾名は持ち主の下: `KyOptions.retry`、入れ子は `KyOptions.inline.before`、インターフェースの
  入れ子 `Opts.nested.inner.leaf`、クラスのフィールド `Server.opts.z`（URN の規則は今までどおり。`parent_urn` も修飾名から決まる）。
  関数型の引数・配列の要素・ほかの総称の型引数・関数の引数と戻り値の型リテラルは持ち主のメンバーではないので拾わない。
- 合併の別々の型リテラルの同名のメンバー（`type Shape = { kind: 'a'; a } | { kind: 'b'; b }`）は今までの衝突の規則で最後が素の URN
  （`Shape.kind`）、前のものは `~sighash`。再定義ではないので警告は出さない。`s.kind` は素の URN に結び、整えの合併の規則
  （持つ構成がちょうど 1 つ）では同じシンボルを 1 つに数える（直接結ぶのと揃える）。
- 別名の受け手の読み（`o.retry`・`o.extra`）は `lookup_alias` の `member` がシンボルを見つけて `structural` で結ぶ。交差では別名自身の
  メンバーをほかの構成より先に見る（抽出器が `this.#options.headers` を `InternalOptions.headers` に直接結ぶのと揃える。クラス自身の
  メンバーが基底より先なのと同じ）。
- 素の名前の解決（`syntax/relations._resolve_bare`）は、TS の型の別名と、メンバーの中の型リテラルのメンバーを素の名前の範囲にしない
  （ky の `readonly stop: typeof stop` の `stop` が `KyInstance.stop` 自身に結ばれたのを直した。import した `constants.ts#stop` のまま）。
- T-02: 骨格の親子は行の範囲の包含で決め、同じ行の範囲は兄弟のまま、修飾名が親の修飾名 + `.` で始まるものだけ子にする
  （`syntax/skeleton._encloses`）。1 行の別名 `type U = { a: string }` のメンバーを最上位に並べると BUG-0078 の縮退で落とせないため。
  縮退は今までどおり `doc:` → `urn:` → 非公開 → 入れ子の公開メンバーを 1 件ずつ（別名のメンバーは入れ子なので落とせる）。
- T-03 / T-04 / T-06 はシンボルの表をそのまま使うので、型リテラルのメンバーを名前で探せ、`path` + `symbol_name`（`KyOptions.inline.before`）・
  URN で解決でき、参照の対象にできる。名前の一致（処理規約 7）の候補にもインターフェースのメンバーと同じく入る。
- 範囲外: `const x = { … } as const` のオブジェクトリテラルのメンバーはシンボルにしない（型の分かる受け手から結ぶには変数の値の形を
  型として扱う仕組みが別に要り、今は名前の一致の候補が増えるだけになる）。
- schema v13（BUG-0113 と共通。表は変えず、v12 からの移行で TS / JS のファイルを取り込み直させる）。仕様書 §7.2・§9.3・T-02 処理規約 2・
  T-04 処理規約 6・T-06 処理規約 9 (a)・T-07 処理規約 11、設計書 内部表の一覧・§7.4.3・§7.4.4・§7.6.2 に理由つきで書いた。理由: TS の
  設定・引数のオブジェクトは型リテラルで書くことが多く、その各プロパティは AI が使用箇所を最もよく探す対象。インターフェースの
  メンバーと同じく T-02〜T-06 の対象にする。

### 確認

確認の数（ky の前後・試験）は BUG-0113 の「確認」にまとめた。この票の箇所:

- 票の `test/resilience.ts:111` の `options.retry`（`options: Options`、`interface Options extends KyOptions, RequestOptions`）: 2 箇所
  （23 列・52 列）とも `source/types/options.ts#KyOptions.retry` への `REFERENCES`（`structural`）になった（前は行も名前だけの行も無い）。
- F-TS（ky）のシンボル: 533 → 683（増えた 150 はすべて型リテラルのメンバーの `Property`。消えた 0）。`source/types/` の `KyOptions`・
  `Hooks`・`RetryOptions`・`InternalOptions` などのメンバー。
- 型リテラルのメンバーへの新しい `structural` の行 127（`this.#options.timeout` → `InternalOptions.timeout`、`options.context` →
  `KyOptions.context`、`ForceRetryError` の `options.code` → `ForceRetryOptions.code`、`server.url` → `ExtendedHttpTestServer.url` 49 など）。
  囲みの定義が型リテラルのメンバーになった型の使用の行 109（`ResponsePromise.json` の中の `TYPE_USES` など。先は同じ）。
- 決定的な試験: `tests/unit/test_syntax_symbols.py::test_ts_type_literal_members_are_symbols`（種別・修飾名・入れ子・交差・`Partial`・
  クラスのフィールド・getter・インターフェースの入れ子・拾わない位置・合併の同名・警告なし）、`tests/unit/test_syntax_skeleton.py::
  test_type_literal_members_are_children_of_the_alias`（複数行・1 行・入れ子の親子、縮退で落とせる）、`tests/unit/
  test_engine_indexer_reexports.py::test_ts_type_literal_members_are_symbols_and_bind`（`symbols`・`parent_urn`・読みの行・連鎖・
  `typeof stop`・`_type_aliases` の行）、契約 `tests/contract/test_T06_T07_reexports.py::test_t02_t03_t04_t06_ts_type_literal_members`
  （T-02 の子・T-03 の名前の検索・T-04 の解決・T-06 の参照）。BUG-0108 の試験 2 件（`o.extra`・`o.tick()`・`m.tag` は行を作らない）は、
  シンボルに結ぶ期待に直した。
