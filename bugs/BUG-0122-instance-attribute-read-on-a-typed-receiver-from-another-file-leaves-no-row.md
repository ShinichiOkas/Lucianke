# BUG-0122 型の分かる受け手の別ファイルのクラスのインスタンス属性の読み（`a.skipped`）が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0117 を直す役（Python の受け手の注釈の読み方を直した前後で Lucianke 自身を索引して比べていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0117 の直しの前と後のコードで同じ）

```
pkg/__init__.py   （空）
pkg/lx.py
class Src:
    def __init__(self) -> None:
        self.skipped = 0

    def go(self) -> int:
        return self.skipped

pkg/use.py
from pkg import lx
from pkg.lx import Src


def h(a: Src, b: lx.Src) -> int:
    return a.skipped + b.skipped + a.go()
```

- `relations` の `pkg/use.py#h` の行は `pkg/lx.py`（`lx` の import の参照）と `pkg/lx.py#Src.go`（`CALLS`・`structural`）だけ。
- `a.skipped`（6 行 12 列）と `b.skipped`（6 行 24 列）は `relations` にも `_name_calls` にも無い（`REFERENCES` の行も名前だけの行も無い）。
- Lucianke 自身では、BUG-0117 の直しで `source: lx.CallEdgeSource | None` の受け手の型が分かるようになった
  `src/lucianke/tools/t07_query_call_hierarchy.py#handler` の `source.from_lsp`（755 行）・`source.skipped`（830・831 行）・`source.warnings`
  （833 行）の 4 か所が、直す前の `_name_calls`（名前だけの行）から消え、`relations` にも入らなかった（`CallEdgeSource.skipped` などは
  `__init__` の中の `self.skipped = 0` のインスタンス属性で、シンボルではない）。

## 利用者（呼ぶ側の AI）として困ること

- T-06 でインスタンス属性 `skipped` の参照を探しても、型の分かる受け手からの読みが 1 件も出ない（型の分からない受け手の読みは
  BUG-0095 の名前の一致で出るのに、型が分かるほうが取りこぼす）。

## 原因・直し方・確認（2026-10-31）

### 原因

- 別ファイルのクラスの受け手（`a: Src` の `a.skipped`）: 抽出器（`syntax/relations._member_of`）は先を `pkg/lx.py#Src.skipped` にし、
  関係を整える段（`index/inherited.park_member_refs`）は、その先がシンボルに無く「クラスの部分」`Src` がワークスペースのクラスなので
  `_member_refs` に退避していた（BUG-0075）。結ぶ段（`_Resolver._lookup`）は C3 の線形化でメンバーの **シンボル** しか探さず、
  インスタンス属性（`self.skipped = 0`）はシンボルでない（`_attr_defs` の行だけ）ので、行は結べないまま `_member_refs` に残った。
  `relations` にも `_name_calls` にも出ず、結べない理由（`blocked_member_refs`）にも当たらないので、T-06 は件数も警告も出さなかった。
  BUG-0117 で受け手の注釈が読めるようになった `source: lx.CallEdgeSource | None` の `source.skipped` などが、名前だけの行から
  この「結べないまま」に移って消えた。
- 同じファイルのクラスの受け手（`self.skipped`・`s: Src` の `s.skipped`）: 抽出器はクラス自身と同じファイルの基底にその名前の
  シンボルが無いと先を `None` にし、受け手の型は分かっているので `name_only` にもせず、取り込みが捨てていた（票の外だが同じ根）。
- インスタンス属性はシンボルでないので、T-06 の対象にもできなかった（`resolve_target` はシンボルだけを引く）。

### 直し方（決めたこと）

- **結ぶ先はインスタンス属性の行**（`_attr_defs` の `<クラス>.<名前>`。BUG-0095 の名前の一致の候補 `(instance attribute)` と同じ URN）。
  シンボルにはしない（地図・検索・アウトラインの答えを変えないため。BUG-0120 / BUG-0121 の領域にも触れない）。`relations` の先が
  シンボルでない行になるので、整えの「無い先の行を消す」から外れるのは、退避行から結び直す行だけ（退避を二重にしない印つき）。
  インスタンス属性が消えたら（クラスのファイルの取り込みで無くなる・ファイルの削除）それを指す行も消す（`index/ingest.py`。消えた
  シンボルと同じ）。
- **整えで結ぶ**（`_Resolver._lookup`）: 線形化のどこにもメンバー（シンボル）が無ければ、先に当たって探すのをやめた基底（中身の分からない
  外部の基底・クラスでない基底）より前のクラスのうち、最初にその名前のインスタンス属性を持ち、基底のものにまとまらない（`merged`:
  線形化の基底が同名のシンボルかインスタンス属性を持てば基底のもの。名前の一致の候補と同じ規則）クラスのインスタンス属性に
  `structural` で結ぶ。メンバーは線形化のどこにあっても先（クラス属性 `limit: int = 3` があれば `self.limit = 4` を書いても `Conf.limit`）。
  `super().x` はクラスの属性しか引かないので結ばない。クラスのファイルの編集で、使う側を取り込み直さずに結び直る（既存の
  `_member_refs` の範囲。インスタンス属性を基底に移せば基底のものに、消せば行も消える）。
