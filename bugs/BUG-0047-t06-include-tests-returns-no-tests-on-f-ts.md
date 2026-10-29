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
