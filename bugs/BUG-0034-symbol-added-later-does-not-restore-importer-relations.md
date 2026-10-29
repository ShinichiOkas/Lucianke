# BUG-0034 既存ファイルに後からシンボルを足しても、すでに索引済みの import 元の関係が戻らない（宙に捨てられたまま）

- 報告: 2026-10-29 / 見た人: BUG-0025 を直す役
- 見たこと:
  - `pkg/b.py`（`def other(): ...` だけ）、`pkg/a.py`（`from pkg.b import helper` / `def run(): return helper()`）の順に `Indexer.index_file` で取り込む。このとき `relations` は 0 行（`helper` が `symbols` に無いので `_settle_relations` が捨てる。`_ext` にもならない）。
  - そのあと `pkg/b.py` に `def helper(): ...` を足して `index_file("pkg/b.py")`（新規ファイルではなく既存ファイルのシンボル追加）を呼んでも、`relations` は 0 行のまま（`SELECT source_urn, relation_kind, target_urn FROM relations` で確認）。`a.py` を取り込み直す（内容が変わる）まで直らない。
  - BUG-0025（ファイルの新規・削除）と同じ根（import 元の解決結果が、定義元の状態が変わっても更新されない）。BUG-0025 の修正は `_import_probes`（import 解決で調べたパスの語幹）を使うが、シンボルの追加はそこで拾っていない。
- 再現手順: 上の順序で `index_file` を呼ぶ（スクリプトは `tests/unit/test_engine_indexer_relations.py` の `Bare` と同じ形で、tmp にワークスペースを作る）。
- 調査はしていない（直す役の仕事）。ただし BUG-0025 の修正では、既存ファイルの編集のたびに import 元を取り込み直す費用（よく import されるモジュールで数百ファイル）を避けるため、あえて拾っていない。

## 原因
- import 元（`a.py`）の `relations` は、取り込んだ時点の定義元（`b.py`）のシンボルで決まる。`from pkg.b import helper` は `pkg/b.py` が在れば目標が `pkg/b.py#helper`（内部の URN）になり、`b.py` の `symbols` にまだ `helper` が無いと `_settle_relations` が「`symbols` に無い目標」として行を捨てる（`_ext` にはならない。`_ext` に倒れるのは定義元のファイルそのものが無いときだけ）。
- 捨てられた行は、あとで定義元に `helper` が足されても作り直されない。`ingest_file` の `_sync_symbols` が直すのは「すでにある行の `target_file_id`」だけで、行が無ければ何も起きない。BUG-0025 の `_import_probes` による解き直しは、定義元の**ファイルの新規・削除**（`known is None` の `index_file`、`remove`、`scan` の created / removed）にしか掛かっておらず、既存ファイルのシンボルの増減はその外だった。
- 逆に定義元から `helper` を消したときは、`_sync_symbols` が `target_urn = helper` の行を消すので行は残らない。ただしオーバーロードの宣言が増減した場合は、import 元の行が最後の宣言へ付け替えられている（`_redirect_overloads`）ため、宣言の増減でも import 元の解き直しが要る。

## 直し方
- 判定を **シンボルの URN の集合が変わったか**（足された・消えた。外部シンボルは含めない）にした。中身だけの編集（集合が同じ）では import 元に触れない。編集のたびに import 元を取り込み直すと、よく import されるモジュールで数百ファイルの解析をやり直し、`exact` の行を失う（ADR-8）ためで、BUG-0025 が避けた費用をそのまま避ける。
- `index_file`: 書き込みの前に、読み取り接続で `symbols` の URN の集合と、取り込む事実（`FileFacts.symbols`）のそれを比べる（`reresolve.symbols_changed`）。違うときだけ `prepare_importers` で import 元の事実を作り、定義元の取り込みと**同じ書き込みジョブ**で `ingest_importers` する（`reresolve.importers_to_redo`。新規ファイルのときは従来どおり常に）。`data_version` は 1 回だけ進む。`indexer.py` の変更は 1 行（`importers_to_redo` の呼び出し）と受入検査用のカウンタ `Indexer.reingested_importers`（取り込み直した import 元のファイル数。`ingest_calls` には入れない）。
- `scan`（起動時の差分走査）にも同じ穴があった（定義元だけが変わっていて import 元は変わっていない）。`IngestStats` に `path` と `symbol_set_changed`（`symbols_added` / `symbols_removed`）を足し、走査が取り込んだ既存ファイルのうち集合が変わったものを `after_scan(changed=...)` に渡して、この走査で取り込まなかった import 元を解き直す（BUG-0025 の created / removed と同じ経路。`data_version` は進めない）。
- 設計書 §7.6.2 の `Indexer` の節は `reresolve` を載せていないので（BUG-0025 から）、設計書は変えていない。仕様は `engine/reresolve.py` の docstring にある。
- テスト（`tests/unit/test_engine_indexer_relations.py`、Python と TypeScript）: 票の再現（足すと結ばれる・`target_file_id` が埋まる・`data_version` は +1 のまま・取り込み直した import 元は 1 件）、名前を消すと行が捨てられる（`_ext` ではなく行なし。最初から無かったときと同じ）、本文だけの編集では import 元を取り込み直さない（`reingested_importers` が増えず、import 元の `relation_id` が不変）、import 元が無ければ何も取り込み直さない、差分走査で足された・消えた場合。

## 確認
- 票の手順（`pkg/b.py` に `def other()` だけ、`pkg/a.py` が `from pkg.b import helper`）で、`b.py` に `helper` を足して `index_file` すると、`a.py` の IMPORTS / CALLS が `pkg/b.py#helper` に結ばれる（修正前は 0 行のまま）ことをテストで確認した。修正を外す（`symbols_changed` を常に `False`）と、足す・消す・本文のみの 6 件が落ちる。
- `tests/unit/test_engine_indexer_relations.py`・`test_engine_indexer.py`・`test_import_graph.py` ほかを 3 回続けて green。
