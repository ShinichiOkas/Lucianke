"""``syntax/symbols.py``（シンボル抽出と URN の付与。設計書 §7.4.3）の単体テスト。

フィクスチャ F-POLY（``tests/fixtures/poly``）を、``tree_sitter.Parser`` で直接解析して使う（登録表・キャッシュに依存しない）。
位置は 0 始まりの行・コードポイント列。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

from lucianke.core.model import Confidence, Kind, Position, Range, RelPath, SymbolRecord
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import CodeSel, parse
from lucianke.syntax.symbols import Extraction, extract, extract_detailed, load_query_source
from lucianke.syntax.types import ParsedFile

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS = "poly"

_LANGUAGES: dict[str, Callable[[], object]] = {
    "python": tree_sitter_python.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "javascript": tree_sitter_javascript.language,
}


def _language(language_id: str) -> Language:
    return Language(_LANGUAGES[language_id]())  # type: ignore[arg-type]


def _parsed(language_id: str, source: bytes) -> ParsedFile:
    tree = Parser(_language(language_id)).parse(source)
    return ParsedFile(
        language_id=language_id,
        sha256="0" * 64,
        source=source,
        tree=tree,
        line_index=LineIndex(source),
        has_errors=tree.root_node.has_error,
    )


def _extract_source(language_id: str, source: bytes, rel: str) -> Extraction:
    return extract_detailed(
        _parsed(language_id, source),
        language_id=language_id,
        language=_language(language_id),
        query_source=load_query_source(language_id),
        workspace_id=WS,
        rel=RelPath(rel),
    )


def _extract_fixture(language_id: str, rel: str) -> Extraction:
    return _extract_source(language_id, (POLY / rel).read_bytes(), rel)


def _by_qualified(result: Extraction) -> dict[str, list[SymbolRecord]]:
    out: dict[str, list[SymbolRecord]] = {}
    for s in result.symbols:
        out.setdefault(s.record.qualified_name, []).append(s.record)
    return out


def _one(result: Extraction, qualified: str) -> SymbolRecord:
    found = _by_qualified(result)[qualified]
    assert len(found) == 1, [str(r.urn) for r in found]
    return found[0]


def _rng(sl: int, sc: int, el: int, ec: int) -> Range:
    return Range(Position(sl, sc), Position(el, ec))


def _selector(record: SymbolRecord) -> CodeSel:
    sel = record.urn.selector
    assert isinstance(sel, CodeSel)
    return sel


@pytest.fixture(scope="module")
def payment() -> Extraction:
    return _extract_fixture("python", "src/shop/services/payment.py")


# --- Python: payment.py ------------------------------------------------------------------


def test_payment_py_definitions_and_ranges(payment: Extraction) -> None:
    names = [s.record.qualified_name for s in payment.symbols]
    assert names == [
        "transactional",
        "PaymentRequest",
        "PaymentRequest.order_id",
        "PaymentRequest.amount",
        "PaymentRequest.currency",
        "PaymentDeclined",
        "PaymentProcessor",
        "PaymentProcessor.__init__",
        "PaymentProcessor.process_charge",
        "PaymentProcessor._validate_limit",
        "PaymentProcessor.apply_discount",
    ]
    # デコレータ付き定義は decorated_definition 全体（@dataclass の行から）が range
    req = _one(payment, "PaymentRequest")
    assert (req.kind, req.range) == (Kind.CLASS, _rng(17, 0, 23, 25))
    assert req.selection == _rng(18, 6, 18, 20)
    assert str(req.urn) == "lk://poly/src/shop/services/payment.py#PaymentRequest"
    assert req.doc_summary == "決済要求。amount > 0 が必須。"
    assert req.path == "src/shop/services/payment.py"
    assert req.confidence is Confidence.STRUCTURAL
    assert req.declared_in == "source"

    proc = _one(payment, "PaymentProcessor")
    assert (proc.kind, proc.range) == (Kind.CLASS, _rng(30, 0, 63, 59))
    assert proc.doc_summary == "注文の決済を担う。"
    assert proc.signature is None

    fn = _one(payment, "transactional")
    assert (fn.kind, fn.range, fn.modifiers) == (Kind.FUNCTION, _rng(12, 0, 14, 13), ("pub",))
    assert fn.signature == "(fn)"
    assert fn.doc_summary == "トランザクション境界を示す印（何もしない）。"


def test_payment_py_constructor_method_and_private(payment: Extraction) -> None:
    ctor = _one(payment, "PaymentProcessor.__init__")
    assert ctor.kind is Kind.CONSTRUCTOR
    assert ctor.range == _rng(33, 4, 35, 29)
    assert ctor.selection == _rng(33, 8, 33, 16)
    assert ctor.signature == "(self, limits: LimitTable) -> None"
    assert ctor.return_type == "None"

    charge = _one(payment, "PaymentProcessor.process_charge")
    assert charge.kind is Kind.METHOD
    assert charge.modifiers == ("pub", "async")
    # @transactional の行から本体の最終行まで
    assert charge.range == _rng(37, 4, 51, 64)
    assert charge.selection == _rng(38, 14, 38, 28)
    assert charge.signature == "(self, req: PaymentRequest) -> Receipt"
    assert charge.return_type == "Receipt"
    assert charge.doc_summary == "注文に対する決済を実行し領収書を発行する。"
    assert str(charge.urn) == "lk://poly/src/shop/services/payment.py#PaymentProcessor.process_charge"

    private = _one(payment, "PaymentProcessor._validate_limit")
    assert private.kind is Kind.METHOD
    assert private.modifiers == ("priv",)
    assert private.range == _rng(53, 4, 58, 30)
    assert private.signature == "(self, amount: Decimal) -> bool"
    assert private.doc_summary is None

    # インスタンス属性（self.currency = ...）とローカル変数（limit = ...）はシンボルにしない
    assert "PaymentProcessor.currency" not in _by_qualified(payment)
    assert "limit" not in _by_qualified(payment)


def test_payment_py_dataclass_fields(payment: Extraction) -> None:
    field = _one(payment, "PaymentRequest.currency")
    assert field.kind is Kind.FIELD
    assert field.range == _rng(23, 4, 23, 25)
    assert field.selection == _rng(23, 4, 23, 12)


def test_no_arity_or_sighash_without_overloads(payment: Extraction) -> None:
    for s in payment.symbols:
        sel = _selector(s.record)
        assert sel.arity is None and sel.sighash is None
        assert "/" not in str(s.record.urn).split("#", 1)[1]
        assert "~" not in str(s.record.urn).split("#", 1)[1]
    assert payment.warnings == ()


# --- Python: receipt.py（再定義・非 ASCII）--------------------------------------------------


def test_redefinition_in_branches_last_wins() -> None:
    result = _extract_fixture("python", "src/shop/models/receipt.py")
    defs = _by_qualified(result)["format_receipt"]
    assert len(defs) == 2
    first, last = sorted(defs, key=lambda r: r.range.start)
    # 最後の定義が素の URN、先の定義は ~sighash
    assert str(last.urn) == "lk://poly/src/shop/models/receipt.py#format_receipt"
    assert _selector(first).sighash is not None and _selector(first).arity is None
    assert str(first.urn).startswith("lk://poly/src/shop/models/receipt.py#format_receipt~")
    assert len(_selector(first).sighash or "") == 8
    assert first.range.start.line < last.range.start.line
    assert first.signature == last.signature == "(r: Receipt) -> str"
    assert len(result.warnings) == 1 and "format_receipt" in result.warnings[0]


def test_non_ascii_identifier_range_and_selection_are_codepoints() -> None:
    result = _extract_fixture("python", "src/shop/models/receipt.py")
    total = _one(result, "合計金額")
    assert total.kind is Kind.FUNCTION
    # def 合計金額(...): は 23 行目（0 始まり）。名前は "def " の直後の 4 コードポイント
    assert total.selection == _rng(23, 4, 23, 8)
    assert total.range.start == Position(23, 0)
    assert total.range.end == Position(28, 16)  # "    return total" の行末
    assert total.signature == "(receipts: list[Receipt]) -> Decimal"
    assert total.doc_summary == "非 ASCII 識別子のフィクスチャ。👍 を含む docstring。"
    # URN は非 ASCII を読めるままにする（urn.py の規約）
    assert str(total.urn) == "lk://poly/src/shop/models/receipt.py#合計金額"
    assert parse(str(total.urn)) == total.urn


def test_columns_after_emoji_on_the_same_line() -> None:
    # バイト列では名前の列が 4 バイトの絵文字ぶんずれるが、Range はコードポイント
    source = 'emoji = "😀"; 値段 = 1\n'.encode()
    result = _extract_source("python", source, "emoji.py")
    value = _one(result, "値段")
    assert value.selection == _rng(0, 13, 0, 15)
    assert value.range == _rng(0, 13, 0, 19)  # 2 つ目の文 "値段 = 1" だけ
    assert value.kind is Kind.VARIABLE


# --- Python: limits.py（相互再帰）、CRLF・BOM、末尾改行なし --------------------------------


def test_mutual_recursion_functions() -> None:
    result = _extract_fixture("python", "src/shop/services/limits.py")
    retry = _one(result, "retry")
    schedule = _one(result, "schedule")
    assert (retry.kind, schedule.kind) == (Kind.FUNCTION, Kind.FUNCTION)
    assert str(retry.urn) == "lk://poly/src/shop/services/limits.py#retry"
    assert str(schedule.urn) == "lk://poly/src/shop/services/limits.py#schedule"
    assert retry.range == _rng(19, 0, 23, 32)
    assert retry.selection == _rng(19, 4, 19, 9)
    assert schedule.range == _rng(26, 0, 27, 25)
    assert retry.signature == "(attempt: int) -> int"
    assert retry.doc_summary == "schedule と互いに呼び合う（循環呼び出しのフィクスチャ）。"
    assert _one(result, "LimitTable.__init__").kind is Kind.CONSTRUCTOR


def test_crlf_and_bom_ranges_do_not_shift() -> None:
    result = _extract_fixture("python", "src/shop/models/crlf_bom.py")
    func = _one(result, "crlf_function")
    assert func.range == _rng(3, 0, 5, 16)
    assert func.selection == _rng(3, 4, 3, 17)
    assert func.doc_summary == "Doubles x."
    cls = _one(result, "CrlfClass")
    assert cls.range == _rng(8, 0, 10, 21)
    method = _one(result, "CrlfClass.method")
    assert (method.kind, method.range) == (Kind.METHOD, _rng(9, 4, 10, 21))
    assert method.selection == _rng(9, 8, 9, 14)


def test_bom_on_first_line_does_not_shift_columns() -> None:
    source = b"\xef\xbb\xbfx = 1\r\ndef f(): pass\r\n"
    result = _extract_source("python", source, "bom.py")
    assert _one(result, "x").range == _rng(0, 0, 0, 5)
    assert _one(result, "x").selection == _rng(0, 0, 0, 1)
    assert _one(result, "f").range == _rng(1, 0, 1, 13)


def test_lone_cr_line_separator_follows_line_index() -> None:
    # Tree-sitter は \r を行区切りにしないが、Range は LineIndex（LSP と同じ）の行になる
    result = _extract_source("python", b"a = 1\rdef f(): pass\r", "cr.py")
    assert _one(result, "f").range == _rng(1, 0, 1, 13)


def test_no_final_newline() -> None:
    result = _extract_fixture("python", "src/shop/models/no_final_newline.py")
    func = _one(result, "no_newline_at_end")
    assert func.range == _rng(0, 0, 1, 20)


# --- Python: 入れ子・修飾子・種別 -----------------------------------------------------------

_PY_NESTING = b"""\
import os
LIMIT: int = 1
type Alias = int
class Outer(Base):
    x: int
    @property
    def p(self) -> int: ...
    @p.setter
    def p(self, v: int) -> None: ...
    @staticmethod
    async def s(): ...
    @abc.abstractmethod
    def a(self): ...
    class Inner:
        def m(self): ...
