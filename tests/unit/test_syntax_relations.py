"""``syntax/relations.py``（構文木 → ``relations`` の ``structural`` 行の候補。仕様書 §9.4、T-06 処理規約 2・3、T-07 処理規約 2・4）の単体テスト。

フィクスチャ F-POLY（``tests/fixtures/poly``）を ``tree_sitter.Parser`` で直接解析する。位置は 0 始まりの行・コードポイント列。
``resolve_name`` は ``imports.extract_imports`` / ``imported_name_map`` から作る（呼び手の配線と同じ形）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

from lucianke.core.model import Confidence, RelPath
from lucianke.core.textpos import LineIndex
from lucianke.syntax.imports import extract_imports, imported_name_map, make_resolver
from lucianke.syntax.relations import RelationCandidate, extract_relations, load_refs_source
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import ParsedFile

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS = "ws"

_LANGUAGES: dict[str, Callable[[], object]] = {
    "python": tree_sitter_python.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "javascript": tree_sitter_javascript.language,
}

PAYMENT = f"lk://{WS}/src/shop/services/payment.py"
LIMITS = f"lk://{WS}/src/shop/services/limits.py"


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


def _name_resolver(parsed: ParsedFile, rel: str, language_id: str) -> Callable[[str], str | None]:
    """import 表（``imported_name_map``）から「名前 → URN」を作る。モジュールそのものはファイルの URN、名前は ``#名前``。"""
    resolve = make_resolver(
        from_rel=RelPath(rel), language_id=language_id, exists=lambda p: (POLY / p).is_file()
    )
    table = imported_name_map(extract_imports(parsed, language_id=language_id), resolve)

    def resolve_name(name: str) -> str | None:
        hit = table.get(name)
        if hit is None:
            return None
        path, original = hit
        return f"lk://{WS}/{path}" if original == "*" else f"lk://{WS}/{path}#{original}"

    return resolve_name


def _relations(
    language_id: str,
    source: bytes,
    rel: str,
    resolve_name: Callable[[str], str | None] | None = None,
) -> list[RelationCandidate]:
    parsed = _parsed(language_id, source)
    language = _language(language_id)
    symbols = extract(
        parsed,
        language_id=language_id,
        language=language,
        query_source=load_query_source(language_id),
        workspace_id=WS,
        rel=RelPath(rel),
    )
    return extract_relations(
        parsed,
        language_id=language_id,
        language=language,
        symbols=symbols,
        workspace_id=WS,
        rel=RelPath(rel),
        resolve_name=resolve_name,
    )


def _poly(rel: str, language_id: str, *, resolve: bool = True) -> list[RelationCandidate]:
    source = (POLY / rel).read_bytes()
    resolver = _name_resolver(_parsed(language_id, source), rel, language_id) if resolve else None
    return _relations(language_id, source, rel, resolver)


def _where(
    rels: list[RelationCandidate],
    *,
    kind: str | None = None,
    name: str | None = None,
    source: str | None = None,
) -> list[RelationCandidate]:
    return [
        r
        for r in rels
        if (kind is None or r.relation_kind == kind)
        and (name is None or r.target_name == name)
        and (source is None or r.source_urn.endswith(source))
    ]


def _one(rels: list[RelationCandidate], **kw: str | None) -> RelationCandidate:
    found = _where(rels, **kw)
    assert len(found) == 1, f"{kw}: {found}"
    return found[0]


# --- routes.py: 呼び出しとガード ---------------------------------------------------------


@pytest.fixture(scope="module")
def routes() -> list[RelationCandidate]:
    return _poly("src/shop/api/routes.py", "python")


def test_create_order_calls_process_charge_with_guard(routes):
    r = _one(routes, kind="CALLS", name="process_charge", source="#create_order")
    assert r.source_urn == f"lk://{WS}/src/shop/api/routes.py#create_order"
    assert r.target_urn == f"{PAYMENT}#PaymentProcessor.process_charge"
    assert (r.call_site_line, r.call_site_col) == (
        15,
        34,
    )  # ``processor.process_charge(req)`` の ``process_charge``
    assert r.use_kind == "call"
    assert r.guard_condition == "req.amount > 0 and not paid"
    assert r.confidence is Confidence.STRUCTURAL


def test_retry_order_calls_process_charge_with_guard(routes):
    r = _one(routes, kind="CALLS", name="process_charge", source="#retry_order")
    assert r.target_urn == f"{PAYMENT}#PaymentProcessor.process_charge"
    assert (r.call_site_line, r.call_site_col) == (24, 38)
    assert r.guard_condition == "attempt < 3"


