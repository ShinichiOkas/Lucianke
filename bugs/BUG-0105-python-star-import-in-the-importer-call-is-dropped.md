# BUG-0105 Python の `from helpers import *` で取り込んだ `flush` の `flush()` が、`relations` にも `_name_calls` にも残らない

- 報告: 2026-10-31 / 見た人: BUG-0100・BUG-0101 を直す役（`from x import *` の再公開をたどる直しの範囲の外を確かめていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0100 の直しを載せたコード）

```
helpers.py    def flush():\n    return 0
use_star.py   from helpers import *\n\n\ndef run():\n    return flush()
```

- `use_star.py` の `relations` は 0 行（`flush()` の `CALLS` が無い）。`_name_calls` も 0 行。
- `syntax/imports.wildcard_sources` は `src/` のどこからも呼ばれていない（grep）。BUG-0100 の直しは、`__init__.py` などの **再公開する側**
  の `from .x import *` だけをたどる（`pkg/__init__.py` の `from .stars import *` を経由した `from pkg import starred` は結ばれる）。

## 利用者（呼ぶ側の AI）として困ること

- `helpers.py#flush` を変える前に T-06 / T-07 で呼び出し元を見ても、`from helpers import *` で使っている呼び出し元は出ず、警告も無い。

## 原因・直し方・確認（2026-10-31）

BUG-0104 と一緒に直した（確認は BUG-0104 の票に共通で書いた）。

### 原因

- 抽出器は素の名前を 同じファイル → import 表 → 組み込み名 の順に引く。`from helpers import *` は名前を列挙できないので import 表に入らず
  （`imported_name_map` が飛ばす）、`flush` はどこでも決まらず候補の目標が `None` になり、取り込みが捨てていた（名前だけの呼び出しは
  受け手のある属性呼び出しだけ）。`wildcard_sources` は BUG-0100 で再び公開する側の `star` の行にだけ使われ、import する側の名前には
  つながっていなかった。

### 直し方（決めたこと）

- **仮の目標**（`syntax/relations.resolve_star`、`engine/indexer._relations`）: ワークスペースに解ける `from x import *` を持つ Python の
  ファイルで、同じファイル・import 表・組み込み名のどれでも決まらなかった素の名前（と受け手・型・基底に使う名前）は `<このファイル>#name`
  にする（組み込み名・`__x__` は引かない。import 文の行には使わない）。
- **整えで定義元まで**（`index/reexports.py`。BUG-0100 と同じ段）: `<このファイル>#name` は `symbols` に無いので、整えがこのファイルの
  `*` の先を後ろから見て `name` を公開しているもの（`__all__` があれば載っている名前、無ければ `_` で始まらず定義・束ね・さらに `*` で
  持つ名前）に書き換え、さらに再び公開した名前をたどる（`from pkg import *` の `flush` → `pkg/__init__.py` → `pkg/helpers.py#flush`）。
  どの `*` の先も公開していなければ、素の名前は今までどおり捨て、受け手の名前（`nowhere.attr()`）は名前だけ（`_name_calls`）にし、
  変数の型（`ITEM = Unknown()`）は消す（型が分からないのと同じ）。
- **リテラルでない `__all__`**（`syntax/imports.module_all_dynamic`、`_reexports` の `dynamic_all` の印）: その `*` の先が公開する名前は
  構文から決まらないので、公開名の規則で見つけた定義に `heuristic` で結ぶ（名前だけで決めた。`ReexportResolver.resolve_detail`。
  再び公開する側の `*` も同じ）。どの `*` の先にも無い名前は結ばない（ワークスペースの同名の関数に名前で当てると、名前の一致の候補が
  メソッドなので素の関数の呼び出しを別物に結びうる）。
- **編集に追従**: たどるときに引いた `(*の先, 名前)` を `_reexport_deps` に覚え（見つからなかった名前も）、`*` の先の公開する名前が変わったら
  import する側を取り込み直す（`*` の先に関数を足すと結ばれ、消すと消える）。`__all__` がリテラルかどうかの変化も `export_changes` が引く。
  自分のファイルの `(パス, 名前)` は覚えない（自分の編集ではいつも取り込み直す）。
- **schema v11**: v10 からの移行で `from x import *` を持つ Python のファイルと、`__all__` を書くがリテラルの名前の無い Python のファイルを
  取り込み直させる（ほかの Python のファイルは変わらない）。
- 範囲外: `*` の先が組み込み名を上書きする（`*` の先が `open` を定義する）ときは組み込み名のまま。外部の `from os.path import *` の名前は
  今までどおり（外部の `*` の先は列挙できない）。
- 仕様書 T-06 処理規約 9 (a)・T-07 処理規約 11、設計書 §7.6.2。理由: `*` で使っている呼び出し元が黙って消えるより、決まる範囲は確かに、
  決まらない範囲は水準（`heuristic`）で知らせて結ぶ方が、編集の影響を見落とさない。
