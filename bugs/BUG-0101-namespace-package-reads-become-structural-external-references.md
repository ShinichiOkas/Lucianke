# BUG-0101 名前空間パッケージの `import nsp.tools.fmt` の `nsp`・`nsp.tools` の読みが外部 `lk://_ext/nsp`・`lk://_ext/nsp#tools` への `structural` の参照になる

- 報告: 2026-10-31 / 見た人: BUG-0093・BUG-0094・BUG-0095 を直す役（点つきのモジュールのパスを確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`tests/fixtures/untyped_refs` を `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0093〜0095 の直しを載せたコード）

```
nsp/tools/fmt.py     def render():\n    return "x"          （nsp/・nsp/tools/ に __init__.py は無い）
use_modules.py       import nsp.tools.fmt
                     ...
                     def run():
                         ...
                         nsp.tools.fmt.render()
```

- `use_modules.py#run` の `relations`（`nsp.tools.fmt.render()` の行）:
  - `nsp/tools/fmt.py#render`（`CALLS`）と `nsp/tools/fmt.py`（`REFERENCES`）はワークスペース（BUG-0093 の直し）。
  - `lk://_ext/nsp`（`REFERENCES`、`nsp` の読み）と `lk://_ext/nsp#tools`（`REFERENCES`、`tools` の読み）は外部。どちらも
    `confidence_level = structural`。
- `nsp/` はワークスペースのディレクトリ（`nsp/tools/fmt.py` がある）で、外部のパッケージではない。

## 利用者（呼ぶ側の AI）として困ること

- T-06 / T-09 などで見ると、ワークスペースの名前空間パッケージを使う箇所に、確かな（`structural`）外部パッケージ `nsp` への参照が
  混ざる（外部の依存があるように見える）。

## 原因・直し方・確認（2026-10-31）

BUG-0100 と一緒に直した。

### 原因

- `__init__.py` の無い `nsp` は `imported_name_map` でファイルに解けないので、`external_name_map` が `import nsp.tools.fmt` の `nsp` を外部
  `lk://_ext/nsp` に束ねていた。BUG-0093 の点つきのモジュールのパスの解決はファイル（`nsp/tools/fmt.py`）だけを答え、ディレクトリの
  `nsp.tools` には `None` を返すので、抽出器は受け手 `nsp` の型（外部）から `lk://_ext/nsp#tools` を作っていた。`import nsp.tools as nt` の
  `nt.fmt.render()`・`from nsp import tools` の `tools.fmt.render()` はワークスペースの関数なのに外部 `lk://_ext/nsp#tools.fmt.render` だった。

### 直し方（決めたこと）

- **名前空間パッケージを見分ける**（`syntax/imports.ImportResolver.namespace_dir`）: どの根にもファイルが無く、根の順に `<根>/<mod>` が
  `__init__.py(i)` の無いディレクトリなら名前空間パッケージ（Python と同じ。標準ライブラリの名前は除く）。それを束ねた名前
  （`namespace_name_map`: `import nsp.tools.fmt` の `nsp`・`import nsp.tools as nt`・`from nsp import tools`）は、import したパスそのもの
  （最上位より下）がワークスペースにあるときだけ数える。`import packaging`・`import packaging.version` は、同名のディレクトリ
  （Lucianke 自身の `packaging/`）があっても外部の普通のパッケージとみなして外部のまま（Python では普通のパッケージが名前空間
  パッケージの部分より勝つ）。
- **読みは参照にしない**（`dotted_module_path` が `NAMESPACE_PACKAGE` と答える）: 名前空間パッケージの名前とそのディレクトリ（`nsp`・
  `nsp.tools`・`nt`・`tools`）の読みは行を作らず、外部にも名前だけにもしない。その下のファイル（`nt.fmt`）はモジュール、ワークスペースに
  無い先（`nsp.ext`。別の場所にある部分かもしれない）は今までどおり外部。普通のパッケージの下の名前空間パッケージ（`pkg.nsdir`）も同じ。
  `import nsp.tools.fmt` の import 文の行は import したモジュール `nsp/tools/fmt.py` への `IMPORTS`（先頭の `nsp` にはファイルが無い）。
  ディレクトリだけの import（`import nsp.tools as nt`）は行を作らない。
  理由（T-06 にとって最も役に立つもの）: T-06 はモジュール・パッケージ（ファイルのみの URN）を対象に取らない（`urn does not name a code
  symbol`）。索引はディレクトリのノードを持たないので、ディレクトリへの参照の行を作っても誰も引けない。誤った外部の参照を消し、その下の
  関数への呼び出しを正しく結ぶのが答えを良くする。
- **編集に追従**: 調べたディレクトリは語幹 `?dir:<ディレクトリ>` として `_import_probes` に書き（`reresolve.DirProbeRecorder`）、ファイルが
  作られた・消えたら祖先のディレクトリの `?dir:` でも import 元を引く（ディレクトリの最初・最後のファイルで名前空間パッケージができる・
  無くなる）。schema v10 の移行で Python のファイルを取り込み直させる。
- 仕様書: T-06 処理規約 9 (a)・T-07 処理規約 11。設計書: §7.4.6 項 1・§7.6.2。

### 確認

BUG-0100 の「確認」と共通（Lucianke 自身で `lk://_ext/nsp` の 3 行が消え、`packaging` の外部の 3 行は変わらない。名前空間パッケージの
テストは `tests/unit/test_engine_indexer_reexports.py` の 2 件・`tests/unit/test_syntax_imports.py`・`tests/contract/test_T06_T07_reexports.py`
の 1 件。既存の `tests/unit/test_engine_indexer_untyped_refs.py` の `nsp.tools.fmt.render()` も通る）。