def test_constructor_calls_resolve_through_the_import_table(routes):
    for source in ("#create_order", "#retry_order"):
        r = _one(routes, kind="CALLS", name="PaymentRequest", source=source)
        assert r.target_urn == f"{PAYMENT}#PaymentRequest"
        assert r.use_kind == "call"
    # モジュール直下の ``processor = PaymentProcessor(LimitTable())`` はファイルの URN から
    file_level = _where(routes, kind="CALLS", name="PaymentProcessor")
    assert [r.source_urn for r in file_level] == [f"lk://{WS}/src/shop/api/routes.py"]
    assert file_level[0].target_urn == f"{PAYMENT}#PaymentProcessor"
    assert _one(routes, kind="CALLS", name="LimitTable").target_urn == f"{LIMITS}#LimitTable"


def test_without_resolve_name_cross_file_targets_stay_unresolved():
    rels = _poly("src/shop/api/routes.py", "python", resolve=False)
    r = _one(rels, kind="CALLS", name="PaymentRequest", source="#create_order")
    assert r.target_urn is None
    assert r.target_name == "PaymentRequest"


def test_locals_keyword_names_and_definition_names_are_not_references(routes):
    names = {r.target_name for r in routes if r.relation_kind != "IMPORTS"}
    # パラメータ・ローカル変数（req, receipt）、キーワード引数の名前（order_id= の左）、定義の名前（create_order）は数えない
    assert "req" not in names and "receipt" not in names and "create_order" not in names
    assert not [r for r in routes if r.target_name == "order_id" and r.use_kind == "w"]
    # 属性の読み取りはレシーバの型（注釈つきパラメータ）から解決する
    assert (
        _one(routes, name="amount", source="#create_order").target_urn == f"{PAYMENT}#PaymentRequest.amount"
    )


def test_imports_have_one_record_per_name_at_the_name_token(routes):
    imports = _where(routes, kind="IMPORTS")
    assert {r.target_name for r in imports} == {"Decimal", "LimitTable", "PaymentProcessor", "PaymentRequest"}
    assert all(
        r.use_kind == "import" and r.source_urn == f"lk://{WS}/src/shop/api/routes.py" for r in imports
    )
    request = _one(imports, name="PaymentRequest")
    assert request.target_urn == f"{PAYMENT}#PaymentRequest"
    assert (request.call_site_line, request.call_site_col) == (
        7,
        52,
    )  # ``from shop.services.payment import PaymentProcessor, PaymentRequest``
    assert _one(imports, name="Decimal").target_urn is None  # 標準ライブラリはワークスペース外


def test_one_record_per_position(routes):
    positions = [(r.call_site_line, r.call_site_col) for r in routes]
    assert len(positions) == len(set(positions))
    assert positions == sorted(positions)


# --- limits.py: 同じファイル内の解決 -----------------------------------------------------


def test_mutual_recursion_resolves_within_the_file():
    rels = _poly("src/shop/services/limits.py", "python")
    retry_calls_schedule = _one(rels, kind="CALLS", name="schedule", source="#retry")
    assert retry_calls_schedule.target_urn == f"{LIMITS}#schedule"
    assert retry_calls_schedule.source_urn == f"{LIMITS}#retry"
    assert (retry_calls_schedule.call_site_line, retry_calls_schedule.call_site_col) == (23, 11)
    schedule_calls_retry = _one(rels, kind="CALLS", name="retry", source="#schedule")
    assert schedule_calls_retry.target_urn == f"{LIMITS}#retry"
    assert schedule_calls_retry.source_urn == f"{LIMITS}#schedule"


def test_unknown_receiver_does_not_resolve_to_the_enclosing_method():
    # ``self._table.get(...)``: ``_table`` の型は不明。同名の ``LimitTable.get`` を自分自身の呼び出しと誤認しない
    rels = _poly("src/shop/services/limits.py", "python")
    assert _one(rels, kind="CALLS", name="get", source="#LimitTable.get").target_urn is None


def test_guard_of_short_circuit_default():
    rels = _poly("src/shop/services/limits.py", "python")
    decimal_calls = _where(rels, kind="CALLS", name="Decimal", source="#LimitTable.__init__")
    assert len(decimal_calls) == 2 and all(r.guard_condition == "!(table)" for r in decimal_calls)


# --- payment.py: 継承・型・メンバー呼び出し ---------------------------------------------


