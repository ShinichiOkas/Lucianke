# BUG-0095 型の分からない受け手の属性の読み書き（`mon.stdout_lines += 1`）は、構文木でも名前の一致でも結ばれず、T-06 に何も書かれない

- 報告: 2026-10-31 / 見た人: BUG-0085 を直す役（直しの前後を比べていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（Lucianke 自身 `8c10a2a` のスナップショットを in-process に索引。LSP なしの `structural`）

- BUG-0085 の直し（型の分からない名前の受け手を同じファイルの唯一の同名に `structural` で結ばない）の前後で、`relations` の
  `REFERENCES` が 1,293 行消えた。どれも `_name_calls` には入らない（`_name_calls` に入ったのは `CALLS` の 288 行だけ）。
  BUG-0079（受け手が式のとき）でも同じ形で `REFERENCES` が 135 行消えている（BUG-0079 の票の確認）。
- 消えた行の例（直しの前の `relations`。受け手はどれも型の分からないローカル・引数）:

```
bench/_stdio.py:344  McpStdioClient._read_loop    -> bench/_stdio.py#Monitor.stdout_lines      mon.stdout_lines += 1
bench/_stdio.py:345  McpStdioClient._read_loop    -> bench/_stdio.py#Monitor.stdout_bytes      mon.stdout_bytes += len(line)
bench/_stdio.py:406  McpStdioClient._observe      -> bench/_stdio.py#Monitor.samples           mon.samples += 1
bench/_stdio.py:423  McpStdioClient.watch_report  -> bench/_stdio.py#Monitor.violation_count   stdout_ok = mon.violation_count == 0
```

- `RelationCandidate.name_only` は `CALLS` のときだけ立つ（`syntax/relations.py` の `_ref_candidate`）。T-06 の名前の判定
  （`tools/_name_calls.NameIndex.target_name`）は対象が `Method` のときだけ。

## 利用者（呼ぶ側の AI）として困ること

- フィールド・属性（`Monitor.stdout_lines` など）の参照を T-06 で見ると、型の分からない受け手を通した読み書きは出ず、件数も警告も
  無い（BUG-0071 の「黙って 0 件に見える」と同じ形。メソッドの呼び出しなら名前の一致が `conf=heuristic` で結ぶか、曖昧と書く）。

## 原因・直し方・確認（2026-10-31）

BUG-0093・BUG-0094 と一緒に直した。

### 原因

- 抽出器の `RelationCandidate.name_only` は `relation_kind == "CALLS"` のときだけ立っていた（`_ref_candidate`）。受け手の型が分からない
  属性の読み書き（`mon.stdout_lines += 1`・`x.note`）は先が決まらず、名前だけの行にもならないので、取り込みが捨てていた。BUG-0085 で
  型の分からない名前の受け手を同じファイルの唯一の同名に `structural` で結ばなくなり、それまで（たまたま）結ばれていた 1,293 行が
  黙って消えた。
- 名前の一致（`tools/_name_calls.NameIndex`）は対象が `Method` のときだけで、候補もメソッドだけだった。インスタンス属性
  （`self.samples = 0`）はシンボルにならないので、候補として数える手立ても無かった。

### 直し方（決めたこと）

- **属性の読み書きも名前の一致に任せる**: `name_only` を `REFERENCES`（`r` / `w` / `rw`）にも立て、`_name_calls` に使用種別の列
  `use_kind`（`call` / `r` / `w` / `rw`。schema v9）を足して同じ表に残す（別の表にしなかったのは、位置・囲み・ファイル単位の全置換・
  BUG-0094 の書き戻しがすべて同じだから。T-07 は `use_kind = 'call'` だけを読む）。
- **候補**（`NameIndex` の `ref` の集まり）: 同じ言語族の同名の **クラスのメンバー**（`Method`〈`x.save` を渡すのはメソッドの参照〉・
  `Field`〈クラス属性・dataclass のフィールド〉・`Property`・クラスの `Constant`）と **インスタンス属性**（新しい内部表 `_attr_defs`。
  抽出器がメソッドの中の `self.x = ...` / `cls.x = ...` / `this.x = ...` をクラス・名前ごとに 1 つ控える。クラス自身か同じファイルの基底に
  同名のシンボル・インスタンス属性があれば入れない。別ファイルの基底は問い合わせのときに C3 でたどり、基底が同名のシンボルか
  インスタンス属性を持てば基底のものと同じ属性として 1 つに数える）。段（同じファイル → 箇所を含むテストフィクスチャ → ワークスペース）・
  テストダブル・囲みのクラス自身の規則は呼び出しと同じ（BUG-0077）。
- **結ぶ**: 決めた段の候補がちょうど 1 つで、組み込み型の属性・メソッドの名前（`syntax/builtin_names.PYTHON_BUILTIN_ATTRIBUTES` /
  `JS_BUILTIN_ATTRIBUTES`。`name`・`args`・`path`・`value`・`length` など。Python は `__x__` も）でなく、**外部の型のメンバーとも同名でない**
  とき、`conf=heuristic`・`match: name` で T-06 の答えに足す（`use` は `r` / `w` / `rw`）。外部の型のメンバーは、索引にある
  `lk://_ext/<pkg>#<Class>.<名前>` を型の分かる受け手が使っているもの（`node: Node` の `node.parent` → `tree_sitter.Node.parent`。
  `index/queries.external_members_named`。`sys.path` を足して読む兄弟のモジュール〈`spikes` の `import rawclient`〉を import の解決が外部に
  したものは除く）。理由: 型の分からない受け手はその外部の型であることが多い。この規則を入れる前は、Lucianke 自身で名前で結んだ属性の
  読みの上位が tree-sitter の `node.type` → `SkelNode.type`（332 箇所）・`node.parent` → `_Def.parent`（153 箇所）だった。
- **結ばない**（候補が 2 つ以上・組み込み型と同名・外部の型と同名）: 対象が候補に入っている箇所だけ、件数・候補（インスタンス属性は
  `(instance attribute)` / json の `kind: "instance_attribute"`）・先頭 5 件の位置を `warnings` に 1 行ずつ、全体（候補と位置は先頭 20 件、
  件数は全部）を `data.unattributed_refs[]`（`reason: ambiguous | builtin_name | external_name | not_in_exact_answer`。`external_name` は
  `external_members[]` つき）に書く。位置を全部 json に載せないのは呼び出しの `unattributed_calls` と同じ理由（予算を本物の参照から
  奪わない。件数は必ず書くので黙って落ちない）。
- **T-06 の対象**: クラスの `Field` / `Property` / `Constant` は読み書き、`Method` は呼び出しと読み書き（メソッドの参照）。`exact` の答えには
  足さず、結べるのに LSP の答えに無い位置の件数と先頭を書く（呼び出しと同じ）。T-07 は呼び出しの木なので属性の読み書きを入れない。
- **スキーマ v9**: `_name_calls.use_kind`・`_attr_defs`・`_var_types`（BUG-0094）。v8 からの移行は列と表を足し（列が既にあれば足さない）、
  Python / TS / JS のファイルの `sha256` を空にして次の差分走査で取り込み直させる（属性の読み書き・インスタンス属性・変数の型は取り込み
  でしか作れない）。
- 仕様書: T-06 処理規約 7（属性の読み書き・候補・外部の型と同名・理由 1 行）と json の `unattributed_refs[]`、T-07 処理規約 9 に 1 文、
  §8.3 の `heuristic`。設計書: 内部表の一覧（v9）と §7.6.2。

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
