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

## 原因
- import の解決（`syntax/imports.py` の `resolve_module`）は、定義元ファイルの有無を **取り込んだ時点のファイルシステム**（`Indexer._exists`）で決める。解決結果（`lk://ws/pkg/b.py#helper` か `lk://_ext/pkg#b.helper` か、相対 import なら行なし）は取り込んだ側の `relations` に焼き付き、あとで定義元が作られた・消えても、取り込んだ側を取り込み直さない限り変わらない。`_settle_relations` は「`symbols` に無い目標の行を消す」「`target_file_id` を埋める」だけで、`_ext` ↔ 実体の付け替えはできない。
- DB には「どのファイルがどのモジュールを import しているか」を引ける表が無かった（`relations` の IMPORTS 行は解決に成功した・外部に倒れた分だけ。**未解決の相対 import は行そのものを持たない**ので、`relations` からは辿れない）。
- 票に書かれた順序（a.py → 後から b.py）のほかに、同じ根の別の現れ方を確かめた: (1) 相対 import（`from .b import x`、TS の `./util`）は b が無い間は行が 0 件で、後から作っても現れない、(2) b.py がディスクにはあるが索引にまだ無いうちに a.py を取り込むと、`_settle_relations` が行を捨てる（`_ext` の痕も残らない）、(3) b.py を消すと、a.py の行は宙に浮くか（モジュール目標）消える（`remove_file` が目標側の行を消す）だけで、外部には戻らない、(4) `scan()` の差分走査でも、変わっていない a.py は取り込まれないので、走査の間に b.py ができていれば同じ（票の「scan では起きない」は初回の全体走査だけ）。

## 直し方
- **import 解決で調べたパスの語幹を DB に残す**。内部表 `_import_probes(file_id, stem)`（`schema.sql` v3、`migrate.py` の `2 → 3` で足す。`ON DELETE CASCADE`、`stem` に索引 `_import_probes_stem`）。`engine/reresolve.py` の `ProbeRecorder` が `make_resolver` の `exists` を包み、**見つかった・見つからなかった両方** のパスの語幹（拡張子と `__init__` / `index` を除いたもの。`pkg/b.py` も `pkg/b/__init__.py` も `pkg/b`）を集め、`FileFacts.import_stems` → `ingest_file` が `(file_id, stem)` を全置換する。語幹はパスの束なので行数は import 1 件あたり数行で済む。
- **ファイルが作られた・消えたとき、その語幹を持つファイルだけ** を `_import_probes` から引き、今のファイルシステムで解析し直して取り込み直す（全体の再走査はしない）:
  - `Indexer.index_file`（新規 = `files` に行が無かったとき）と `Indexer.remove`: 書き込みの前に `reresolve.prepare_importers` で事実を作り、**同じ書き込みジョブ** で `ingest_file`（import 元）+ `_settle_relations`。`data_version` は 1 回だけ進む（`test_deleted_file_disappears` などの「+1」が保たれる）。
  - `Indexer.scan`（差分のときだけ）: 最後に、この走査で新しく見つかった・消えたファイルの語幹を持つファイルのうち **この走査で取り込まなかったもの** を `reresolve.after_scan` が解き直す（別トランザクション、`data_version` は進めない。`_settle_relations` と同じ扱い）。初回の全体走査（`known` が空）は何もしない。
  - 自分自身（`__init__.py` が自分の親ディレクトリの語幹を調べる）は対象外にする（除外に変わっただけで実体が残るファイルを `remove` した直後に取り込み直して蘇らせないため）。解析に失敗した（`files` 行だけに落ちる）・読めない import 元は取り込み直さない（いまある行を壊さない）。
- 改名は `remove` + `index_file` で組み合わさる（旧名を import していた側は外部へ、新名を import していた側は実体へ）。
- 副作用（知っておく点）: 取り込み直された import 元は、同じ内容でも `exact` の行を失い `structural` に戻る（ADR-8。次に LSP が答えたとき戻る）。`files_fingerprint`（`(path, sha256)` の指紋）は import 元の sha が変わらないので動かない。T-06 / T-07 が LSP の結果を「完全」と覚えている印を `files_fingerprint` で鮮度判定している場合、新規・削除ファイルでは指紋が変わるので印は外れる（この修正で挙動は変わらない）。
- 範囲外として切り出したもの: 既存ファイルに **シンボルが増えた** だけ（b.py に後から `helper` を足した）ときは、a.py の行は捨てられたまま戻らない → BUG-0034。

## 確認
- `tests/unit/test_engine_indexer_relations.py` に 10 本（`stem_of` の表は 9 ケース。票の再現〔IMPORTS と CALLS が `pkg/b.py#helper` になり `target_file_id` も埋まる、`data_version` が +1〕、ファイル削除で `_ext` に戻る〔+1〕、改名、TS の相対 import〔作ると現れ、消すと消える〕、Python の相対 import、ディスクにあるが未索引の b.py、既存ファイルの編集で import 元を取り込み直さない、`__init__.py` を除外 remove しても蘇らない、差分走査で取り込まれない import 元が解ける〔作成・削除の両方〕、`_import_probes` の置換と CASCADE、`stem_of`）。`tests/unit/test_index_core.py` に `2 → 3` の移行（データが残る）、`1 → 3` の連鎖。`tests/unit/test_index_schema.py` の許可リストに内部表と索引を足した。
- `tests/contract/test_T06_T07.py::test_real_file_created_after_its_importer_is_seen_by_t06_t07`: `hook.py` を先に、`audit.py` を後から `index_file` で取り込むと、T-07 の outgoing が外部ノード（`audit.record`）から実体（`record`、`is_external: false`）に変わり、incoming と T-06 の参照に `hook.py` の呼び出しが載る。
- `tests/unit/test_engine_indexer.py`・`test_engine_indexer_relations.py`・`test_index_ingest.py`・`test_index_core.py`・`test_index_schema.py`・`test_import_graph.py`・`tests/contract/test_T06_T07.py` を 3 回続けて green（169 件）。`ruff check` / `ruff format --check` clean。