@pytest.fixture(scope="module")
def payment() -> list[RelationCandidate]:
    return _poly("src/shop/services/payment.py", "python")


def test_inherits(payment):
    r = _one(payment, kind="INHERITS")
    assert r.source_urn == f"{PAYMENT}#PaymentDeclined"
    assert r.target_name == "Exception"
    assert r.use_kind == "impl"
    assert (r.call_site_line, r.call_site_col) == (26, 22)


def test_type_annotation_is_type_uses(payment):
    r = _one(payment, kind="TYPE_USES", name="PaymentRequest")
    assert r.source_urn == f"{PAYMENT}#PaymentProcessor.process_charge"
    assert r.target_urn == f"{PAYMENT}#PaymentRequest"
    assert r.use_kind == "type"
    assert (r.call_site_line, r.call_site_col) == (38, 40)  # ``req: PaymentRequest``
    # 引数 ``limits: LimitTable`` は import 表から
    limit = _one(payment, kind="TYPE_USES", name="LimitTable", source="#PaymentProcessor.__init__")
    assert limit.target_urn == f"{LIMITS}#LimitTable"


def test_member_call_through_a_typed_self_attribute(payment):
    # ``self._limits = limits``（``limits: LimitTable``）→ ``self._limits.get(...)`` は ``LimitTable.get``
    r = _one(payment, kind="CALLS", name="get", source="#PaymentProcessor._validate_limit")
    assert r.target_urn == f"{LIMITS}#LimitTable.get"
    assert (r.call_site_line, r.call_site_col) == (55, 29)
    assert r.guard_condition is None


def test_self_method_call_and_guard_text(payment):
    r = _one(payment, kind="CALLS", name="_validate_limit")
    assert r.target_urn == f"{PAYMENT}#PaymentProcessor._validate_limit"
    assert r.source_urn == f"{PAYMENT}#PaymentProcessor.process_charge"
    raised = _one(payment, kind="CALLS", name="PaymentDeclined")
    assert raised.target_urn == f"{PAYMENT}#PaymentDeclined"
    assert raised.guard_condition == "not self._validate_limit(req.amount)"


def test_self_member_missing_in_the_class_is_not_filled_with_another_class(payment):
    # ``self.currency = "JPY"``: ``PaymentProcessor`` に ``currency`` の定義は無い（``PaymentRequest.currency`` は別のクラス）
    r = _one(payment, kind="REFERENCES", name="currency", source="#PaymentProcessor.__init__")
    assert r.target_urn is None
    assert r.use_kind == "w"


def test_decorator_is_a_reference_from_the_decorated_definition(payment):
    r = _one(payment, name="transactional")
    assert (r.relation_kind, r.use_kind) == ("REFERENCES", "r")
    assert r.source_urn == f"{PAYMENT}#PaymentProcessor.process_charge"
    assert r.target_urn == f"{PAYMENT}#transactional"


# --- web/src/app.ts ----------------------------------------------------------------------


def test_typescript_new_and_member_call():
    rels = _poly("web/src/app.ts", "typescript")
    ctor = _one(rels, kind="CALLS", name="PaymentClient")
    assert ctor.source_urn == f"lk://{WS}/web/src/app.ts"
    assert ctor.target_urn == f"lk://{WS}/web/src/payment.ts#PaymentClient"
    assert (ctor.call_site_line, ctor.call_site_col) == (2, 19)  # ``new PaymentClient(...)``
    charge = _one(rels, kind="CALLS", name="charge")
    assert charge.source_urn == f"lk://{WS}/web/src/app.ts#checkout"
    assert charge.target_urn == f"lk://{WS}/web/src/payment.ts#PaymentClient.charge"
    assert (charge.call_site_line, charge.call_site_col) == (6, 16)
    imp = _one(rels, kind="IMPORTS")
    assert imp.target_name == "PaymentClient"
    assert imp.target_urn == f"lk://{WS}/web/src/payment.ts#PaymentClient"
    assert _one(rels, kind="TYPE_USES", name="Promise").target_urn is None


# --- 単体のソース ------------------------------------------------------------------------


def test_non_ascii_columns_are_codepoints_not_bytes():
    src = 's = "あ😀"; foo(1)\n名前 = "漢字"; bar("日本語", baz(2))\n'
    rels = _relations("python", src.encode(), "m.py")
    foo = _one(rels, name="foo")
    assert (foo.call_site_line, foo.call_site_col) == (
        0,
        10,
    )  # 'あ' と '😀' は 1 コードポイントずつ（バイトなら 15）
    bar = _one(rels, name="bar")
    assert (bar.call_site_line, bar.call_site_col) == (1, 11)
    baz = _one(rels, name="baz")
    assert (baz.call_site_line, baz.call_site_col) == (1, 22)


