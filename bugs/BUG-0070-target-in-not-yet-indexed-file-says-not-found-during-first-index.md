# BUG-0070 初回の索引中、まだ索引していないファイルの対象が `E_TARGET_NOT_FOUND`「file is not in the index」になり、検索は 0 件になる

- 報告: 2026-10-31 / 見た人: 親（exe の試しと dogfood）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（2026-10-31、PyInstaller でビルドした exe、`tests/fixtures/poly` の写しを cwd にして stdio で起動）

`configure_workspace set_roots` の応答が `jobs: job_0001 reindex running 0%` のとき、直後の呼び出し:

```
--- search_workspace_symbols {"query": "process_charge"}  isError=False
query: "process_charge" match=fuzzy hits=0
--- query_call_hierarchy {"target": {"path": "src/shop/services/payment.py", "symbol_name": "PaymentProcessor.process_charge"}, ...}  isError=True
# query_call_hierarchy error E_TARGET_NOT_FOUND
! file is not in the index: src/shop/services/payment.py
--- find_references_and_implementations（同じ対象）  isError=True
# find_references_and_implementations error E_TARGET_NOT_FOUND
! file is not in the index: src/shop/services/payment.py
```

`get_repo_map` は `! ranking=alpha (graph not ready)` だけを返した。索引が終わってから同じ呼び出しをすると、検索は 1 件・T-07・T-06 は正しい答え。
Lucianke 自身のリポジトリでは、初回の索引に 84.5 秒かかった（dogfood）。

## 利用者（呼ぶ側の AI）として困ること

- ディスクにあるファイルを「無い」と言われる。呼ぶ側は対象の指定を誤ったと判断して、別の探し方へ移る（索引待ちだと分からない）。
- 検索の 0 件も「存在しない」と区別がつかない（索引の進み具合が応答に無い）。
