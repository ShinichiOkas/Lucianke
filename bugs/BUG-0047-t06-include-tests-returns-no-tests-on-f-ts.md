# BUG-0047 find_references_and_implementations が include_tests=true でも F-TS のテストの参照を 0 件で返す（grep では test/ 配下に複数ある）

- 報告: 2026-10-29 15:22 / 見た人: トークン削減ベンチ 18c（bench/run_tokens.py S2）
- 見たこと（実測）:
  - 対象: F-TS（ky @ 3541888）の `source/utils/type-guards.ts` の `isHTTPError`（関数）。引数 `{ target: { path: "source/utils/type-guards.ts", symbol_name: "isHTTPError" }, query_type: "references", include_tests: true, max_tokens: 6000 }`。
  - 応答のヘッダー `conf=exact lsp=ready`（typescript-language-server が ready のとき）、本文 `references: 2 files, 3 sites (tests: 0)`。出た場所は `source/core/Ky.ts:42`（import）、`source/core/Ky.ts:733`（call）、`source/index.ts:105`（re-export）だけ。
  - 同じシンボルの名前を grep（`\bisHTTPError\b`、scope = source・test・test-d）すると、ヒットするファイルは `source/core/Ky.ts`・`source/index.ts`・`source/types/hooks.ts`・`source/utils/type-guards.ts`・`test-d/error-names.ts`・`test/context.ts`・`test/errors.ts`・`test/hooks.ts`・`test/http-error.ts`・`test/type-guards.ts`（10 ファイル）。テストの 5 ファイルは L の応答に 1 つも出ない。
  - F-PY（httpx）の S2（`URL.copy_with`、同じ引数）は `references: 6 files, 29 sites (tests: 20)` で、テストのファイル 3 つを返す。
  - LSP が `unavailable`（typescript-language-server が PATH に無い）のときは構文木のみで `source/core/Ky.ts` だけ（1 ファイル）。
- 影響の見込み: ベンチの S2 の L が「テストを列挙する」という依頼の答えを欠いたまま小さく出る（99.8% 削減に数えられるが、答えは不完全）。
- 再現手順: `uv run --with tiktoken python bench/run_tokens.py -f F-TS -s S2`（typescript-language-server を PATH に入れる）。結果 JSON の `scenarios[].L.steps[0].response` と `B.steps[]` の `read` を比べる。
- 調査はしていない（直す役の仕事）。

## 原因
- tsserver（typescript-language-server の中身）は、`tsconfig.json` の `include` に入っていないファイルを、**開かれる（`didOpen`）まで** どのプロジェクトにも入れず、`references` の結果に出さない。ky の `tsconfig.json` は `include: ["source"]`、`tsconfig.test.json` は `include: ["test-d"]` で、`test/` はどの tsconfig にも入っていない。T-06 は対象のファイル（`source/utils/type-guards.ts`）だけを開いて聞いていたので、`source/`（3 箇所）しか返らなかった。
- 実測（`bench/.cache/ky`、typescript-language-server 直叩き）: 何も開かずに `isHTTPError` の references = 3 件（`source/core/Ky.ts`・`source/index.ts`）。`test/errors.ts` を 1 つ開くと 7 件、名前を含むテスト 5 ファイルを開くと 30 件（`test/context.ts`・`errors.ts`・`hooks.ts`・`http-error.ts`・`type-guards.ts` が出る）。同じ `exact` のまま、LSP が見たものを返すだけで足りる。
- `test-d/error-names.ts` は `import ... from 'ky'`（パッケージ名の自己参照。ビルド成果物の `distribution/` が無いと解決できない）なので、開いても出ない。これは LSP が解決できないだけで、今回の原因とは別。
- 設計書 §（診断の再取得の節）の「`references` のために参照元を開く必要は無い（未オープンの文書も対象になる）」は basedpyright の実測で、TypeScript には当てはまらなかった。
- 精度の扱い: (a) LSP にテストのファイルも読ませる、(b) LSP の答えに無いファイルを索引の構造的な参照で補い `mixed` にする、の二択。**(a) を採った**。仕様書 T-06 の事後条件「`exact` のとき `references[]` は LSP の参照検索結果と等しい」を保てる。(b) は索引側が `test/` の import（`../source/index.js` の再エクスポート越し）を解決できず（構文木だけだと `source/core/Ky.ts` の 1 ファイルだけ）、補う材料が無い。

## 直し方
- `lsp/features.py`: `open_documents(handle, [(RelPath, languageId), ...])` を足した（未オープンのものだけ `didOpen`。読めないファイルは飛ばす。`ready` でなければ `E_LSP_UNAVAILABLE`）。
- `tools/_lsp_exact.py`: `prime_candidates`（索引の Tier 1〜2 の TypeScript / TSX / JavaScript のファイルのうち、対象の名前を単語境界〔`$` を識別子の文字に数える〕で含み、LSP が未オープンのもの。対象自身は除く。テスト優先・パス順、1 回に `PRIME_MAX` = 100 件まで〔`didOpen` の LRU 200 の半分〕。超えた分は `PrimeSet.skipped` に数える）、`prime_documents`、`needs_priming`（`typescript` / `tsx` / `javascript` だけ。Python の basedpyright は未オープンの文書も探すので要らない）。`.tsx` は `typescriptreact` の languageId で、同じ tsserver に開く。
- `tools/t06_find_references_and_implementations.py`: `_exact_references` が `references` を聞く前に `prime_candidates` → `prime_documents`。上限を超えたら応答の `warnings[]` に「N more file(s) mention '<名前>' but were not opened for the LSP (limit 100 per call); references there may be missing (narrow with scope_paths)」。結果は LSP が見たものそのままなので `exact` のまま、`relations` への書き戻しと 2 回目以降の PaaD 回答にもテストの行が入る。
- 仕様書 T-06 処理規約 2 と設計書（参照元を開く節）に TypeScript の例外を追記。
- テスト: `tests/contract/test_T06_ts_outside_tsconfig.py`（typescript-language-server と `typescript` パッケージがあるときだけ走る〔`LUCIANKE_LSP_TYPESCRIPT_LANGUAGE_SERVER_CMD` / PATH / `bench/.cache/ky/node_modules`〕。tsconfig の外の `test/`・別 tsconfig の `test-d/` から参照が返る、`include_tests=false` ではテストだけ落ちる、LSP を止めた 2 回目も PaaD の `exact` で同じ、ky の `isHTTPError` が `test/errors.ts` ほかを返す）、`tests/unit/test_lsp_prime.py`（偽の入れ物で、候補の選び方・上限と警告・`.tsx` の languageId・二度開かない・`ready` でなければ `E_LSP_UNAVAILABLE`）。

## 確認
- 直す前: 上の契約テストが `PRIME_LANGUAGES` を空にすると落ちる（`['source/index.ts', 'source/use.ts']` だけ）。
- 直した後: `tests/contract/test_T06_ts_outside_tsconfig.py` 4 件 green（ky の `isHTTPError` は `test/context.ts`・`errors.ts`・`hooks.ts`・`http-error.ts`・`type-guards.ts` を含む `exact`）。`tests/unit/test_lsp_prime.py`・`test_lsp_features.py` 40 件、`tests/contract/test_T06_T07.py`・`test_T06_T07_lsp.py` 35 件 green。
- 未対応（同じ原因の別の入口・未観測）: T-07 `query_call_hierarchy` の `incoming` も tsserver の同じ制約を受ける見込み（テストからの呼び出し元が欠ける）。実測はしていない。直すなら `_lsp_exact.prime_candidates` / `prime_documents` を T-07 の `exact` 経路からも呼ぶだけでよい。