def f(a, *args, b=1, **kw) -> int:
    local = 1
    def g(): ...
    class K: ...
    return lambda: 1
if os.name == "nt":
    z = 1
def _hidden(): ...
def __dunder__(): ...
"""


def test_python_nesting_kinds_and_modifiers() -> None:
    result = _extract_source("python", _PY_NESTING, "n.py")
    got = {s.record.qualified_name: (s.record.kind, s.record.modifiers) for s in result.symbols}
    assert got == {
        "LIMIT": (Kind.CONSTANT, ("pub",)),
        "Alias": (Kind.TYPE, ("pub",)),
        "Outer": (Kind.CLASS, ("pub",)),
        "Outer.x": (Kind.FIELD, ("pub",)),
        "Outer.p": (Kind.PROPERTY, ("pub",)),  # @p.setter は別のシンボルにしない
        "Outer.s": (Kind.METHOD, ("pub", "static", "async")),
        "Outer.a": (Kind.METHOD, ("pub", "abstract")),
        "Outer.Inner": (Kind.CLASS, ("pub",)),
        "Outer.Inner.m": (Kind.METHOD, ("pub",)),
        "f": (Kind.FUNCTION, ("pub",)),
        "f.g": (Kind.FUNCTION, ("pub",)),
        "f.K": (Kind.CLASS, ("pub",)),
        "z": (Kind.VARIABLE, ("pub",)),
        "_hidden": (Kind.FUNCTION, ("priv",)),
        "__dunder__": (Kind.FUNCTION, ("pub",)),
    }
    assert _one(result, "f").signature == "(a, *args, b=1, **kw) -> int"
    assert "local" not in _by_qualified(result)  # ローカル変数


def test_stub_file_is_declared_as_stub() -> None:
    result = _extract_source("python", b"def f(x: int) -> int: ...\n", "pkg/mod.pyi")
    assert [s.record.declared_in for s in result.symbols] == ["stub"]


def test_multiline_signature_is_collapsed() -> None:
    source = b"def f(\n    a: int,\n    b: str = 'x',\n) -> None:\n    pass\n"
    result = _extract_source("python", source, "m.py")
    assert _one(result, "f").signature == "(a: int, b: str = 'x') -> None"


# --- TypeScript: payment.ts ----------------------------------------------------------------


@pytest.fixture(scope="module")
def ts_payment() -> Extraction:
    return _extract_fixture("typescript", "web/src/payment.ts")


def test_ts_interface_class_constructor(ts_payment: Extraction) -> None:
    iface = _one(ts_payment, "PaymentRequest")
    assert iface.kind is Kind.INTERFACE
    assert iface.modifiers == ("pub", "export")
    assert iface.range == _rng(0, 0, 4, 1)  # export interface ... { ... }
    assert iface.selection == _rng(0, 17, 0, 31)
    assert str(iface.urn) == "lk://poly/web/src/payment.ts#PaymentRequest"
    assert _one(ts_payment, "PaymentRequest.amount").kind is Kind.PROPERTY

    cls = _one(ts_payment, "PaymentClient")
    assert (cls.kind, cls.modifiers, cls.range) == (Kind.CLASS, ("pub", "export"), _rng(6, 0, 18, 1))

    ctor = _one(ts_payment, "PaymentClient.constructor")
    assert ctor.kind is Kind.CONSTRUCTOR
    assert ctor.range == _rng(7, 2, 7, 50)


def test_ts_overloads_get_distinct_urns(ts_payment: Extraction) -> None:
    charges = sorted(_by_qualified(ts_payment)["PaymentClient.charge"], key=lambda r: r.range.start)
    assert len(charges) == 3  # 宣言 2 つ + 実装 1 つ
    urns = [str(r.urn) for r in charges]
    assert len(set(urns)) == 3
    base = "lk://poly/web/src/payment.ts#PaymentClient.charge"
    # アリティで区別がつくもの（1 引数）は /1 だけ。同じ 2 引数の宣言と実装は ~sighash でさらに区別する
    assert urns[0] == f"{base}/1"
    assert urns[1].startswith(f"{base}/2~") and urns[2].startswith(f"{base}/2~")
    assert [r.signature for r in charges] == [
        "(req: PaymentRequest): Promise<string>",
        "(req: PaymentRequest, retries: number): Promise<string>",
        "(req: PaymentRequest, retries = 0): Promise<string>",
    ]
    assert charges[0].return_type == "Promise<string>"
    assert [r.modifiers for r in charges] == [("pub",), ("pub",), ("pub", "async")]
    # 実装だけ JSDoc の直後にある
    assert [r.doc_summary for r in charges] == [None, None, "決済を要求する。amount は 0 より大きいこと。"]
    assert charges[2].range == _rng(12, 2, 17, 3)
    assert charges[2].selection == _rng(12, 8, 12, 14)
    # 宣言とは別の定義として `~` を持つが、警告にはならない（シグネチャが異なる）
    assert ts_payment.warnings == ()


def test_ts_app_file(tmp_path: Path) -> None:
    result = _extract_fixture("typescript", "web/src/app.ts")
    checkout = _one(result, "checkout")
    assert (checkout.kind, checkout.modifiers) == (Kind.FUNCTION, ("pub", "async", "export"))
    assert checkout.signature == "(orderId: string): Promise<string>"
    client = _one(result, "client")
    assert (client.kind, client.modifiers) == (Kind.VARIABLE, ("priv",))  # import があるモジュールの非 export


_TS_SAMPLE = b"""\
export type T<X> = { a: X };
export enum E { A, B }
declare enum DE { A }
namespace NS { export function inner() {} export const K = 1; }
export abstract class AC<T> extends Base {
  private x: number = 1;
  static readonly Y = 2;
  protected abstract foo(a: number): string;
  #priv = 3;
  get v(): number { return 1 }
  set v(n: number) {}
  constructor(private a: string) { super(); }
  arrow = () => 1;
}
export function over(a: string): string;
export function over(a: number): number;
export function over(a: any): any { return a; }
const FOO = 1;
export const fn = async (x: number): Promise<void> => { const inner = 1; };
const o = { m() {}, n: () => 1 };
export interface I2 { m(a: number): void; readonly p: string; }
function outer() { function innerf() {} const local = 1; }
"""


def test_ts_kinds_modifiers_and_scopes() -> None:
    result = _extract_source("typescript", _TS_SAMPLE, "s.ts")
    got = {s.record.qualified_name: (s.record.kind, s.record.modifiers) for s in result.symbols}
    assert got["T"] == (Kind.TYPE, ("pub", "export"))
    assert got["E"] == (Kind.ENUM, ("pub", "export"))
    assert got["DE"] == (Kind.ENUM, ("priv",))
    assert got["NS"] == (Kind.MODULE, ("priv",))
    assert got["NS.inner"] == (Kind.FUNCTION, ("pub", "export"))
    assert got["NS.K"] == (Kind.CONSTANT, ("pub", "export"))
    assert got["AC"] == (Kind.CLASS, ("pub", "abstract", "export"))
    assert got["AC.x"] == (Kind.FIELD, ("priv",))
    assert got["AC.Y"] == (Kind.CONSTANT, ("pub", "static"))
    assert got["AC.foo"] == (Kind.METHOD, ("prot", "abstract"))
    assert got["AC.#priv"] == (Kind.FIELD, ("priv",))
    assert got["AC.v"] == (Kind.PROPERTY, ("pub",))  # get / set は 1 つ
    assert got["AC.constructor"] == (Kind.CONSTRUCTOR, ("pub",))
    assert got["AC.arrow"] == (Kind.METHOD, ("pub",))
    assert got["FOO"] == (Kind.CONSTANT, ("priv",))
    assert got["fn"] == (Kind.FUNCTION, ("pub", "async", "export"))
    assert got["I2.m"] == (Kind.METHOD, ("pub",))
    assert got["I2.p"] == (Kind.PROPERTY, ("pub",))
    assert got["outer.innerf"] == (Kind.FUNCTION, ("priv",))
    # ローカル変数・オブジェクトリテラルのメンバーは拾わない
    for hidden in ("fn.inner", "inner", "outer.local", "local", "o.m", "o.n", "m", "n"):
        assert hidden not in got
    assert len(_by_qualified(result)["over"]) == 3
    assert {str(r.urn) for r in _by_qualified(result)["over"]} == {
        f"lk://poly/s.ts#over~{_selector(r).sighash}" for r in _by_qualified(result)["over"]
    }


def test_ts_ranges_include_export_and_declare() -> None:
    source = b"export declare function a(): void;\nexport namespace N { export const x = 1 }\n"
    result = _extract_source("typescript", source, "d.ts")
    assert _one(result, "a").range == _rng(0, 0, 0, 34)
    assert _one(result, "N").range == _rng(1, 0, 1, 41)
    assert _one(result, "N.x").range == _rng(1, 21, 1, 39)
    assert _one(result, "N.x").modifiers == ("pub", "export")


_TS_TYPE_MEMBERS = b"""\
/** Options. */
export type KyOptions = {
  /** Retry. */
  retry?: Retry | number;
  inner: { a: number; b?: { c: string } };
  fn: (o: { notMe: number }) => void;
  list: { el: number }[];
  box: Promise<{ wrapped: number }>;
  tick(x: number): void;
};
export type U = { kind: 'a'; x: number } | { kind: 'b'; y: string };
export type I = Partial<{ p: number }> & ({ q: string });
export interface Face { nested: { deep: string; m(): void } }
export class C { opts: { z: number } = { z: 1 }; get view(): { w: number } { return { w: 1 }; } }
function f(o: { local: number }): { ret: number } { return { ret: o.local }; }
"""


def test_ts_type_literal_members_are_symbols() -> None:
    """BUG-0114: 型の別名・メンバーの型注釈に書いた型リテラルのメンバーは Property（メソッドのシグネチャは Method）で、修飾名は持ち主の下
    （入れ子は ``KyOptions.inner.b.c``）、修飾子は ``pub``。交差・括弧・``Partial<{...}>`` の第 1 型引数・クラスのフィールド・getter の
    戻り値・インターフェースの入れ子も同じ。関数型の引数・配列の要素・ほかの総称の型引数・関数の引数と戻り値の型リテラルは拾わない。
    合併の別々の型リテラルの同名のメンバーは衝突の規則で最後が素の URN（再定義の警告は出さない）。"""
    result = _extract_source("typescript", _TS_TYPE_MEMBERS, "t.ts")
    got = {s.record.qualified_name: (s.record.kind, s.record.modifiers) for s in result.symbols}
    prop, method = (Kind.PROPERTY, ("pub",)), (Kind.METHOD, ("pub",))
    for name in (
        "KyOptions.retry",
        "KyOptions.inner",
        "KyOptions.inner.a",
        "KyOptions.inner.b",
        "KyOptions.inner.b.c",
        "KyOptions.fn",
        "KyOptions.list",
        "KyOptions.box",
        "U.x",
        "U.y",
        "I.p",
        "I.q",
        "Face.nested",
        "Face.nested.deep",
        "C.opts.z",
        "C.view.w",
    ):
        assert got[name] == prop, name
    assert got["KyOptions.tick"] == method
    assert got["Face.nested.m"] == method
    for hidden in ("KyOptions.fn.notMe", "KyOptions.list.el", "KyOptions.box.wrapped", "f.local", "f.ret", "notMe", "el"):
        assert hidden not in got, hidden
    retry = _one(result, "KyOptions.retry")
    assert retry.doc_summary == "Retry."
    assert str(retry.urn) == "lk://poly/t.ts#KyOptions.retry"
    assert _one(result, "KyOptions.tick").signature == "(x: number): void"
    kinds = sorted(str(r.urn) for r in _by_qualified(result)["U.kind"])
    assert kinds[0] == "lk://poly/t.ts#U.kind"
    assert len(kinds) == 2 and kinds[1].startswith("lk://poly/t.ts#U.kind~")
    assert result.warnings == ()


def test_tsx_uses_the_typescript_queries() -> None:
    source = b'export function App(): JSX.Element { return <div id="x"/>; }\n'
    result = _extract_source("tsx", source, "App.tsx")
    app = _one(result, "App")
    assert (app.kind, app.signature) == (Kind.FUNCTION, "(): JSX.Element")


# --- JavaScript ----------------------------------------------------------------------------


def test_javascript_definitions() -> None:
    source = b"""\
