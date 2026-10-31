# BUG-0117 Python の受け手の注釈 `x: A | B`・`Union[A, B]` を `A` と読み、`x.run()` を `A.run` に `structural` で結ぶ

- 報告: 2026-10-31 / 見た人: BUG-0113・BUG-0114 を直す役（TS の受け手の注釈を先頭の名前に縮める箇所を探していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（小さなワークスペースを `Indexer` で in-process に索引。LSP なしの `structural`。BUG-0113・BUG-0114 の直しを載せたコード）

```
m.py
from typing import Union


class A:
    def run(self) -> int:
        return 1


class B:
    def run(self) -> int:
        return 2


def f(x: A | B, y: Union[A, B], z: "B | A") -> int:
    return x.run() + y.run() + z.run()
```

- `relations` に `m.py#f` → `m.py#A.run`（`CALLS`・`structural`）が 2 行ある（15 行 14 列の `x.run()`・24 列の `y.run()`）。`x`・`y` の型は
  `A | B` で、`B.run` かもしれない箇所が `A.run` の確かな行になっている。`B.run` への行は無い。
- `z: "B | A"`（前方参照の文字列）の `z.run()` は `_name_calls` の名前だけの行になる（`run`・15 行 34 列）。
- TS の同じ形（`w: Opts | Retry`）は BUG-0113 の直しで型の分からない受け手（名前だけ）になった。

## 利用者（呼ぶ側の AI）として困ること

- T-06 で `A.run` の参照を見ると、`B` の値でも呼ばれうる箇所が `structural`（確かな行）として混ざり、`B.run` の参照からは抜ける。

## 原因・直し方・確認（2026-10-31）

### 原因

- `syntax/relations._Extractor._py_ann_name` が Python の注釈を深さ優先でたどり、`_ANNOTATION_SKIP`（`Optional`・`Union`・`Final` など）に
  無い最初の名前を受け手の型にしていた（先頭の名前に縮める）。`A | B` も `Union[A, B]` も `ClassVar[A | B]` も `A`、`list[A]` は `list`
  （たまたま正しい）、`Callable[[A], B]` は `Callable`、`type[A]` は組み込みの `type`、`List[A]` は `typing.List`、点つきの `mod.Foo` は
  最後の名前 `Foo` を同じファイルの名前として引いていた。前方参照の文字列は中身が名前 1 つのときだけ読み、`"Optional[A]"`・`"B | A"` は
  読まなかった（票の `z` が名前だけになったのはこのため。直りではなく読めていなかっただけ）。
- 使っていたのはパラメータ（`typed_parameter`・`typed_default_parameter`）・代入（クラス直下の `x: T`・`self.x: T = ...`・モジュール直下の
  変数〈`_var_types`。BUG-0094〉）・戻り値の注釈つきの property の 3 か所（同じ関数）。代入は注釈から名前が取れなければ値 `Foo()` の型に
  落ちていたので、`u: A | B = A()` も `A` になった。`OA: TypeAlias = Optional[A]` は `TypeAlias` を型にして、`_var_types` に
  `lk://_ext/typing#TypeAlias` を書き、import した側の `c: OA` の `c.run()` を `lk://_ext/typing#TypeAlias.run` に結んでいた。

### 直し方（決めたこと）

