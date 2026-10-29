# 引き継ぎ — 現在地と次の作業

この文書は、セッションが中断した（トークンクォータ等）ときに、**別の PC・別のエージェントが続きを拾うため**の現在地。
作業が進んだらこの文書を更新してコミットする（履歴は git に残る）。

最終更新: 2026-10-29 / 対象: `main`（この文書を含むコミット） / 直前のコミット: `7aa42e2` 20e: LSP の exact 経路の残り

---

## 0. まず流すもの（3 分）

```powershell
git pull
uv run pytest -q                     # 期待: 2 failed, 3781 passed, 22 skipped
uv run pytest tests/contract_fixtures/test_fx_db_T19_T20.py -q   # 落ちる 2 件の正体
```

- 落ちるのは **BUG-0061（`test_t19_postcondition_1_2_3_4_5_schema[F-PY]`・`[F-TS]`）だけ**。これは直前のコミットの時点で既に落ちる（下記 §5 で確認済み）。**新しく赤が出たら、それは引き継いだ変更のせい**なので先に直す。
- `uv run ruff check src tests` は clean。`ruff format --check` は 56 ファイルが未整形になるが、**これはインストール済み ruff とリポジトリの版のずれ**。全体整形はしない（差分が膨らむだけ）。触ったファイルだけ手で揃える。

## 1. 状態

| 項目 | 値 |
|---|---|
| ブランチ | `main`（`origin/main` と同期） |
| スプリント | `m1-impl-20261029`（`.pair-agent/sprints/m1-impl-20261029.json`）。M1 実装 第 1 ラウンド |
| 全体テスト | 3781 passed / 22 skipped / 2 failed（BUG-0061 のみ） |
| 索引 DB のスキーマ | v4（`_exact_marks`、`index/exact_marks.py`、ADR-16） |

## 2. このコミットで入ったもの

前セッションが中断した時点の作業ツリーを、**そのまま捨てずに完成させてから**入れた。テーマは 2 つ。

### (a) 関数の中の `require` の束縛（BUG-0052。BUG-0048 の TS/JS 版）

`function f() { const { foo } = require("./foo"); return foo(1); }` の `foo` の使用が、参照（T-06）に載るようにした。
モジュール直下の `require` と同じ扱いで、別関数で同名を別モジュールから require していても巻き込まない。

- `src/lucianke/syntax/imports.py`
  - `JS_FUNCTION_NODES`（関数ノードの集合）を公開。`_JS_FUNCTIONS` の定義元を 1 か所に寄せた
  - 関数の中の `require` を `ImportFact.nested=True` に
  - `js_require_bindings(declarator, src)` を追加（`relations.py` と共有）
- `src/lucianke/syntax/relations.py`
  - `_js_scoped_require` で、関数の中で require が束縛した名前を「ローカル変数」でなく「import」として控える
  - `object_assignment_pattern`（`{ foo = 1 }`）のキーも対象に
- テスト: `tests/unit/test_syntax_relations_scoped_imports.py` に 14 本、
  `tests/contract/test_T06_T07.py::test_real_t06_use_after_a_function_local_require_is_a_reference`（実索引で F 相当を作り T-06 を通す）

### (b) `Node.text` の全廃（BUG-0020・BUG-0051）

py-tree-sitter 0.26.0 の `Node.text` は、`read_callback` で解析した木では**読み取り関数を呼び直し**（解析期限を過ぎると空回りして戻らない）、返した `bytes` を解放してから読む（アクセス違反）。ノードの綴りは必ず `source[node.start_byte : node.end_byte]` で切る。

- `syntax/boundaries.py`・`syntax/imports.py`・`syntax/relations.py`・`tools/_t11_scope.py` から `node.text` を除去
- `tests/unit/test_syntax_skeleton_realfiles.py` に**番人テスト**を追加: `src/` を AST で走査し、`.text` の読み取りが残っていないことを検査する（ノードでない `.text` は `_NOT_A_NODE` に名前で登録して除外）
- 前セッションが残した壊れたテスト（文字列リテラルの `\n` が実改行に化けて collection error）を修復した

### (c) その他

- `tools/t07_query_call_hierarchy.py`: `exclude_external` の説明文を実装に合わせた（`other stdlib/package callees`）
- `bench/e2e/records/20261029-151430-windows.jsonl`: e2e の記録（前セッションの実行分）
- `bench/dogfood/out/` はスクラッチ（344 KB の探針ログ）なので `.gitignore` に足して追跡しない
- `BUG-0061` の票を追加（下記）

## 3. 次にやること（優先順）

1. **BUG-0061**（`bugs/BUG-0061-t19-returns-only-two-tables.md`）— T-19 が公開ビューのうち 2 つしか返さない。契約テスト 2 件が赤。**M1 の受入が閉じないので最優先**。票には見たことだけ書いてある（原因は未調査）
2. **BUG-0058**（レイテンシ）— F-PY・F-TS で T-06・T-07・T-08 のサーバー内 p95 が Fast Path の 50 ms を大きく超える。要約は `.pair-agent/acceptance/M1-3-latency-20261029-154219.md`
3. **BUG-0059** — `bench/run_latency.py` の台本に T-00・T-01・T-10・T-11・T-13 が無く、14 ツールで判定できない
4. **BUG-0060** — 計測対象の選び方: F-TS で `E_TARGET_AMBIGUOUS` が 20〜33/200、F-PY の T-07 で `E_UNSUPPORTED` 13/200
5. **BUG-0057** — TypeScript の LSP が無い環境では F-TS の exact 水準の事後条件を確かめられない（検証の仕組みの穴）
6. BUG-0041〜0056 の未処理分。処理済みかは `git log --oneline --grep=BUG-00xx` で確認する（票に状態欄は無い）

## 4. 触るときの注意

- **`Node.text` を書くと番人テストが落ちる**。`source[node.start_byte : node.end_byte]` で切る。ノードでない `.text`（自前の型・`re.Match` など）は `test_syntax_skeleton_realfiles.py` の `_NOT_A_NODE` に**受け手がノードでないことを確かめてから**足す
- 関数ノードの集合は `imports.JS_FUNCTION_NODES` が唯一の定義。`relations.py` 側に写しを作らない（ずれると使用が落ちるか解決できなくなる）
- `bench/.cache/` に F-PY（httpx）・F-TS（ky）を `uv run python bench/fetch_fixtures.py` で置いてから、`tests/contract_fixtures/` が本物の索引で走る。無いと skip になる（**skip は pass ではない**）
- `.pair-agent/current/` のポインタの鍵はホスト名 + リポジトリのパスで決まる。**別 PC では別のポインタになる**ので、現在地はこの文書と `.pair-agent/sprints/m1-impl-20261029.json` を読む
- 区切りごとにコミット・push（常に sync）。作業の前に `git pull`

## 5. この文書を書いた時点で確かめたこと

- `git stash` で `src/` とテストの未コミット変更を退避して HEAD（`7aa42e2`）のまま `tests/contract_fixtures/test_fx_db_T19_T20.py` を走らせ、**同じ 2 件が落ちる**ことを確認 → BUG-0061 は引き継いだ変更が原因ではない
- 全体テスト 1 回（544 秒）と、影響範囲のテスト 4 ファイル（89 件 green）
- `ruff check src tests` clean