def test_use_kinds_in_python():
    src = (
        b"import mod\n"
        b"counter = 0\n"
        b"class Box:\n"
        b"    size = 1\n"
        b"def f(box, n):\n"
        b"    box.size = n\n"
        b"    box.size += 1\n"
        b"    n += 1\n"
        b"    return box.size + mod.limit\n"
    )
    rels = _relations("python", src, "m.py")
    by_pos = {(r.call_site_line, r.call_site_col): r for r in rels}
    assert (by_pos[(5, 8)].use_kind, by_pos[(5, 8)].relation_kind) == ("w", "REFERENCES")  # box.size = n
    assert by_pos[(6, 8)].use_kind == "rw"  # box.size += 1
    assert (7, 4) not in by_pos  # ローカルの ``n += 1`` は数えない
    assert by_pos[(8, 15)].use_kind == "r"  # box.size
    assert (by_pos[(8, 26)].target_name, by_pos[(8, 26)].use_kind) == ("limit", "r")  # mod.limit
    assert (1, 0) not in by_pos and (3, 4) not in by_pos  # 定義の名前（counter, size）
    assert by_pos[(0, 7)].relation_kind == "IMPORTS"


def test_python_import_forms_and_module_attribute_resolution():
    table = {"limits": f"{LIMITS}", "L": f"{LIMITS}#LimitTable"}
    src = b"from shop.services import limits\nfrom shop.services.limits import LimitTable as L\nimport os.path\n\nx = limits.retry(1)\ny = L.get\n"
    rels = _relations("python", src, "m.py", table.get)
    imports = {r.target_name: r for r in _where(rels, kind="IMPORTS")}
    assert set(imports) == {"limits", "LimitTable", "os.path"}
    assert imports["limits"].target_urn == LIMITS
    assert imports["LimitTable"].target_urn == f"{LIMITS}#LimitTable"  # 別名 L で引く
    assert imports["os.path"].target_urn is None
    call = _one(rels, kind="CALLS", name="retry")
    assert call.target_urn == f"{LIMITS}#retry"  # モジュール属性 → <ファイル URN>#名前
    assert _one(rels, kind="REFERENCES", name="get").target_urn == f"{LIMITS}#LimitTable.get"


def test_inherits_through_a_local_base_class():
    src = b"class Base:\n    def ping(self): ...\n\nclass Child(Base):\n    def run(self):\n        return self.ping()\n"
    rels = _relations("python", src, "m.py")
    assert _one(rels, kind="CALLS", name="ping").target_urn == f"lk://{WS}/m.py#Base.ping"
    base = _one(rels, kind="INHERITS")
    assert base.target_urn == f"lk://{WS}/m.py#Base" and base.source_urn == f"lk://{WS}/m.py#Child"


# --- 継承したメンバー（BUG-0075） ----------------------------------------------------------------

SUB_PY = (
    b"from pkg.base import Base\n\n\nclass Sub(Base):\n    def run(self):\n        return self.helper()\n\n"
    b"    def again(self):\n        return self.run()\n\n    @classmethod\n    def build(cls):\n        return cls.make()\n\n\n"
    b"def use(s: Sub):\n    return s.helper()\n\n\ndef fresh():\n    return Sub().helper()\n"
)


def test_member_inherited_from_a_base_in_another_file_is_left_to_the_settle_step():
    """基底クラスが別ファイル: ``<このクラス>.name`` を返す（索引の整えが ``INHERITS`` 行をたどって基底のメンバーに結ぶ）。

    ``self`` / ``cls`` / ``s: Sub`` / ``Sub()`` のどれも同じ。受け手の型は分かっているので ``name_only`` にはしない。自分のメンバーは自分のもの。
    """
    rels = _relations("python", SUB_PY, "pkg/sub.py", {"Base": f"lk://{WS}/pkg/base.py#Base"}.get)
    requested = f"lk://{WS}/pkg/sub.py#Sub.helper"
    assert [(r.source_urn.rpartition("#")[2], r.target_urn) for r in _where(rels, kind="CALLS", name="helper")] == [
        ("Sub.run", requested),
        ("use", requested),
        ("fresh", requested),
    ]
    assert _one(rels, kind="CALLS", name="make").target_urn == f"lk://{WS}/pkg/sub.py#Sub.make"
    assert _one(rels, kind="CALLS", name="run").target_urn == f"lk://{WS}/pkg/sub.py#Sub.run"
    assert not [r for r in rels if r.name_only]


