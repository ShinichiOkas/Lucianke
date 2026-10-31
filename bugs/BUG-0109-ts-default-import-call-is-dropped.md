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