- 注釈を `_PyAnn`（名前・`名前[引数]`・合併・`None`・読まない形）に読み、`_py_read` が 1 つの規則で受け手の型にする（`_py_ann_type`。
  木〈`binary_operator` の `|`・`generic_type`・`subscript`・`attribute`〉と前方参照の文字列〈小さな字句解析。入れ子の文字列も〉を同じ形に
  する。`_ANNOTATION_SKIP` は消した）:
  - `A | None`・`None | A`・`Optional[A]`・`Union[A, None]`（`"Optional[A]"`・`Optional["A"]` も）は `A`。`None` を除いて構成が 2 つ以上の
    合併（`A | B`・`Union[A, B]`・`"B | A"`・`Optional[A | B]`・`Union[A, Union[B, None]]`）は分からない（名前の一致）。TS の受け手の
    合併（BUG-0113 の `_ts_type`）と同じで、構成のどれか 1 つだけがメンバーを持つかは見ない（`_type_aliases` の `or` の「ちょうど 1 つ」の
    規則は TS の別名の構成だけ。Python の受け手でそれをするには構成を整えの段に渡す表が要り、票の誤りを消すのに要らないので入れない）。
  - 容器 `list[A]`・`dict[K, V]`・`Sequence[A]`・総称のクラス `Box[A]` は容器の型（要素の型にしない）。typing の古い綴り `List`・`Dict`・
    `Set`・`FrozenSet`・`Tuple`（素の `Type` も）は組み込みの型（`_TsType(builtin=True)` を Python でも使う。`lk://_ext/builtins#list`）。
  - `type[A]` / `Type[A]` / `typing.Type[A]` はクラス `A`（`t.make()` は `A.make`。`type[A | B]` は分からない）。`Annotated[A, ...]`・
    `Final[A]`・`ClassVar[A]`・`Required[A]`・`NotRequired[A]`・`ReadOnly[A]` は `A`。
  - 点つきの名前 `mod.Foo`・`pkg.mod.Foo` はいちばん長いワークスペースのモジュール（`resolve_dotted`。`import pkg.mod` の `pkg.mod`）の
    `Foo`、決まらなければ先頭の名前（`from pkg import models` の `models`・外部の `argparse`・同じファイルの `Outer.Inner`）からたどる
    （`_py_dotted_ref`・`_dotted_type_ref`）。
  - 同じファイルのモジュール直下の型の別名（PEP 695 の `type AB = A | B`・`OptA: TypeAlias = Optional[A]`）は値を同じ規則で読む（走査の前に
    控える。型引数つきの `type L[T] = ...` は控えない）。別ファイルの別名は索引に構成を書いていない（`_type_aliases` は TS だけ）ので
    受け手の型にしない（`AB` の URN の `.run` を整えが探して見つからず名前の一致になる。`TypeAlias.run` のような誤った外部のメンバーに
    結ばない）。
  - `Literal[...]`・`Callable[...]`・`Concatenate`・`TypeGuard`・`Unpack` なども分からない。`Any`・`Self`・素の `Final` / `ClassVar` /
    `TypeAlias` は型の手がかりにならない（代入の値 `x: Final = Foo()` の `Foo` に任せる。BUG-0071 の `Any` の決まりはそのまま）。それ以外の
    分からない注釈は値に任せない（`x: A | B = A()` は分からない。TS の「注釈があるときは注釈が決める」と同じ）。
  - typing の特別な形として読むのは、先頭の名前が同じファイルの定義でもワークスペースの import でもないとき（`class Literal` の
    `x: Literal` は同じファイルの `Literal`）。
- 型の分からない受け手を確かな行にしない決まり（BUG-0079・BUG-0085）はそのまま: 合併の受け手は `name_only`（`_name_calls`）になる。
  `A` に無いメンバー（`x.only_b()`）は、前は `A.only_b` を探して見つからず黙って落ちていたが、名前だけの行になる。
- schema v14（表は変えない。v13 からの移行で Python のファイルを取り込み直させる）。`tests/unit/test_index_core.py`（版 14）・
  `tests/unit/test_index_schema.py`（`_type_aliases` の注記）、続く v14 が Python を取り込み直すので `test_engine_indexer_reexports.py` の
  v10 → v11・v11 → v12・v12 → v13 の移行の試験の Python のファイルの期待を空にした。仕様書 T-06 処理規約 9 (a)・T-07 処理規約 11 に
  理由つきで書いた。理由: 誤った確かな行は取りこぼしより悪い（TS の BUG-0113 と同じ）。

### 確認

修正前（`66cd468` の `src`）と修正後のコードで、Lucianke 自身（`git archive` した同じ木）をそれぞれ新しい索引に in-process で索引
（LSP なしの `structural`）して比べた。

```
relations:   100,263 → 101,089（消えた箇所 22・新しい箇所 848・先が変わった箇所 0。すべて structural）
_name_calls:  24,611 →  23,781（増えた 22〈消えた 22 箇所がそのまま名前だけに〉・消えた 852）
_var_types:      245 →     245（weakref.WeakKeyDictionary[...] の _CAPTURES が増え、Callable[[str], int] の default_estimate が消えた）
```

