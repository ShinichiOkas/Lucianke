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