def test_receiver_of_an_imported_class_points_at_that_class_member():
    src = b"from pkg.sub import Sub\n\n\ndef go(s: Sub):\n    return s.helper()\n"
    rels = _relations("python", src, "pkg/use.py", {"Sub": f"lk://{WS}/pkg/sub.py#Sub"}.get)
    assert _one(rels, kind="CALLS", name="helper").target_urn == f"lk://{WS}/pkg/sub.py#Sub.helper"


def test_same_file_bases_follow_the_c3_order():
    """菱形（``D(B, C)``・``B(A)``・``C(A)``）: C3 は D, B, C, A なので ``C`` の上書きが ``A`` に勝つ（深さ優先なら ``A.m``）。"""
    src = (
        b"class A:\n    def m(self): ...\n\nclass B(A):\n    pass\n\nclass C(A):\n    def m(self): ...\n\n"
        b"class D(B, C):\n    def run(self):\n        return self.m()\n"
    )
    rels = _relations("python", src, "d.py")
    assert _one(rels, kind="CALLS", name="m").target_urn == f"lk://{WS}/d.py#C.m"


def test_opaque_external_base_before_the_member_stops_the_lookup():
    """``Worker(Thread, Base)``: 先の ``Thread`` が ``helper`` を持つかもしれないので結ばない。``Plain(Base, Thread)`` は ``Base.helper``。
    ``ABC`` はユーザーのメソッドを持たないので素通りする（``Shape(ABC, Base)`` は ``Base.helper``）。"""
    src = (
        b"from threading import Thread\nfrom abc import ABC\n\n\nclass Base:\n    def helper(self): ...\n\n\n"
        b"class Worker(Thread, Base):\n    def go(self):\n        return self.helper()\n\n\n"
        b"class Plain(Base, Thread):\n    def go(self):\n        return self.helper()\n\n\n"
        b"class Shape(ABC, Base):\n    def go(self):\n        return self.helper()\n"
    )
    table = {"Thread": "lk://_ext/threading#Thread", "ABC": "lk://_ext/abc#ABC"}
    rels = _relations("python", src, "w.py", table.get)
    worker = _one(rels, kind="CALLS", name="helper", source="#Worker.go")
    assert worker.target_urn is None and not worker.name_only
    assert _one(rels, kind="CALLS", name="helper", source="#Plain.go").target_urn == f"lk://{WS}/w.py#Base.helper"
    assert _one(rels, kind="CALLS", name="helper", source="#Shape.go").target_urn == f"lk://{WS}/w.py#Base.helper"


def test_dotted_base_is_left_to_the_settle_step():
    """``class Sub(base.Base)``: 綴りから同じファイルのクラスと分からないので ``<このクラス>.name``（整えが INHERITS 行をたどる）。"""
    src = b"from pkg import base\n\n\nclass Sub(base.Base):\n    def run(self):\n        return self.helper()\n"
    rels = _relations("python", src, "pkg/sub.py", {"base": f"lk://{WS}/pkg/base.py"}.get)
    assert _one(rels, kind="CALLS", name="helper").target_urn == f"lk://{WS}/pkg/sub.py#Sub.helper"
    assert _one(rels, kind="INHERITS").target_urn == f"lk://{WS}/pkg/base.py#Base"


def test_typescript_members_inherited_from_another_file():
    """``class Sub extends Base`` の ``this.helper()`` と、``interface Loud extends Pingable`` の ``l.ping()``（基底は別ファイル）。"""
    src = (
        b'import { Base, Pingable } from "./base";\n\nexport class Sub extends Base {\n  run(): number {\n'
        b"    return this.helper();\n  }\n}\n\nexport interface Loud extends Pingable {\n  shout(): void;\n}\n\n"
        b"export function use(l: Loud): void {\n  l.ping();\n  l.shout();\n}\n"
    )
    table = {"Base": f"lk://{WS}/web/base.ts#Base", "Pingable": f"lk://{WS}/web/base.ts#Pingable"}
    rels = _relations("typescript", src, "web/sub.ts", table.get)
    assert _one(rels, kind="CALLS", name="helper").target_urn == f"lk://{WS}/web/sub.ts#Sub.helper"
    assert _one(rels, kind="CALLS", name="ping").target_urn == f"lk://{WS}/web/sub.ts#Loud.ping"
    assert _one(rels, kind="CALLS", name="shout").target_urn == f"lk://{WS}/web/sub.ts#Loud.shout"


