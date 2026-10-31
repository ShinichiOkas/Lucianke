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