/** Greeter. */
class Greeter extends Base {
  static #count = 0;
  /** Say hi. */
  hello(who) { const local = 1; return who; }
  get title() { return 1; }
  static make() {}
  constructor() { super(); }
}
function plain(a, b) {}
const handler = async (x) => x;
const SIZE = 10;
const obj = { m() {} };
"""
    result = _extract_source("javascript", source, "g.js")
    got = {s.record.qualified_name: (s.record.kind, s.record.modifiers) for s in result.symbols}
    assert got == {
        "Greeter": (Kind.CLASS, ("pub",)),
        "Greeter.#count": (Kind.FIELD, ("priv", "static")),
        "Greeter.hello": (Kind.METHOD, ("pub",)),
        "Greeter.title": (Kind.PROPERTY, ("pub",)),
        "Greeter.make": (Kind.METHOD, ("pub", "static")),
        "Greeter.constructor": (Kind.CONSTRUCTOR, ("pub",)),
        "plain": (Kind.FUNCTION, ("pub",)),
        "handler": (Kind.FUNCTION, ("pub", "async")),
        "SIZE": (Kind.CONSTANT, ("pub",)),
        "obj": (Kind.VARIABLE, ("pub",)),
    }
    assert _one(result, "Greeter").doc_summary == "Greeter."
    assert _one(result, "Greeter.hello").doc_summary == "Say hi."
    assert _one(result, "plain").signature == "(a, b)"
    assert _one(result, "handler").signature == "(x)"


# --- URN・body_sha256・決定性 ----------------------------------------------------------------

_FIXTURE_FILES = [
    ("python", "src/shop/services/payment.py"),
    ("python", "src/shop/services/limits.py"),
    ("python", "src/shop/models/receipt.py"),
    ("python", "src/shop/models/crlf_bom.py"),
    ("python", "src/shop/models/no_final_newline.py"),
    ("python", "src/shop/api/routes.py"),
    ("python", "tests/test_payment.py"),
    ("typescript", "web/src/payment.ts"),
    ("typescript", "web/src/app.ts"),
]


@pytest.mark.parametrize(("language_id", "rel"), _FIXTURE_FILES)
def test_every_urn_round_trips_and_is_unique(language_id: str, rel: str) -> None:
    result = _extract_fixture(language_id, rel)
    assert result.symbols, rel
    urns = [str(s.record.urn) for s in result.symbols]
    assert len(set(urns)) == len(urns)
    for s in result.symbols:
        rec = s.record
        parsed = parse(str(rec.urn))
        assert parsed == rec.urn
        assert parsed.path == rel
        assert parsed.workspace_id == WS
        assert isinstance(parsed.selector, CodeSel)
        assert parsed.selector.qualified == rec.qualified_name
        assert rec.name == rec.qualified_name.rsplit(".", 1)[-1]
        assert rec.range.contains_range(rec.selection or rec.range)
        # JSON 外部表現にできる（selection は 1 行）
        assert SymbolRecord.from_json(rec.to_json()).urn == rec.urn


def test_extract_twice_gives_the_same_urns_and_body_hashes() -> None:
    first = _extract_fixture("python", "src/shop/services/payment.py")
    second = _extract_fixture("python", "src/shop/services/payment.py")
    assert [(str(s.record.urn), s.body_sha256) for s in first.symbols] == [
        (str(s.record.urn), s.body_sha256) for s in second.symbols
    ]
    assert all(len(s.body_sha256) == 64 for s in first.symbols)


def test_body_hash_separates_signature_and_body_changes() -> None:
    def hashes(source: bytes) -> dict[str, str]:
        return {
            s.record.qualified_name: s.body_sha256 for s in _extract_source("python", source, "h.py").symbols
        }

    base = hashes(b"def f(a):\n    return a + 1\n\ndef g():\n    return 0\n")
    sig_changed = hashes(b"def f(a, b):\n    return a + 1\n\ndef g():\n    return 0\n")
    body_changed = hashes(b"def f(a):\n    return a + 2\n\ndef g():\n    return 0\n")
    assert sig_changed["f"] == base["f"] and sig_changed["g"] == base["g"]
    assert body_changed["f"] != base["f"] and body_changed["g"] == base["g"]


def test_extract_returns_records_only() -> None:
    parsed = _parsed("python", b"def f(): ...\n")
    records = extract(
        parsed,
        language_id="python",
        language=_language("python"),
        query_source=load_query_source("python"),
        workspace_id=WS,
        rel=RelPath("a.py"),
    )
    assert [type(r) for r in records] == [SymbolRecord]
    assert str(records[0].urn) == "lk://poly/a.py#f"


def test_syntax_errors_still_extract_what_parses() -> None:
    result = _extract_source("python", b"def ok(): ...\ndef broken(:\n", "e.py")
    assert "ok" in _by_qualified(result)


def test_same_name_different_arity_get_arity_selectors() -> None:
    source = b"""\