- **同じファイルのクラスも整えに回す**（`_member_of`）: クラス自身・同じファイルの基底にシンボルが無ければ `<クラス>.<名前>` を求めて
  整えに任せる（中身の分からない外部の基底に先に当たるときは、その前のクラスがインスタンス属性を持つときだけ。持たなければ今までどおり
  結ばない）。別のクラスの同名には結ばない（受け手のクラスの線形化だけを見る）。
- **どれでもない名前は残して書く**: メンバーもインスタンス属性も無い名前（`a.nope`・`base.skipped` で `Base` には無い）は結ばずに
  `_member_refs` に残す（名前の一致にも回さない: 受け手の型が分かっているので、別のクラスの同名に推定で結ぶと影響範囲を誤らせる）。
  受け手の型が対象のクラスの真の基底なら（`b: Base` の `b.skipped` で `skipped` は派生クラス `Src` の `self.skipped = 0`）、実体が
  `Src` なら対象に届くので、`blocked_member_refs` が `reason: base_receiver` で返し、T-06 が件数・先頭の位置を `warnings[]` と
  `data.unattributed_calls[]` に書く（BUG-0081 の `external_base` / `unknown_base` と同じ欄）。
- **T-06 の対象**: `resolve_target(..., instance_attrs=True)`（T-06 だけ）で、シンボルが見つからない `symbol_name`（`Src.skipped`、ファイルに
  1 つなら `skipped`）・`urn` をインスタンス属性に当てる（仮の `Field`・`modifiers: ["instance"]`、宣言の位置は最初の代入の名前）。参照は
  `relations` から、型の分からない受け手は名前の一致（BUG-0095）から。`include_declaration: false` なら最初の代入（`w` の行でもある）は出さない。
- **schema v15**: 表は変えない。同じファイルのクラスの受け手は取り込みでしか直らないので、Python / TS / JS のファイルの `sha256` を空にして
  次の差分走査で取り込み直させる。
- 仕様書: T-06 処理規約 9 (d)（理由つき）と json の `unattributed_calls[]` の `reason: base_receiver`。

### 確認

Lucianke 自身（`56e693d` のスナップショットを、修正前のコードと修正後のコードで、同じ手順で in-process に索引。LSP なしの `structural`）:

```
relations: 102,549 → 106,270（増えた 3,721 行・消えた 0 行・先が変わった 0 行）
  増えた行はすべてインスタンス属性への行: 同じファイルの REFERENCES 3,450・別ファイルの REFERENCES 209・同じファイルの CALLS 62
  （self.callback() のように保持した呼び出し可能を呼ぶもの）。use: r 2,639・w 952・rw 68・call 62
  宙に浮いた行（シンボルでもインスタンス属性でもない先）: 0
_name_calls: 24,127 → 24,127（名前の一致の行は変わらない）
_member_refs: 533 → 3,968（結べないまま: 381 → 95。うち super() 16）
_attr_defs: 784（変わらない）
索引の大きさ: 52.2 MB → 55.2 MB、走査 71.5 秒 → 74.0 秒（並行する他の役の負荷あり）
```

票の 4 箇所（`src/lucianke/tools/t07_query_call_hierarchy.py#handler`）はすべて `structural` で結ばれた:
`source.from_lsp`（755 行 23 列）→ `_lsp_exact.py#CallEdgeSource.from_lsp`、`source.skipped`（830 行 38 列・831 行 35 列）→
`CallEdgeSource.skipped`、`source.warnings`（833 行 32 列）→ `CallEdgeSource.warnings`（どれも `r`）。修正前は 4 箇所とも `_member_refs` に
結べないまま（`resolved_urn` が NULL）で、`relations` にも `_name_calls` にも無かった。

票の小さなワークスペース（`pkg/lx.py`・`pkg/use.py`）でも `a.skipped`（6 行 12 列）・`b.skipped`（6 行 24 列）が
`pkg/lx.py#Src.skipped`（`REFERENCES`・`structural`）になった。

テスト（決定的）: 新しい `tests/unit/test_engine_indexer_instance_attrs.py`（5 件。修正前の src では 4 件が赤）

- 別ファイルの受け手 `a: Src` / `b: lx.Src` の読み・書き（`a.skipped = 1`）・`+=`（`rw`）は `Src.skipped`、基底の `self.inherited` は C3 で
  `Base.inherited`（`structural`）。`_name_calls` には入らない。
- 同じファイルの `self.skipped`・`s.skipped` も同じ先。クラス属性 `Conf.limit` は `self.limit = 4` があってもシンボルが先（今までどおり）。
  メソッドの呼び出しも今までどおり。
- 知らない名前（`a.nope`・`base.skipped`）は先を作らず `_member_refs` に結べないまま残る（黙って捨てない）。
- T-06 は `Src.skipped` を `symbol_name` でも `urn` でも対象にでき、6 箇所を `structural` で返し（宣言は含めない、`include_declaration`
  なら宣言の行も）、`base.skipped` を `reason: base_receiver` で `unattributed_calls` と `warnings` に書く。
- `self.skipped = 0` を `Base` に移すと `use.py` を取り込み直さずに `Base.skipped` に結び直り、消すと行も消える。

既存の試験で直したもの: 同じファイルのクラスに無いメンバーの抽出器の期待（`R.pack`・`PaymentProcessor.currency` は自分のメンバーとして
求める。別のクラスの同名にはしない意図は同じ）、宙に浮いた行の判定（インスタンス属性を除く）、v13 → 現在の移行の TS / JS の期待
（続く v15 が取り込み直す）、版 15。`uv run ruff check` クリーン。
