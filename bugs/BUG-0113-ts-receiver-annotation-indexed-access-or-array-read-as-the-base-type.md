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
