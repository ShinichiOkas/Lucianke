# BUG-0048 T-06 が関数内 import で束縛した名前の使用（pytest.raises(LuciankeError)）を参照として返さない

- 報告: 2026-10-29 / 見た人: dogfood-2（実 stdio クライアント、使い捨て作業ツリー 42640ac〔HEAD〕、このリポジトリ自身を `set_roots`）
- 症状: `find_references_and_implementations {target: {path: "src/lucianke/core/errors.py", symbol_name: "LuciankeError"}, query_type: "references", include_declaration: true}` の参照に、`tests/unit/test_engine_indexer.py:239`（`    with pytest.raises(LuciankeError):`）が含まれない。同じファイルの 237 行（関数内の `    from lucianke.core.errors import LuciankeError`）は含まれる。
  - 実測: 返り 473 件（`lsp=unavailable`・`confidence=structural`）。`ast` で `src/`・`tests/` の `Name` の識別子 `LuciankeError` を集めた 473 件と比べ、`ast` にあって返りに無いのは `(tests/unit/test_engine_indexer.py, 239)` の 1 件のみ（返りにあって `ast` に無いのは宣言行 1 件）。239 行の `LuciankeError` は 237 行の関数内 import で束縛された名前の使用。
  - 同じ調査で `resolve_target`（5 件 vs `ast` 4 件＋宣言）、`confine`（72 件 vs 71 件＋宣言）は漏れ 0。
- 期待: 関数内 import で束縛した名前の使用も参照として返る（import 行が返るのだから、使用箇所も同じ対象に解決される）。
- 再現手順: `bench/dogfood/run_dogfood2.py --repo <作業ツリー> self`（`bench/dogfood/out/obs2_findings_self.json`）。
- 調査はしていない（直す役の仕事）。

## 原因
- `syntax/relations.py` の Python の走査（`_scan_python`）が、`def` の中の `import` / `from ... import` が束縛した名前を「その関数のローカル変数」の表（`scan.locals`）に入れていた。素の識別子の解決（`_resolve_bare`）は、ローカルに束縛された名前を参照に数えず捨てる（パラメータ・代入と同じ扱い）。import の行そのものは別の道（`_import_candidate`）で出るので、`IMPORTS` の行だけが残り、同じ関数の中の使用（`pytest.raises(LuciankeError)`）が落ちた。票の 237 行（import）が返り、239 行（使用）が返らない現象と一致する。`tests/unit/test_engine_indexer.py` 以外でも、関数の中で import した名前の使用はすべて同じ理由で漏れる。
- もう 1 つの欠陥（同じ根）: 索引器の名前表（`imported_name_map`）はファイル全体で 1 枚（文書順で後勝ち）で、関数の中の import もモジュール直下の同名と混ざる。ローカル扱いで使用を捨てていたために表に出なかったが、使用を数えるようにすれば、別のスコープの同名の import を取り違える。

## 直し方（コミット 3b9a017）
- スコープを区別して解決する。`ImportFact` に `nested`（Python: `def` / `class` の中の import。`if TYPE_CHECKING:`・`try:` のモジュール直下は含めない）を足した（`syntax/imports.py`）。索引器（`engine/indexer.py`）は、モジュール直下の import だけでファイルの名前表を作り、`nested` の import は文の開始位置ごとの名前表を持つ。`extract_relations` の新しい引数 `resolve_import(import 文の開始位置, 名前) -> URN` で引く。
- `_Extractor` は、`def` / `class` の中の import が束縛した名前を、ローカル変数とは別の表（`scan.imports`: スコープのノード → 名前 → 文の位置）に控える。`_binding(name, node)` が使用箇所から内側のスコープへ向かって最初の束縛を探す（関数は外側まで、クラス直下の束縛は使用箇所がそのクラス直下のときだけ。`global` の名前はモジュールの名前）。import なら `resolve_import` でその文の先へ、ローカル変数なら従来どおり参照に数えない。`self.x` の型・`Foo.create()` のレシーバ・`import a.b` の連鎖・IMPORTS の行も同じ道を通る。
- `resolve_import` を渡さない呼び手（`tools/_lsp_exact.py` の位置の分類）でも、使用は候補として残る（解決先は `resolve_name` で引く）。
- テスト: `tests/unit/test_syntax_relations_scoped_imports.py`（関数内 import の使用・別名・モジュール import・外部の連鎖・内側の関数・クラス直下とメソッド・モジュール直下の `if TYPE_CHECKING:`・関数内の `if TYPE_CHECKING:`・同名の別スコープを巻き込まない・内側のパラメータが外側の import を隠す・レシーバ・型の漏れ・`resolve_import` なし）、`tests/contract/test_T06_T07.py::test_real_t06_use_after_a_function_local_import_is_a_reference`（実の索引器と T-06 で、関数内 import の使用が載り、同名を別モジュールから別の関数で import した使用は載らない）。

## 確認
- 修正前の再現: 関数内 `from ... import LuciankeError` ＋ `with pytest.raises(LuciankeError):` の抽出が、`IMPORTS` の行と `pytest` / `raises` だけで、`LuciankeError` の使用を返さないことを確かめた。直した後は `REFERENCES`（`pytest.raises(...)`）と `CALLS`（`raise AppError()`）が import と同じ URN で返る。
- 再現手順（`bench/dogfood/run_dogfood2.py --repo <作業ツリー> self`）: 共有ツリーでは他の役が `src/lucianke/tools/_runtime.py`・`t11_rename_symbol.py` を書き換えている最中で、索引と `ast` の行がずれる（`_runtime.py:330` と `334` など）ため、`ast` との比較が揺れた。その代わりに、現在の作業ツリーの `src/`・`tests/` をコピーして固定し、実の索引器 ＋ T-06（`include_declaration: true`）に当て、`ast` で集めた `Name` と比べた: `LuciankeError` は返り 520 件・`ast` 440 件・`ast` にあって返りに無い 0 件（票の `test_engine_indexer.py:239` を含む）、`confine` は返り 81 件・`ast` 61 件・漏れ 0 件。
- `tests/unit/test_syntax_relations*.py`・`test_engine_indexer*.py`・`tests/contract/test_T06_T07.py` を 3 回続けて green（142 件）。`tests/unit` 全体も green。`ruff check` / `ruff format --check` は変更したファイルで通る。
- 残り: JavaScript / TypeScript の関数の中の `const { foo } = require("./foo")` が束縛した名前の使用は、同じ理由で落ちる（BUG-0052）。
