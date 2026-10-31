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
