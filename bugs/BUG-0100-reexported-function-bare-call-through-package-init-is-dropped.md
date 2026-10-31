# BUG-0100 `__init__.py` が再び公開した関数を `from pkg import flush` で import した `flush()` が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0093・BUG-0094・BUG-0095 を直す役（import した変数の受け手を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0093〜0095 の直しを載せたコード）

```
pkg/__init__.py   from pkg.impl import flush\n\n__all__ = ["flush"]
pkg/impl.py       def flush():\n    return 0
use.py            from pkg import flush\n\n\ndef f():\n    flush()\n    return flush
```

- `use.py` の `relations` は 0 行（`flush()` の `CALLS` も、`return flush` の `REFERENCES` も、`IMPORTS` 以外は何も無い）。
- `use.py` の `_name_calls` も 0 行。
- `pkg/__init__.py#flush` という URN のシンボルは無い（import は `symbols` にならない）。
- 同じ形で Lucianke 自身の `EST = TokenEstimator()`（`tests/unit/test_mcp_budget.py`。`TokenEstimator` は `mcp/budget.py` が
  `core/tokens.py` から再び公開している）の `EST.count(...)` は、BUG-0094 の直しで名前だけの呼び出し（`builtin_name`）になったが、
  `core/tokens.py#TokenEstimator.count` への `structural` の行にはならない。

## 利用者（呼ぶ側の AI）として困ること

- `pkg/impl.py#flush` を変える前に T-06 / T-07 で呼び出し元を見ても、パッケージの `__init__.py` 経由で import した呼び出し元は
  `structural` でも `heuristic` でも出ず、警告も無い（パッケージの公開 API を `__init__.py` で再び公開するのはよくある書き方）。

## 原因・直し方・確認（2026-10-31）

BUG-0101 と一緒に直した（どちらも Python の import の解決の穴）。

### 原因

- 抽出器（`syntax/relations.py`）は `from pkg import flush` の `flush` を import 表で `pkg/__init__.py#flush` に解く。`pkg/__init__.py` は
  `flush` を定義しておらず（`from .helpers import flush` の import はシンボルにならない）、関係を整える段（`engine/indexer._settle_relations`）が
  「`symbols` に無い目標」として捨てていた。名前だけの呼び出しにもならない（抽出器の段では先が決まっていた）ので、どこにも残らなかった。
- 再び公開したクラスのメンバー（`TokenEstimator().count()` の `mcp/budget.py#TokenEstimator.count`・`EST = TokenEstimator()` の型
  `mcp/budget.py#TokenEstimator`）は、BUG-0094 の退避で「クラスの部分がシンボルでないが索引にあるファイル」として名前だけの呼び出しに
  落ちていた（`builtin_name` の判定で結ばれない）。基底（`class Args(CommonArgs)` の `INHERITS`）も同じく捨てられていた。

### 直し方（決めたこと）

- **束ねた名前を索引に残す**（内部表 `_reexports`、schema v10）: 取り込みが Python のファイルごとに、モジュール直下の import が束ねた
  名前とその先（`import`。ワークスペースのシンボル・モジュール・外部）、`from x import *` のワークスペースの先（`star`）、文字列の
  リテラルだけで書かれた `__all__`（`all`。`syntax/imports.module_all_names`）を書く。
- **整えで定義元までたどる**（`index/reexports.py`。`_settle_relations` の最初、`_member_refs` への退避の前）: 整えるファイルの `symbols` に
  無いワークスペースの目標（呼び出し・参照・`INHERITS`・型の使用・import の行すべて）と `_var_types` の型を書き換える。`P#A.rest` の
  `A` を `P` が束ねた先に置き換えて続け（別名・絶対 import・何段でも。普通のモジュールの再公開も）、束ねていなければ `P` の `*` の先を
  後ろから見て `A` を公開しているもの（`__all__` があれば載っている、無ければ `_` で始まらず定義・束ね・さらに `*` で持つ）。シンボル・
  外部・`P#A` がシンボル（残りは継承したメンバー・変数の型として続く退避で決まる）で止める。段の上限は 5（T-04 の `MAX_HOPS` と同じ）、
  循環なら元のまま（今までどおり捨てる）。たどれなければ今までどおり（素の名前は捨て、メンバーは BUG-0094 の名前だけに）。
  理由: パッケージは公開 API を `__init__.py` で再び公開するのが普通で、そこから import した呼び出し元が消えると、ライブラリの関数の
  実際の呼び出し元の大半が T-06 / T-07 から見えない。import 文の行も定義元へ（直接 import したときと同じ答え）。