def test_typescript_extends_implements_and_types():
    src = (
        b"import { Base, Port as P } from './base';\n"
        b"interface Shape extends P { area(): number }\n"
        b"export class Circle extends Base implements Shape, Other<Item> {\n"
        b"  private repo: Repo = new Repo();\n"
        b"  constructor(private readonly client: Client) { super(); }\n"
        b"  run(n: number): Item {\n"
        b"    this.repo.save(n);\n"
        b"    this.client.send(n);\n"
        b"    n++;\n"
        b"    return new Item() as Item;\n"
        b"  }\n"
        b"}\n"
    )
    table = {
        "Base": f"lk://{WS}/base.ts#Base",
        "P": f"lk://{WS}/base.ts#Port",
        "Repo": f"lk://{WS}/repo.ts#Repo",
        "Client": f"lk://{WS}/client.ts#Client",
    }
    rels = _relations("typescript", src, "c.ts", table.get)
    iface = _one(rels, kind="INHERITS", source="#Shape")
    assert (iface.target_name, iface.target_urn) == ("P", f"lk://{WS}/base.ts#Port")
    base = _one(rels, kind="INHERITS", source="#Circle")
    assert (base.target_name, base.target_urn, base.use_kind) == ("Base", f"lk://{WS}/base.ts#Base", "impl")
    impls = _where(rels, kind="IMPLEMENTS")
    assert [r.target_name for r in impls] == ["Shape", "Other"]
    assert impls[0].target_urn == f"lk://{WS}/c.ts#Shape" and impls[0].use_kind == "impl"
    assert _one(rels, kind="TYPE_USES", name="Item", source="#Circle").use_kind == "type"
    assert (
        _one(rels, kind="CALLS", name="save").target_urn == f"lk://{WS}/repo.ts#Repo.save"
    )  # フィールドの型
    assert (
        _one(rels, kind="CALLS", name="send").target_urn == f"lk://{WS}/client.ts#Client.send"
    )  # パラメータプロパティ
    assert _one(rels, kind="CALLS", name="Item", source="#Circle.run").use_kind == "call"
    assert not [r for r in rels if r.target_name == "n"]  # パラメータ ``n`` は数えない（``n++`` も）


def test_typescript_guard_and_rw():
    src = b"let total = 0;\nfunction f(ok: boolean) {\n  if (ok) { g(); }\n  total += 1;\n  total = 2;\n}\nfunction g() {}\n"
    rels = _relations("typescript", src, "g.ts")
    call = _one(rels, kind="CALLS", name="g")
    assert call.target_urn == f"lk://{WS}/g.ts#g" and call.guard_condition == "ok"
    kinds = [(r.call_site_line, r.use_kind) for r in rels if r.target_name == "total"]
    assert kinds == [(3, "rw"), (4, "w")]


def test_javascript_and_jsx_use_the_javascript_queries():
    src = b"import { helper } from './h.js';\nclass A extends B { run() { helper(); this.other(); } other() {} }\n"
    for language_id in ("javascript",):
        rels = _relations(language_id, src, "a.js", {"helper": f"lk://{WS}/h.js#helper"}.get)
        assert _one(rels, kind="CALLS", name="helper").target_urn == f"lk://{WS}/h.js#helper"
        assert _one(rels, kind="CALLS", name="other").target_urn == f"lk://{WS}/a.js#A.other"
        assert _one(rels, kind="INHERITS").target_name == "B"


def test_unsupported_language_and_queries():
    with pytest.raises(ValueError):
        _relations_unsupported()
    for language_id in ("python", "typescript", "tsx", "javascript", "jsx"):
        assert "@call" in load_refs_source(language_id)
    with pytest.raises(ValueError):
        load_refs_source("rust")


def _relations_unsupported() -> None:
    extract_relations(
        _parsed("python", b"x = 1\n"),
        language_id="rust",
        language=_language("python"),
        symbols=[],
        workspace_id=WS,
        rel=RelPath("a.rs"),
    )


def test_syntax_errors_do_not_break_extraction():
    rels = _relations("python", b"def f(:\n    g(1)\n\ndef h():\n    k(2)\n", "e.py")
    assert {"k"} <= {r.target_name for r in rels}
