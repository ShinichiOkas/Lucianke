# BUG-0082 テストの兄弟モジュールの import（`from test_T06_T07_lsp import Env`・`from _t11_harness import Rig`）とフィクスチャの中の import（`from app.engine import Packer`）が、ワークスペースのファイルに結ばれず `lk://_ext/...` になる

- 報告: 2026-10-31 / 見た人: BUG-0077 を直す役（名前だけの一致の候補を測っている途中で見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`fa82dc7` のスナップショットを in-process で索引。LSP なしの `structural`。ワークスペース ID `Lucianke`）

`relations`（`source_file_id` のファイルで、先の URN に名前を含む行）:

```
tests/contract/test_T06_T07_exact_marks.py:18   IMPORTS    lk://_ext/test_T06_T07_lsp#Env
tests/contract/test_T06_T07_exact_marks.py:229  TYPE_USES  lk://_ext/test_T06_T07_lsp#Env
tests/contract/test_T06_T07_exact_marks.py:254  CALLS      lk://_ext/test_T06_T07_lsp#build_env
tests/contract/test_T11_rename.py:24            IMPORTS    lk://_ext/_t11_harness#Rig
tests/contract/test_T11_rename.py:134           CALLS      lk://_ext/_t11_harness#Rig.pos
tests/fixtures/untyped_calls/app/service.py:5   IMPORTS    lk://_ext/app#engine.Packer
tests/fixtures/untyped_calls/app/service.py:44  CALLS      lk://_ext/app#engine.Packer.pack
```

- `Env` は `tests/contract/test_T06_T07_lsp.py`、`Rig` は `tests/contract/_t11_harness.py`、`Packer` は
  `tests/fixtures/untyped_calls/app/engine.py` に定義がある（どれもワークスペースの中）。
- `tests/contract/test_T06_T07_exact_marks.py` から `lk://Lucianke/tests/contract/test_T06_T07_lsp.py#Env` への行は 0。
- `find_references_and_implementations {"target": {"path": "tests/contract/test_T06_T07_lsp.py", "symbol_name": "Env"}, "query_type": "references", "format": "json"}`
  の参照のファイルは `tests/contract/test_T06_T07_lsp.py` だけ（`test_T06_T07_exact_marks.py` の行は 0 件）。
- `lk://_ext/test_%`・`lk://_ext/_t11_harness%`・`lk://_ext/app#%` の外部シンボルは 76 個。
- `tests/fixtures/untyped_calls/` だけをワークスペースにした索引（`tests/contract/test_T06_T07_name_calls.py`）では、同じ
  `Packer().pack(["x"])`（44 行）は `app/engine.py#Packer.pack` への `structural` の参照として出る。

## 再現

1. Lucianke のリポジトリを索引する（stdio でも in-process でもよい）。
2. `find_references_and_implementations {"target": {"path": "tests/contract/test_T06_T07_lsp.py", "symbol_name": "Env"}, "query_type": "references"}`
   に、`tests/contract/test_T06_T07_exact_marks.py:18` の import と 229 行・252 行の型の使用が出ない。
3. `query_project_db` で上の `relations` の行を見る。

## 利用者（呼ぶ側の AI）として困ること

- テストの土台（`Env`・`Rig`）やフィクスチャのクラスを変えるときに、それを import して使うテストファイルが参照にも呼び出し
  階層にも出ない（影響範囲を見落とす）。

## 原因・直し方・確認（2026-10-31）

### 原因

- `syntax/imports.py` の `_resolve_python` は、Python の絶対 import を **ルート（`""`）と `src` だけ**で探していた
  （`DEFAULT_SOURCE_ROOTS`。索引器の `make_resolver` は根を渡していなかった）。見つからない import は `external_name_map` が
  `lk://_ext/<最上位の名前>#...` にする。
- そのため、実行時は通る次の 2 種類が外部になった:
  - pytest の既定の import（prepend）が `sys.path` の先頭に足すテストのディレクトリからの import（`tests/contract/` で
    `from test_T06_T07_lsp import Env`・`from _t11_harness import Rig`）。
  - テストフィクスチャ（入れ子のワークスペース）の中で、フィクスチャ自身を根にした絶対 import
    （`tests/fixtures/untyped_calls/app/service.py` の `from app.engine import Packer`）。フィクスチャだけを索引したときは
    ルートが根なので結べていた。

### 直し方（`syntax/imports.py`・`workspace/pyenv.py`・`engine/indexer.py`・`engine/reresolve.py`・`tools/t04_resolve_symbols.py`）

