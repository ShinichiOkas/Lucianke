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