- 消えた 22（どれも合併の注釈の最初の構成への誤った structural。今は名前だけ）の例:
  - `bench/run_edit_safety.py#run_op` の `env.lsp.check(...)`（`lsp: PyLsp | TsServer | None`）が `PyLsp.check` に結ばれていた（`TsServer.check`
    かもしれない）。
  - `src/lucianke/git/repo.py#_safe_tree_path` の `raw.decode()`・`raw.startswith()`・`raw.split()`（`raw: bytes | str`）が `builtins.bytes.*` に、
    `tests/unit/test_syntax_guards.py#parse` などの `source.encode()`（`source: bytes | str`）が `bytes.encode` に結ばれていた。
  - `src/lucianke/syntax/imports.py#namespace_name_map` の `resolve.namespace_dir(...)`（`resolve: Callable[[ImportFact], RelPath | None]`）が
    `collections.abc.Callable.namespace_dir` に結ばれていた（実物は `namespace_dir` を持つ解決器。今は名前の一致で本物に届く）。
  - ほかに `_regular_staged` の `entry.this` など（`entry: IndexEntry | ConflictedIndexEntry`。`isinstance` で絞った後でも合併のまま読む）、
    `core/urn.py#derivable` の `urn.workspace_id`（`urn: Urn | str`）、`DiagnosticsHub._spawn` の `Awaitable.close`。
- 新しい 848（点つきの注釈 `mod.Foo` を最後の名前で引いて決まらず名前だけだったもの）: `sqlite3.Connection.execute` 328
  （`conn: sqlite3.Connection`）、`pytest.*` 160（`monkeypatch: pytest.MonkeyPatch` など）、`asyncio.*` 121（`proc: asyncio.subprocess.Process` の
  `proc.stdin.write`）、`argparse.Namespace.*` 51、ワークスペースの型 49（`source: lx.CallEdgeSource` の `source.x` など）、`random`・`mcp`・
  `logging`・`queue`・`re`・`threading` ほか。
- 名前だけの行が `relations` にも入らず消えたのは 4（`t07_query_call_hierarchy.py#handler` の `source.from_lsp`・`source.skipped` 2・
  `source.warnings`。受け手が `lx.CallEdgeSource` と分かるようになり、インスタンス属性の読みになった）。これは直す前から
  `from x import CallEdgeSource` の注釈でも同じに落ちる別の不具合で、票 **BUG-0122** にした。
- 小さなワークスペース（票の `m.py` の形）: `x.run()`・`y.run()`・`z.run()` は名前だけ（`A.run` の行は無い）、`o: A | None`・`Optional[A]`・
  `"Optional[A]"` は `A.run`、`t: type[A]` の `t.make()` は `A.make`（前は `builtins.type.make`）、`List[A]` の `append` は
  `builtins.list.append`（前は `typing.List.append`）。

テスト（決定的）:

- `tests/unit/test_syntax_relations.py::test_python_receiver_annotations_are_read_precisely`（抽出器の段: 合併 5 形・`Optional` の 8 形・
  前方参照・容器 5 形・`type` 4 形・`Annotated`・点つき 2 形・同じファイルの別名 3 形・`class Literal`・`Callable`・読めない文字列、
  クラスの `Final[A]` / 素の `Final` / `ClassVar[A | B]` / `u: A | B = A()` / `Any`・property の戻り値の合併と `Optional`）。
- `tests/unit/test_engine_indexer_py_annotations.py`（索引器の段 4 件）: 合併の受け手は名前だけで `A` に無い `only_b` も名前だけ、
  `Optional`・前方参照・`type[A]`・`pkg.models.B` は structural、`_var_types` と別ファイルの別名、v13 → v14 の移行。
  どちらも `66cd468` の `src` では赤になることを確かめた。
- BUG-0085 の前方参照・BUG-0079 の属性の型・BUG-0094 の `_var_types` の既存の試験はそのまま通る。
- `uv run ruff check`（引数なし）: クリーン。
- 構文の関係・シンボル・索引器（`test_engine_indexer*`）・索引（`test_index_*`）・名前の呼び出し・T-02・T-03・T-04・T-06・T-07 の単体と契約:
  `origin/main` `7d9ab21`（BUG-0115・BUG-0116 を含む）の上で `772 passed, 7 skipped`（skip は TS の LSP が無い PC での exact の試験）。
- `tests/contract_fixtures`: `45 passed, 6 skipped`（F-PY の事後条件は満たす。skip は TS の LSP が無い PC での F-TS の exact など）。
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の不具合（見たことだけの票）: **BUG-0122**（型の分かる受け手の別ファイルのクラスのインスタンス属性の読み `a.skipped` が
  `relations` にも `_name_calls` にも残らない）。
