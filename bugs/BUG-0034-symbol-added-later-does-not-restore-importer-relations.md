# BUG-0034 既存ファイルに後からシンボルを足しても、すでに索引済みの import 元の関係が戻らない（宙に捨てられたまま）

- 報告: 2026-10-29 / 見た人: BUG-0025 を直す役
- 見たこと:
  - `pkg/b.py`（`def other(): ...` だけ）、`pkg/a.py`（`from pkg.b import helper` / `def run(): return helper()`）の順に `Indexer.index_file` で取り込む。このとき `relations` は 0 行（`helper` が `symbols` に無いので `_settle_relations` が捨てる。`_ext` にもならない）。
  - そのあと `pkg/b.py` に `def helper(): ...` を足して `index_file("pkg/b.py")`（新規ファイルではなく既存ファイルのシンボル追加）を呼んでも、`relations` は 0 行のまま（`SELECT source_urn, relation_kind, target_urn FROM relations` で確認）。`a.py` を取り込み直す（内容が変わる）まで直らない。
  - BUG-0025（ファイルの新規・削除）と同じ根（import 元の解決結果が、定義元の状態が変わっても更新されない）。BUG-0025 の修正は `_import_probes`（import 解決で調べたパスの語幹）を使うが、シンボルの追加はそこで拾っていない。
- 再現手順: 上の順序で `index_file` を呼ぶ（スクリプトは `tests/unit/test_engine_indexer_relations.py` の `Bare` と同じ形で、tmp にワークスペースを作る）。
- 調査はしていない（直す役の仕事）。ただし BUG-0025 の修正では、既存ファイルの編集のたびに import 元を取り込み直す費用（よく import されるモジュールで数百ファイル）を避けるため、あえて拾っていない。
