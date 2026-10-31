# BUG-0124 get_repo_map が非公開のクラスのメンバーを公開シンボルとして地図に出す（`_DbLock.hold`）

- 報告: 2026-10-31 / 見た人: BUG-0120 を直した役（能力ベンチの T01 の地図を読んでいて）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`main` = `ba80b1e`、Mnemo の固定の写し、`get_repo_map` 既定）

- `src/mnemo/storage/db.py` のブロックの先頭のシンボルが `[Method] pub _DbLock.hold(self, what: str, sql: str | None = None) -> "_Hold" @L1162-1163`。
- 索引（`symbols`）では `_DbLock`（1098 行の Class）の `modifiers_json` が `["priv"]`、そのメソッド `_DbLock.__init__` / `acquire` / `release` / `locked` / `__enter__` / `__exit__` / `hold` は `["pub"]`（`_should_warn` / `_report` は `["priv"]`）。
- 地図はクラスを公開でないとして外し、メソッドだけを親の名前つき（`_DbLock.hold`）で、ファイルの公開シンボルの上位として並べている。

## 利用者（呼ぶ側の AI）として困ること

- モジュールの外から使わない内部のクラスのメソッドが「このファイルの重要な公開 API」として地図の限られた行を使う。
- `pub` の印が付くので、外から呼んでよいものと誤解しうる。

## 原因・直し方・確認（2026-10-31）

### 原因

「公開シンボル」の判定が、シンボル自身の修飾子（`priv` / `prot` を持たない）しか見ていなかった。Python の抽出器は名前が `_` 始まりかどうかだけで
`priv` / `pub` を付けるので、`_DbLock`（`priv`）のメソッド `hold` は `pub`。TS も export されていないクラスは `priv`、そのメンバーはクラスの
中の `private` / `#` でなければ `pub`。親の可視性を見ていないので、`_DbLock.hold` は T-01 の代表（`_eligible`）にも、ファイルの重要度の
SQL（`rank.PUBLIC_SYMBOL_SQL`。`files.pagerank_score` と `repo_map_files`）にも数えられていた。親（クラス）が外されるだけでメンバーが残り、
親の名前つき（`_DbLock.hold`）で上位に出た。

### 直し方

- 公開シンボルの定義に「祖先（親・その親…）にも `priv` / `prot` のシンボルが無い」を足した（実効的に非公開とみなす）。
  Python の `_` 始まりのクラス・入れ子の非公開クラス（`Store._Cursor.advance`）、TS の export されていないクラスのメンバーが同じ規則で外れる。
  関数の中の定義は抽出器がもともと `priv` を付けている。
- `tools/t01_get_repo_map.py`: `_hidden_by_ancestor`（親の連鎖に `priv` / `prot` があるシンボルの urn）を `_select` の候補から除く。
- `index/rank.py`: `PUBLIC_SYMBOL_SQL` に再帰 CTE で祖先の修飾子を見る条件を足した（`parent_urn` が NULL なら素通り）。ファイルの重要度が、
  非公開クラスのメンバーの被参照では上がらなくなる。Lucianke 自身の索引（14,919 シンボル）で、代表になれるシンボルのうち ranked な
  8,992 → 8,757 件（235 件が外れた）、`repo_map_files` 0.13 秒・条件の評価に差は出ない（0.03〜0.05 秒）。
- 仕様書 T-01 処理規約 2「公開シンボル」と設計書の PageRank の節（ファイルのスコア）を合わせた。
- スキーマは変えない（表も抽出器の出力も同じ）。T-01 は `repo_map_files` を読むたびに計算するので、既存の索引でもすぐ直る。
  `files.pagerank_score`（書き込み済みの列）は次の順位の再計算（ファイルの更新・走査）で揃う。

### 確認

- 新しい `tests/unit/test_tools_repo_map_private_parents.py`（5 件）。修正前は `_DbLock.hold` / `_DbLock.release` が `src/db.py` の
  シンボルに出て赤（`['_DbLock.hold', '_DbLock.release', 'Store', 'Store._Cursor.advance', 'Store.open', 'connect']`）、修正後は緑。
  Python の `_` 始まりのクラス・入れ子の非公開クラス・3 通りの `ranking`・TS の export されていないクラス・非公開クラスだけのファイルの
  重要度が 0（`rank.repo_map_files` と `files.pagerank_score`）・公開のメンバーは残ること。
- 関係する既存の試験（`test_index_rank.py`・`test_T01_repo_map.py`・`test_tools_overview.py`・`test_engine_indexer_rank_refresh.py`・
  `test_engine_rank_scheduler.py`）は変更なしで緑。