- **編集に追従**（内部表 `_reexport_deps` と `engine/reresolve.py`）: たどるときに引いた `(ファイル, 名前)` を整えたファイルごとに覚える
  （2 段目からは着いた先がシンボルでも）。ファイルの取り込み・削除で「公開する名前」（最上位のシンボルの増減・束ねた名前の先・`*` の先・
  `__all__`。`reexports.export_changes`）が変わったら、その名前に依っていたファイルを同じトランザクションで取り込み直し、取り込み直した側の
  公開する名前も変われば続けて引く（5 段まで）。連鎖の途中のファイルもすべて覚えるので、`pkg/sub/__init__.py` の再公開の先を変えても
  `pkg` 経由の側が 1 回で結び直る。本文・コメントだけの編集では触れない。差分走査は `IngestStats.export_names_changed` で同じことをする。
- TS / JS は対象外（`export ... from` のバレルは票 BUG-0104）。import する側の `from x import *` も対象外（票 BUG-0105）。
- 仕様書: T-06 処理規約 9 (a)・T-07 処理規約 11。設計書: 内部表の一覧（v10）・§7.6.2（再び公開した名前・import 元の解き直し）。

### 確認（BUG-0100・BUG-0101 共通）

Lucianke 自身（`bc63ffa` のスナップショット 770 ファイルを、修正前のコード〈`bc63ffa` の `src`〉と修正後のコードで、それぞれ新しい索引に
in-process で索引。LSP なしの `structural`。ワークスペース ID `Lucianke`）:

```
relations（CALLS / REFERENCES / IMPORTS / INHERITS / TYPE_USES / IMPLEMENTS）: 93,507 → 93,746（増えた 242 行・消えた 3 行）
  増えた: CALLS 163・REFERENCES 42・IMPORTS 30・INHERITS 5・TYPE_USES 2（ワークスペースの先 234・外部の先 8）
    先のファイルの上位: core/compact_lines.py 131（mcp/render/compact.py が再び公開）・core/tokens.py 48（mcp/budget.py が
    TokenEstimator を再び公開）・tools/_base.py 20（CommonArgs。テストの Args(CommonArgs) の INHERITS 5 を含む）・core/hashing.py 8
    外部の 8: 再び公開したモジュールの属性（edit/writer.py の import time を通した writer.time.sleep → lk://_ext/time#sleep など）
  消えた 3: tests/fixtures/untyped_refs/use_modules.py の lk://_ext/nsp（IMPORTS 1・REFERENCES 2。BUG-0101。IMPORTS は
    nsp/tools/fmt.py に変わった）
_name_calls: 23,124 → 23,090（名前だけだった 34 箇所が同じ位置の structural の行になった。消えて名前だけになったものは 0）
  EST = TokenEstimator() の EST.count()（tests/unit/test_mcp_budget.py の 93・119・146・151・153 行ほか）・TokenEstimator().count()
  （bench/tokens/calibrate.py・bench/dogfood/run_dogfood.py）→ src/lucianke/core/tokens.py#TokenEstimator.count（structural）。
  _var_types の tests/unit/test_mcp_budget.py#EST の型は src/lucianke/core/tokens.py#TokenEstimator に書き換わった。
  ほかに Tier.T1・symbols_by_qualified_suffix（テストが再び公開した名前を通して使う）、use_conf.py の shared.reload()。
_reexports 6,333 行（import 5,741・all 592・star 0）・_reexport_deps 144 行（28 ファイル）
ワークスペースの packaging/（__init__.py の無いディレクトリ）と同名の外部の packaging への参照: 3 行のまま（外部のまま）
索引の大きさ: 49.6 MB → 50.4 MB。走査の時間: 29.4 秒 → 27.6〜30.4 秒（差は測れない）
```

テスト（決定的。フィクスチャ `tests/fixtures/reexports`、バイト列のまま `-text`。README に各箇所の意図）:

- `tests/unit/test_engine_indexer_reexports.py`（14 件）: 1 段・2 段・普通のモジュールの別名・絶対 import の別名の再公開は定義元の
  関数に `structural`（呼び出し・読み・import 文の行）、`from x import *`（`__all__` があれば載っている名前だけ・無ければ `_` で
  始まらない名前）、循環は止まって行を作らない、再び公開したクラスの構築・メソッド・`EST = TokenEstimator()` の `EST.count()`・基底の
  メソッド `EST.reset()`・`class Sub(TokenEstimator)` の `INHERITS`・外部の再公開 `Lock()`・`_var_types` の書き換え、`_reexports` と
  `_reexport_deps`（途中のファイルも覚える）、編集で結び直す（`__init__.py` の再公開の先を変える・戻す、2 段目の先を変える、再公開を
  消すと素の名前は消えメンバーは名前だけに戻る・戻すと結び直る、終わりのファイルのシンボルを消す・足す、`*` の先の `__all__` を変える、
  本文だけの編集では取り込み直さない）、名前空間パッケージの読みを外部にしない（`nsp`・`nsp.tools`・`nt`・`tools`）、名前空間
  パッケージのディレクトリを作る・消すと `?dir:` の語幹で import 元を解き直す、v9 → v10 の移行。
- `tests/contract/test_T06_T07_reexports.py`（4 件）: T-06 の `flush`（3 つの書き方の呼び出しと読みが `structural`・`heuristic` なし）・
  `deep_go`・`starred`（`hidden` は出ない）・`TokenEstimator.count`・`Base.reset`、T-07 の incoming・outgoing（`pkg/__init__.py` の
  無い先・循環・公開しない名前は出ない）、名前空間パッケージの `render`（3 つの書き方）と outgoing に外部の `nsp` が出ない。
- `tests/unit/test_syntax_imports.py`（足した 9 件）: `module_all_names`（リテラル・`+=`・`append`・`extend`・注釈つき・リテラルでない
  ものは `None`）、`ImportResolver.namespace_dir`・`namespace_name_map`・`dotted_module_path` の `NAMESPACE_PACKAGE`（`src` の根・
  標準ライブラリの名前・普通のパッケージの下の名前空間パッケージ・`packaging` は外部のまま・`is_dir` が無ければ見分けない）。
- 既存のテストを直した: `tests/unit/test_engine_indexer_untyped_refs.py`・`tests/contract/test_T06_T07_untyped_refs.py`（`shared.reload()`
  は名前だけ → `Settings.reload` に `structural`。T-06 の `by_name` 2 → 1）、`tests/unit/test_index_schema.py`（内部表と索引の許容リストに
  `_reexports`・`_reexport_deps`）、`tests/fixtures/untyped_refs/README.md`。
- `uv run ruff check`（引数なし）: クリーン。
- 関係・import・索引器・名前の呼び出し・継承・受け手・スキーマ・T-04・T-06・T-07 の単体と契約（上の新しいテストを含む 33 ファイル。
  `origin/main` の `5af9c49` に載せ直した後）: `471 passed, 6 skipped, 1 failed`（落ちた 1 件は `test_T04_lsp.py::test_fake_timeout_degrades_to_structural_and_skips_the_rest_of_the_call`。この変更の前の `bc63ffa` の `src` でも同じく落ちる。票 BUG-0103。skip は TS の LSP が無い PC での exact の試験）。
- `tests/contract_fixtures`: `45 passed, 6 skipped`（`5af9c49` に載せ直した後。skip は TS の LSP が無い PC での F-TS の exact の事後条件 5 件と、F-TS には
  当てはまらない BUG-0079 の試験 1 件）。
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の不具合（見たことだけの票）: **BUG-0103**（`test_T04_lsp.py` の LSP の時間切れの試験が、警告の期限 300 ms に対して 3000 ms を
  期待して `origin/main` で落ちる。BUG-0091 の `d2ac0fc` から）、**BUG-0104**（TS のバレル `export { flush } from './helpers'` を経由した
  呼び出しが消える）、**BUG-0105**（Python の import する側の `from helpers import *` の呼び出しが消える）。
