# BUG-0093 `import pkg.helpers` の `pkg.helpers.flush()`（ワークスペースのモジュールの関数）が名前だけの呼び出しになり、どこにも結ばれない

- 報告: 2026-10-31 / 見た人: BUG-0085 を直す役（受け手の決め方を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。`8c10a2a` のコードでも BUG-0085 の直しを載せたコードでも同じ）

```
pkg/__init__.py   （空）
pkg/helpers.py    def flush():\n    return 0
use.py            import pkg.helpers\n...\ndef f():\n    pkg.helpers.flush()
```

- `relations` に `use.py#f` → `pkg/helpers.py#flush` の `CALLS` 行は無い。
- `_name_calls` に `('lk://obs/use.py#f', 'flush', 'use.py', 6, 17)` が入る（受け手の型が分からない属性呼び出しの扱い）。
- その行の名前の判定（`tools/_name_calls.NameIndex.verdict_for`）は `None`（候補なし。`flush` という `Method` が無い）。
- 同じ関数を `from pkg import helpers` の `helpers.flush()` で呼ぶと `structural` の `pkg/helpers.py#flush` になる（BUG-0085 の単体テスト
  `test_module_receiver_calls_the_module_function` と同じ形）。外部の `import os` の `os.path.join(...)` は `lk://_ext/os#path.join` になる。

## 利用者（呼ぶ側の AI）として困ること

- `flush` を変える前に T-06 / T-07 で呼び出し元を見ても、`pkg.helpers.flush()` の形の呼び出し元は `structural` でも `heuristic` でも出ず、
  警告も無い（モジュールの関数の呼び出しが「型の分からない受け手のメソッド呼び出し」として扱われている）。

## 原因・直し方・確認（2026-10-31）

BUG-0094・BUG-0095 と一緒に直した（どれも構造の関係・名前の一致の層で黙って落ちていた）。

### 原因

- 抽出器（`syntax/relations.py`）の `_type_ref` は、受け手 `pkg.helpers`（属性）の型を、先頭の `pkg` の型（`import pkg.helpers` が束ねた
  `pkg/__init__.py` のモジュール）の属性 `helpers` として引き、ワークスペースのモジュールの属性は「別ファイルのクラス・モジュールの属性の型は
  分からない」として `None` にしていた。`import pkg.helpers` が `helpers` をサブモジュールとして読み込むことを見ていなかった。受け手の型が
  分からないので `pkg.helpers.flush()` は名前だけの呼び出し（`_name_calls`）になり、名前の一致（`tools/_name_calls.py`）の候補は
  `Method` だけなので、モジュールの関数 `flush` には決して結ばれなかった。
- `__init__.py` の無い名前空間パッケージ（`import ns.mod`）は、先頭の `ns` がワークスペースのファイルに解けず外部 `lk://_ext/ns` に
  なるので、`ns.mod.f()` は外部の `lk://_ext/ns#mod.f` に `structural` で結ばれていた（ワークスペースの関数なのに外部）。
- `pkg.helpers` の `helpers` の読みそのものは `pkg/__init__.py#helpers` という無い先になり、関係を整える段で消えていた。

### 直し方（決めたこと）

- **点つきのモジュールのパスをサブモジュールに解く**（新しい `syntax/imports.dotted_module_path`、索引器の `resolve_dotted`、抽出器の
  `_dotted_module`）: 受け手が識別子だけの連鎖で、先頭の名前がモジュール直下の import が束ねた名前（ローカル・関数の中の import・同じ
  ファイルの定義に隠されていない）なら、索引器に引く。先頭の名前がパッケージのファイル（`__init__.py(i)`）に束ねられている
  （`import pkg.x` の `pkg`・`import a.b as ab`・`from pkg import sub`）なら、そのディレクトリの下で残りを `import` と同じ順
  （`<mod>.py` → `<mod>/__init__.py` → `.pyi`）に探す。先頭が `import head.x` の `head` でワークスペースのファイルに解けない（名前空間
  パッケージ）なら、点つきのパスを絶対 import として解く（標準ライブラリの名前は解かない: `os.path` は外部のまま）。見つかればモジュールの
  受け手で、`pkg.helpers.flush()` は `pkg/helpers.py#flush`（`structural`）、`pkg.helpers` の `helpers` の読みはサブモジュールのファイルへの
  参照。調べたパスは `_import_probes` の語幹に残す（後からサブモジュールを作った・消したとき、import 元の解き直しで追従する）。
- **モジュールの名前の属性の連鎖**: `from pkg import conf` の `conf.Settings.make()`・`conf.settings.reload()` の受け手 `conf.Settings` は
  `conf.py#Settings`（クラスならそのメンバー、変数なら BUG-0094 の型、シンボルでない〈再び公開した名前〉なら名前だけ）。
- TS / JS は対象外（`import * as ns` の `ns.f()` は既にモジュールの関数。サブモジュールの概念が無い）。
- 仕様書: T-06 処理規約 9 (a) に点つきのモジュールのパスとモジュールの名前の属性の連鎖（理由 1 行）、T-07 処理規約 11 に 1 文。設計書: §7.6.2。

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
- `2aebfc1`（BUG-0091 の追加）に載せ直した後、同じ一式に `tests/contract/test_T07_exact_budget.py` を足した 28 ファイルで `369 passed, 6 skipped`（`0a3a505` として push）。
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の穴（見たことだけの票）: **BUG-0100**（`__init__.py` が再び公開した関数の `from pkg import flush` の `flush()`・`flush` の読みが `relations` にも `_name_calls` にも残らない）、**BUG-0101**（名前空間パッケージの `import nsp.tools.fmt` の `nsp`・`nsp.tools` の読みが外部 `lk://_ext/nsp`・`lk://_ext/nsp#tools` への `structural` の参照になる）。
