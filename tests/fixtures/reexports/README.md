# reexports

BUG-0100（再び公開した名前を定義元までたどる）・BUG-0101（名前空間パッケージを外部にしない）のフィクスチャ
（`tests/unit/test_engine_indexer_reexports.py`・`tests/contract/test_T06_T07_reexports.py`）。LSP なしの `structural` の索引。

| 箇所 | 書き方 | 期待 |
| --- | --- | --- |
| `use.py` `run` | `from pkg import flush` の `flush()`（`pkg/__init__.py` が `from .helpers import flush`） | `pkg/helpers.py#flush` に `structural`（1 段） |
| `use.py` `run` | `from pkg import deep_go` の `deep_go()`（`pkg/__init__.py` → `pkg/sub/__init__.py` → `pkg/sub/impl.py`） | `pkg/sub/impl.py#deep_go` に `structural`（2 段） |
| `use.py` `run` | `from pkg.alias import fl` の `fl()`（`pkg/alias.py` が `from .helpers import flush as fl`。`__init__.py` でない普通のモジュール） | `pkg/helpers.py#flush` に `structural`（別名） |
| `use.py` `run` | `from pkg import absolute`（`pkg/__init__.py` が `from pkg.helpers import flush as absolute`。絶対 import） | `pkg/helpers.py#flush` に `structural` |
| `use.py` `run` | `from pkg import starred` の `starred()`（`pkg/__init__.py` の `from .stars import *`、`pkg/stars.py` の `__all__ = ["starred"]`） | `pkg/stars.py#starred` に `structural` |
| `use.py` `run` | `from pkg import hidden` の `hidden()`（`pkg/stars.py` が定義するが `__all__` に無い） | 行を作らない（`*` は公開しない） |
| `use.py` `run` | `from pkg import public` の `public()`（`from .loose import *`、`pkg/loose.py` に `__all__` が無い） | `pkg/loose.py#public` に `structural` |
| `use.py` `run` | `from pkg import _private` の `_private()`（`pkg/loose.py` の `_` で始まる名前） | 行を作らない |
| `use.py` `run` | `from pkg import loop` の `loop()`（`pkg/cyc_a.py` と `pkg/cyc_b.py` が互いに再び公開する循環） | 行を作らない（循環で止まる） |
| `use.py` `run` | `TokenEstimator().count("x")`・`EST.count("y")`（`EST = TokenEstimator()`）・`EST.reset()`（基底 `Base` のメソッド） | `pkg/est.py#TokenEstimator.count`・`pkg/est.py#Base.reset` に `structural` |
| `use.py` `run` | `Lock()`（`pkg/__init__.py` が外部の `from threading import Lock` を再び公開） | `lk://_ext/threading#Lock` |
| `use.py` `Sub` | `class Sub(TokenEstimator)` | `INHERITS` が `pkg/est.py#TokenEstimator` |
| `use.py` | `return flush`（呼び出しでない読み）・import 文の行 | `REFERENCES` / `IMPORTS` が定義元 |
| `use_ns.py` `run` | `import nsp.tools.fmt` の `nsp`・`nsp.tools`（`nsp/`・`nsp/tools/` に `__init__.py` が無い） | 参照を作らない。外部 `lk://_ext/nsp` にしない（BUG-0101） |
| `use_ns.py` `run` | `import nsp.tools as nt` の `nt.fmt.render()`、`from nsp import tools` の `tools.fmt.render()` | `nsp/tools/fmt.py#render` に `structural`（前は外部 `lk://_ext/nsp#tools.fmt.render`） |
| `use_ns.py` | `import nsp.tools.fmt` の import 文の行 | import したモジュール `nsp/tools/fmt.py` への `IMPORTS`（先頭の `nsp` にはファイルが無い） |