@overload
def f(a): ...
@overload
def f(a, b): ...
def f(a, b=None): ...
x = 1
x = 2
"""
    result = _extract_source("python", source, "o.py")
    urns = sorted(
        str(s.record.urn).split("#", 1)[1] for s in result.symbols if s.record.qualified_name == "f"
    )
    # f(a) だけ /1。2 引数の 2 つは同じアリティなのでシグネチャで区別する（どちらも素の URN にはならない）
    assert urns[0] == "f/1"
    assert all(u.startswith("f/2~") for u in urns[1:]) and len(urns) == 3
    # 同じシグネチャの再代入は最後が素の URN
    xs = {str(s.record.urn).split("#", 1)[1] for s in result.symbols if s.record.qualified_name == "x"}
    assert "x" in xs and len(xs) == 2


# --- クエリの読み込み ------------------------------------------------------------------------


def test_load_query_source() -> None:
    assert "@definition.class" in load_query_source("python")
    assert load_query_source("tsx") == load_query_source("typescript")
    assert "interface_declaration" in load_query_source("typescript")
    assert "field_definition" in load_query_source("javascript")
    with pytest.raises(ValueError):
        load_query_source("cobol")


def test_unsupported_language_is_rejected() -> None:
    with pytest.raises(ValueError):
        extract_detailed(
            _parsed("python", b""),
            language_id="cobol",
            language=_language("python"),
            query_source="",
            workspace_id=WS,
            rel=RelPath("a.cob"),
        )