- **根を決める関数 `python_import_roots(from_rel, exists, pytest_imports)`** を足し、`make_resolver` は `source_roots` を渡されない
  ときこれを使う（索引器・T-04 の両方。最初の絶対 import で 1 回だけ決める）。根は、そのファイルについて実際に `sys.path` に
  載るものの順:
  1. ファイルがテストのパス（`is_test`。`conftest.py` を含む）なら、pytest（prepend / append）が足すディレクトリ:
     テストモジュールの basedir（`__init__.py` を持たない最初の祖先。pytest の `resolve_package_path` の親。名前が識別子でない
     ディレクトリはパッケージとして辿らない）、続いて祖先の `conftest.py` の basedir（近いものから）。`--import-mode=importlib` なら足さない。
  2. pytest の設定の `pythonpath`（`detect_pytest_imports`。`pytest.toml`・`.pytest.toml`・`pytest.ini`・`.pytest.ini`・
     `pyproject.toml` の `[tool.pytest]` / `[tool.pytest.ini_options]`・`tox.ini` の `[pytest]`・`setup.cfg` の `[tool:pytest]` を
     pytest 9 と同じ順で 1 つ。項目は設定ファイルの置き場からの相対。ワークスペースの外・絶対パスは捨てる。`addopts` の
     `--import-mode` も読む）。
  3. ルートと `src`（今までどおり）。
  - 「プロジェクト」は、ファイルを含むテストフィクスチャのルート（BUG-0077 の `workspace.languages.fixture_roots`。内側から）と、
    最後にワークスペースのルート。いちばん内側のプロジェクトで 1〜3 を探し（フィクスチャの中ではフィクスチャ自身の設定・`src`）、
    外側のプロジェクトの 2〜3 を後ろに足す（フィクスチャからワークスペースの `src` のパッケージへの import は今までどおり結ぶ）。
  - ファイルが在るときだけ結ぶ（無ければ今までどおり外部）。同じ候補が複数の根にあれば、先に並ぶ（近い）根が勝つ（決定的）。
- **標準ライブラリとの優先（決めたこと）**: 1・2 の根（pytest が足すもの）では、標準ライブラリ（`sys.stdlib_module_names`）と
  `pytest`・`_pytest`・`pluggy` の名前を解かない（`ImportRoot(stdlib=False)`・`PYTEST_PRELOADED`）。`tests/unit/json.py` が
  あっても `tests/unit/test_a.py` の `import json` は `lk://_ext/json`。理由: pytest がテストのディレクトリを足すのはテストを
  集める時点で、そのとき標準ライブラリの多くは既に `sys.modules` にあり、Python は `sys.path` より先にそれを返す
  （実測: pytest 9.2・Python 3.12 で、収集の前に最上位 301 個中 159 個が読み込み済み。`json`・`types` を含み、`sqlite3` は含まない）。
  読み込み前の名前では実行時に手元のファイルが勝つが、どれが読み込み済みかは版で変わるので、決定的に保つため一律に外部に倒した。
  標準ライブラリでない名前（`requests` など）は prepend の根が site-packages より先なので、手元のファイルに結ぶ（Python と同じ）。
  ルート（`""`）とフィクスチャのルートは今までどおり（標準ライブラリと同名のファイルも結ぶ。ルート直下のテストファイルでも同じ）。
- `make_resolver` は `exists` の答えを 1 つの resolve の中で覚えて使い回す（同じパスを 2 度調べない）。
- **設定ファイルの編集で解き直す（既存の `_import_probes` の仕組みを広げた）**: 索引器は、使った pytest の設定の指紋を
  語幹 `?pytest:<プロジェクト>#<指紋>` として `_import_probes` に書く（スキーマは変えない）。設定ファイル
  （`PYTEST_CONFIG_NAMES`）が `index_file` で取り込まれた・作られた・`remove` された・差分の `scan` で取り込み直されたら、
  覚えた設定を捨てて読み直し、**いまと違う指紋で解いたファイルだけ**を解き直す（`stale_config_importers`）。pytest に関係ない
  鍵だけの編集では何もしない（`exact` の行を失う解き直しを最小にする）。指紋は索引に残るので、サーバーが止まっている間の編集も
  次の差分走査が拾う。`__init__.py`・`conftest.py`・兄弟のモジュールを後から作った・消した場合は、調べたパスの語幹で今までどおり解き直す。
- **限界**: コードの中の `sys.path.insert(...)`（`tests/e2e/*` の `compact_grammar`、`tests/contract_fixtures/conftest.py`
  が足す `tests/contract`〔`_t11_harness` の `snapshot`・`sha`〕、`bench/*`）は読まない（外部のまま）。`tests/e2e/*`・`tests/unit/*` の
  `from _server_cmd import server_argv` は、`tests/conftest.py`（BUG-0076 で入った）の basedir `tests` が根になるので結ばれる（pytest と同じ）。pytest の収集で他のテストディレクトリが `sys.path` に載ることに頼る
  import（別のディレクトリのテストモジュール）も結ばない（実行時も収集の順に依る）。`--rootdir`・`PYTEST_ADDOPTS`・`-c` は見ない。
