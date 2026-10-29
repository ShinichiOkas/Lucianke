# BUG-0025 後から作られたファイルを、すでに索引済みの import 元の relations が拾わない（外部扱いのまま残る）

- 報告: 2026-10-29 / 見た人: 関係抽出を航行ツールの要求に合わせる役（7j）
- 見たこと:
  - 作業ツリーに `pkg/__init__.py` と `pkg/a.py`（`from pkg.b import helper` / `def run(): return helper()`）だけがある状態で、`Indexer.index_file("pkg/__init__.py")`、`index_file("pkg/a.py")` の順に取り込み、そのあと `pkg/b.py`（`def helper(): ...`）を作って `index_file("pkg/b.py")` を呼んだ。
  - その後の `relations` は次の 2 行のまま（`pkg/b.py#helper` への行は無い）:
    - `lk://ws/pkg/a.py | IMPORTS | lk://_ext/pkg#b.helper`
    - `lk://ws/pkg/a.py#run | CALLS | lk://_ext/pkg#b.helper`
  - `a.py` を取り込み直す（内容が変わる）まで直らない。`scan()`（全体）は import 解決を `exists`（ファイルシステム）で行うため、走査では起きない。ファイル監視の差分（新規ファイルの追加）で起きる。
  - 7j の変更前は、この 2 行が「捨てられる」ので 0 行だった。変更後は「外部シンボルへの行」になる（どちらも b.py が作られても a.py の行が更新されない点は同じ）。
- 再現手順: 上の順序で `Indexer.index_file` を呼び、`SELECT source_urn, relation_kind, target_urn FROM relations` を見る（スクリプトは `probe_order.py` の形。tmp にワークスペースを作る）。
- 調査はしていない（直す役の仕事）。
