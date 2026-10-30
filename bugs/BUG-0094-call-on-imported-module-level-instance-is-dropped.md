# BUG-0094 import したモジュール直下のインスタンス `settings` の `settings.reload()` が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0085 を直す役（受け手の決め方を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。`8c10a2a` のコードでも BUG-0085 の直しを載せたコードでも同じ）

```
pkg/conf.py   class Settings:\n    def reload(self):\n        return 1\n\n\nsettings = Settings()
use.py        from pkg.conf import settings\n...\ndef f():\n    settings.reload()
```

- 抽出器（`syntax/relations.extract_relations`、import 表 `{"settings": "lk://ws/pkg/conf.py#settings"}`）の候補は
  `CALLS reload -> lk://ws/pkg/conf.py#settings.reload`（`name_only=False`）。
- 索引した後の `relations` に `use.py#f` の `reload` の行は無く、`_name_calls` にも無い（`use.py` の `CALLS` は 0 行、ワークスペース全体の
  `_name_calls` は別の 1 行だけ）。
- `pkg/conf.py#settings.reload` という URN のシンボルは無い（`settings` は変数）。

## 利用者（呼ぶ側の AI）として困ること

- `Settings.reload` を変える前に T-06 / T-07 で呼び出し元を見ても、`from x import logger` / `settings` のような共有インスタンス経由の呼び出しは
  `structural` でも名前の一致（`heuristic`）でも出ず、警告も無い。

## 原因・直し方・確認（2026-10-31）

BUG-0093・BUG-0095 と一緒に直した。

### 原因

- 抽出器は `from pkg.conf import settings` の `settings.reload()` の受け手 `settings` を import 表で `pkg/conf.py#settings` に解き、
  「型の分かった受け手（別ファイル）」として `pkg/conf.py#settings.reload` を先にしていた（`_member_of` の `("urn", 型)` は `<型>.name`）。
  `settings` がクラスでなく変数だとは、そのファイルを読まない抽出器には分からない。
- 関係を整える段（`engine/indexer._settle_relations`）は、`symbols` に無い先の行を消す前に、先の「クラスの部分」がワークスペースのクラス
  のときだけ `_member_refs` に退避していた（BUG-0075）。`pkg/conf.py#settings` は変数なので退避されず、行は消えた。名前だけの呼び出し
  （`_name_calls`）にも入らない（抽出器の段では先が決まっていた）ので、`structural` でも名前の一致でも出ず、警告も無かった。
- 同じ形で、`pkg/__init__.py` が `from .conf import settings as shared` で再び公開した `shared` の `shared.reload()`（先の「クラスの部分」が
  シンボルでない）も消えていた（Lucianke 自身の `EST = TokenEstimator()` の `EST.count()`。`TokenEstimator` は `mcp/budget.py` が
  `core/tokens.py` から再び公開している）。

### 直し方（決めたこと）

- **定義元の変数の型を索引に残す**（内部表 `_var_types`、schema v9）: 抽出器がモジュール直下の変数・定数の型を構文から読めるとき
  （`settings = Settings()`・`settings: Settings = ...`・TS の `export const meter = new Meter()`）、その型の URN（同じファイル・import した
  クラス、外部の型）を `extract_relations_detailed` の `var_types` で返し、取り込みが書く（ファイルごとに全置換）。
- **整えで変数の受け手を型のメンバーに結ぶ**（`index/inherited.py`）: 先の「クラスの部分」がワークスペースのモジュール直下の変数・定数
  なら `_member_refs` に退避する（`class_urn` は変数）。結ぶときは `_var_types` の型の C3 線形化で探す（BUG-0075 と同じ。`structural`）。
  型が外部なら外部のメンバー（`lock = Lock()` の `lock.acquire()` → `lk://_ext/threading#Lock.acquire`。外部シンボルの行も作る）。
  型が構文から分からなければ（`loose = make()`）、その位置を `_name_calls` に書いて名前の一致に任せる（`heuristic` か曖昧と書く）。
  `pkg/conf.py#settings.reload` という無い先には結ばない。
- **編集に追従**: 結び直す範囲に「整えるファイルの変数を受け手にする退避行」と「型が整えるファイルのクラス（とその派生クラス）である
  変数を受け手にする退避行」を足した。`loose = make()` → `loose = Settings()` の編集で、import している `use_conf.py` を取り込み直さずに
  `_name_calls` の行が消えて `Settings.reload` の行ができ、戻せば逆になる。
- **再び公開した名前**: 先の「クラスの部分」がシンボルでないが、そのファイルは索引にある行は、受け手の型が分からないのと同じなので
  消さずに `_name_calls` に移す（名前の一致に任せる）。再び公開した名前を定義元まで辿って `structural` にするのは別の票
  （**BUG-0100**）。
- 仕様書: T-06 処理規約 9 (a) に import した変数と再び公開した名前（理由 1 行）、T-07 処理規約 11 に 1 文。設計書: 内部表の一覧（v9）と §7.6.2。

### 確認（BUG-0093・BUG-0094・BUG-0095 共通）

Lucianke 自身（`525bd6e` のスナップショットを、修正前のコード〈`525bd6e` の `src`〉と修正後のコードで、同じ手順で順に in-process に索引。
LSP なしの `structural`。名前の判定は修正後の `tools/_name_calls.NameIndex` で数えた）:

