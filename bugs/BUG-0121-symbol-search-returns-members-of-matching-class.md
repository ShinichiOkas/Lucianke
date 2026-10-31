# BUG-0121 search_workspace_symbols が修飾名の部分一致で、一致したクラスのメンバーまで返す（能力ベンチの T02 で適合率 0.5）

- 報告: 2026-10-31 / 見た人: 親（能力測定の基準〈Mnemo の能力ベンチ〉の課題 T02）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`main` = `b124359`、Mnemo の固定の写し）

- T02「名前に 'InstanceLock' を含むクラス・関数をすべて」（`search_workspace_symbols {"query": "InstanceLock"}`）: 再現率 1.0、適合率 0.5。
- 誤検出 4 件は `companion_sdk/src/mnemo_companion/_instance.py` の `InstanceLock` クラスのメンバー（28 行 `InstanceLock.info_path`、32 行 `InstanceLock.acquire` など）。メンバーの名前自体は 'InstanceLock' を含まず、修飾名（`InstanceLock.acquire`）が含む。

## 利用者（呼ぶ側の AI）として困ること

- 名前で探すと、一致したクラスの中身が並んで、探している定義が埋もれる（トークンも使う）。メンバーが欲しいときは `InstanceLock.` のようにドット付きで探せる。

## 原因・直し方・確認（2026-10-31）

- 原因: `fuzzy` / `prefix` の当て先に限定名（`qualified_name`）が入っていた。索引の検索（`index/queries.search_symbols_fts`）の FTS5 は
  `fuzzy` で `name` / `qualified_name` / `signature` / `doc_summary` の全列、`prefix` で `{name qualified_name}` を見ていて、
  `InstanceLock.acquire` の限定名の語 `instancelock` に `InstanceLock` の語が当たった。T-03 の順位付け（`_names_match`）も
  限定名の部分文字列・語を「名前の一致」に数えていたので、メンバーが名前の部分一致（BUG-0069 の 3 段目）として並んだ。
- 直し方（呼ぶ側の AI の使いやすさで決めた）: query に `.` が無いときは、シンボル自身の名前（`name`）だけに当てる。
  FTS5 の列は `fuzzy` で `{name signature doc_summary}`、`prefix` で `{name}`。順位付けの「名前の部分一致」も `name` の部分文字列・語だけで見る。
  query に `.` があるとき（`InstanceLock.acq`）だけ今までどおり限定名に当てる。BUG-0069 の順位（完全一致 > 前方一致 > 名前の部分一致 >
  シグネチャ・Doc だけの一致）はそのまま。`exact` と `regex` は変えていない。ツールの説明に「`.` が無ければ自身の名前、メンバーは `Class.mem`」を 1 文足した。
- 仕様書 T-03 の処理規約 2 に「当てる名前」の規則と理由を書いた。
- 試験: `tests/contract/test_T03_T04.py::test_t03_query_without_dot_matches_own_name_not_members_of_a_matching_class`
  （`InstanceLock` で `InstanceLock`・`DirInstanceLock` だけが返り `InstanceLock.acquire` などのメンバーは出ない、`prefix` も同じ、
  `InstanceLock.acq` でメンバーが返る、名前で当たるメンバー `holder_pid` は今までどおり）。`tests/unit/test_index_ingest.py` の
  `test_search_prefix` / `test_search_fuzzy` の期待を直した（`pay` で `PaymentProcessor.process_charge` を返さない。`.` 付きで返す）。
- 能力ベンチ（Mnemo の固定の写し、`--only T01,T02`）: **T02 F1 0.667 → 1.0**（適合率 0.5 → 1.0・再現率 1.0。返るのは `InstanceLock`・`DirInstanceLock`・`RecallInstanceLock`・テストの `TestInstanceLockHolder` の 4 件、応答 282 → 125 トークン）。BUG-0120 の確認と同じ走りで測った。