- 仕様書 T-04 処理規約 3 と設計書 §7.4.6 項 1・§7.6.2 に、根の順・標準ライブラリの扱い・解き直しを書いた
  （理由: テストの土台やフィクスチャのクラスを変える AI に、それを import して使うテストが参照・呼び出し階層で 0 件に見えると
  影響を見落とす。pytest と Python が実際に使う根なので推定ではない）。

### 確認

- Lucianke のリポジトリを in-process で索引（`structural`、ワークスペース ID `Lucianke`）した前後（直す前 = `c7f2b7e` の `src`）:

  | | 直す前 | 直した後 |
  |---|---|---|
  | `tests/contract/test_T06_T07_exact_marks.py:18` IMPORTS | `lk://_ext/test_T06_T07_lsp#Env` | `lk://Lucianke/tests/contract/test_T06_T07_lsp.py#Env` |
  | 同 229・252 TYPE_USES / 254 CALLS | `lk://_ext/test_T06_T07_lsp#Env` / `#build_env` | `.../test_T06_T07_lsp.py#Env` / `#build_env` |
  | `tests/contract/test_T11_rename.py:24` IMPORTS・134 CALLS | `lk://_ext/_t11_harness#Rig` / `#Rig.pos` | `.../_t11_harness.py#Rig` / `#Rig.pos` |
  | `tests/fixtures/untyped_calls/app/service.py:5` IMPORTS・44 CALLS | `lk://_ext/app#engine.Packer(.pack)` | `.../untyped_calls/app/engine.py#Packer(.pack)` |
  | `test_T06_T07_lsp.py#Env` を指す行 | 10 行・1 ファイル（自分だけ） | 26 行（`9f4ffa3` の上では 27 行）・5 ファイル（`test_T06_T07_exact_marks.py`・`test_T06_T07_lsp_postconditions.py`・`test_T06_ts_outside_tsconfig.py`・`test_T07_ts_outside_tsconfig.py` を含む） |
  | `_t11_harness.py#Rig` を指す行 | 2 行・1 ファイル | 16 行・3 ファイル（`test_T11_rename.py`・`test_T11_rename_real.py` を含む） |
  | `untyped_calls/app/engine.py#Packer` を指す行 | 0 | 2 行（`app/service.py`） |
  | テスト・フィクスチャのモジュール名の外部シンボル（`lk://_ext/test_%`・`_t11_harness%`・`app`・`shop%`・`pkg#%`・`_server_cmd%`・`compact_grammar%`） | 102 | 6（すべて `sys.path.insert` によるもの〔上の限界〕と `spikes/` の中。`9f4ffa3` に載せた後。`tests/conftest.py` が入る前の `c7f2b7e` の上では 7〔`_server_cmd` が残る〕） |
  | ワークスペース内を指す行 / 外部を指す行 | 43,168 / 46,846 | 44,056 / 45,806 |
  | 走査の時間（同じ機械で続けて） | 46.4 秒 | 44.2 秒 |

  import の解決だけの時間（393 ファイル、3 回の最良）は 4.35 秒 → 5.30 秒（根が増えた分。走査全体では差が見えない）。
- 足したテスト:
  - `tests/unit/test_syntax_import_roots.py`（32 件）: 兄弟のテストモジュール・テストの土台の兄弟・テストのディレクトリがルートより先・
    テストでないファイルは兄弟を引かない・`__init__.py` あり（`tests` から `unit.helpers`）／ルートまであり／識別子でない名前・
    `conftest.py` の根（近いものから）・`importlib` では足さない・`json` は外部（`tests/unit/json.py` があっても）・`pytest` 自身・
    標準ライブラリでない名前は手元・ルートの今までの扱い・`pythonpath`（順・標準ライブラリは解かない）・フィクスチャの中・
    フィクスチャが先・ワークスペースへのフォールバック・フィクスチャの `src`・入れ子・フィクスチャの設定・明示の `source_roots` は今までどおり・
    `exists` は 1 パス 1 回、設定ファイル 6 種の読み取り・優先順・フィクスチャの設定と悪いパス・指紋。
  - `tests/unit/test_engine_indexer_import_roots.py`（7 件）: 索引の行（兄弟・フィクスチャ・外部シンボルが 0）、兄弟を後から作る、
    `__init__.py` を後から作る、`pyproject.toml` の `pythonpath` を足す・関係ない鍵だけ変える（解き直さない）・消す、`pytest.ini` を作る・消す、
    止まっている間の `setup.cfg` の編集を差分走査が拾う、`--import-mode=importlib`。
  - `tests/contract/test_T06_T07_import_roots.py`（4 件）: T-06 の `Env` の参照に import と型注釈の使用、`Rig` / `Rig.pos` の参照、
    フィクスチャの `Packer` の参照、T-07 の `build_env` の呼び出し元。
  - 直す前の `src` では、足した索引器と契約のテスト 11 件がすべて落ちる。