```
relations（CALLS / REFERENCES）: 増えた 41 行・消えた 0 行
  import した変数・モジュールの変数の受け手（BUG-0094）: 38 行（ワークスペースの型 26・外部の型 12）
  サブモジュールそのものへの参照（BUG-0093）: 3 行
_name_calls: 10,275 → 22,389（call 10,290・r 11,946・w 145・rw 8）
  修正前に名前だけだった呼び出しのうち structural になった: 14（_lsp_exact.MEMO.clear() → _Memo.clear など）
  新しく名前だけになった呼び出し: 29（型の分からない import した変数・再び公開した名前の受け手。修正前は黙って消えていた）
    判定: builtin_name 25（EST.count() など）・候補なし 4
  型の分からない受け手の属性の読み書き（BUG-0095。修正前はどこにも残らなかった）: 12,099
    判定: heuristic 2,715・ambiguous 6,662・builtin_name 646・external_name 939・候補なし 1,137
_attr_defs 667 行・_var_types 225 行・_member_refs 343 → 387 行
索引の大きさ: 42.9 MB → 46.0 MB（走査の時間は並行する他の役の負荷で 31〜122 秒に揺れ、差は測れなかった）
```

heuristic の 2,715 箇所から 25 件を抜き取って読んだ: 22 件が正しい先（`page.next_cursor` → `Page.next_cursor`、`rec.resume` →
`Record.resume`、同じファイルの `e.existed` → `_Entry.existed` など）。誤り 3 件は `args.json`（argparse）→ `_Out.json`、`cp.returncode`
（subprocess）→ `LspClient.returncode`、`ws.workspace_id`（`Workspace`）→ 同じファイルの `_BoardJob.workspace_id`（同じファイルの段が勝つ。
BUG-0077 の規則のまま）。外部の型のメンバーと同名を結ばない規則（`external_name`）を入れる前は heuristic が 3,659 箇所で、上位は
tree-sitter の `node.type` → `SkelNode.type` 332・`node.parent` → `_Def.parent` 153 だった。

テスト（決定的。フィクスチャ `tests/fixtures/untyped_refs`、バイト列のまま `-text`。README に各箇所の意図）:

- `tests/unit/test_engine_indexer_untyped_refs.py`（11 件）: 点つきのモジュールのパスの呼び出し（`pkg.helpers.flush()`・`pkg.sub.deep.go()`・
  `ps.deep.go()`・名前空間パッケージの `nsp.tools.fmt.render()`）は `structural`、モジュールの名前の属性の連鎖（`conf.settings.reload()`・
  `conf.Settings.debug`）は `structural`、型の分かる import した変数（`settings = Settings()`・`typed: Settings = ...`・外部の型の
  `lock = Lock()`・TS の `export const meter = new Meter()`）は型のメンバーに `structural`、型の分からない `loose.reload()` と再び公開した
  `shared.reload()` は `_name_calls`、変数の定義の編集で import している側を取り込み直さずに結び直る（行き来の両方向）、型の分からない
  受け手の属性の読み書きは使用種別つきで `_name_calls`、`_attr_defs`（同じファイルの基底の属性・クラスのシンボルは入れない）、名前の判定
  （唯一のフィールド・唯一のインスタンス属性・曖昧・基底のインスタンス属性にまとめる・言語族をまたがない・組み込み型の属性と同名・
  外部の型のメンバーと同名・兄弟のモジュールは外部の証拠にしない・呼び出しの候補はメソッドだけ）、型の分からない受け手は `structural`
  にしない（BUG-0085 の後戻りなし）、v8 → v9 の移行。
- `tests/contract/test_T06_T07_untyped_refs.py`（12 件）: T-06 のモジュールの関数・`Settings.reload`（structural 3・heuristic 2）・
  `Settings.debug`・`Monitor.stdout_lines`（heuristic・`match: name`・`by name: 1`・compact の `[rw] … conf=heuristic`・`use_kinds` が効く）・
  曖昧な `Monitor.total`（`data.unattributed_refs` の候補にインスタンス属性）・`Gauge.hits`（サブクラスのインスタンス属性をまとめる）・
  組み込み型と同名の `Monitor.name`・外部の型と同名の `Monitor.parent`・TS の `Meter.hits`・`Base.save` / `Base.note`（structural に
  ならない）、T-07 の outgoing のモジュールの関数・incoming の `Settings.reload`・属性の読み書きは木に入らない。
- 既存のテストを直した: `tests/unit/test_syntax_relations.py`（呼び出しでない参照も `name_only`）、`tests/unit/test_name_calls.py`
  （属性の読み書きも名前だけ。使用種別つき）、`tests/unit/test_index_schema.py`（内部表と索引の許容リストに `_attr_defs`・`_var_types`）。
- `uv run ruff check`（引数なし）: クリーン。
- 関係・import・索引器・名前の呼び出し・継承・受け手・スキーマ・T-06 / T-07 の単体と契約（上の新しいテストを含む）: `362 passed, 6 skipped`（`test_T06_T07_lsp.py`・`test_T06_T07_lsp_postconditions.py`・TS の tsconfig の外の T-06 / T-07 を含む 27 ファイル。skip は TS の LSP が無い PC での exact の試験。`51c062d` に載せ直した後）。
- `tests/contract_fixtures`: `45 passed, 6 skipped`（skip は TS の LSP が無い PC での F-TS の exact の事後条件 5 件と、F-TS には当てはまらない BUG-0079 の試験 1 件）。F-PY の事後条件はすべて通過（`origin/main` の `51c062d` に載せ直した後）。
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の穴（見たことだけの票）: **BUG-0100**（`__init__.py` が再び公開した関数の `from pkg import flush` の `flush()`・`flush` の読みが `relations` にも `_name_calls` にも残らない）、**BUG-0101**（名前空間パッケージの `import nsp.tools.fmt` の `nsp`・`nsp.tools` の読みが外部 `lk://_ext/nsp`・`lk://_ext/nsp#tools` への `structural` の参照になる）。
