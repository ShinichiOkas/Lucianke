"""core.urn のテスト（仕様書 第 7 章）。"""

from __future__ import annotations

import random

import pytest

from lucianke.core.urn import (
    CellSel,
    CodeSel,
    DataSel,
    LexSel,
    TagSel,
    Urn,
    format,
    normalize_signature,
    parse,
    relax,
    sighash,
    workspace_id_from_dirname,
)

# 仕様書 §7.2 の表の 8 例（URN 文字列と、期待する構造）
SPEC_EXAMPLES: list[tuple[str, Urn]] = [
    (
        "lk://app/src/services/payment.py#PaymentProcessor.process_charge",
        Urn("app", "src/services/payment.py", CodeSel("PaymentProcessor.process_charge")),
    ),
    (
        "lk://engine/src/math.cpp#clamp/3~9f2a4c10",
        Urn("engine", "src/math.cpp", CodeSel("clamp", 3, "9f2a4c10")),
    ),
    ("lk://app/pyproject.toml", Urn("app", "pyproject.toml")),
    (
        "lk://app/pyproject.toml#toml:project.dependencies",
        Urn("app", "pyproject.toml", DataSel("toml", "project.dependencies")),
    ),
    (
        "lk://app/reports/junit.xml#xpath:%2F%2Ftestcase%5Bfailure%5D",
        Urn("app", "reports/junit.xml", DataSel("xpath", "//testcase[failure]")),
    ),
    (
        "lk://app/notebooks/eda.ipynb#cell:3/load_frame",
        Urn("app", "notebooks/eda.ipynb", CellSel(3, "load_frame")),
    ),
    (
        "lk://app/legacy/calc.cob#ctags:COMPUTE-TAX@L120",
        Urn("app", "legacy/calc.cob", TagSel("COMPUTE-TAX", 120)),
    ),
    (
        "lk://app/conf/rules.custom#lex:MAX_RETRY@L8",
        Urn("app", "conf/rules.custom", LexSel("MAX_RETRY", 8)),
    ),
]


@pytest.mark.parametrize(("text", "expected"), SPEC_EXAMPLES, ids=[t for t, _ in SPEC_EXAMPLES])
def test_spec_examples_roundtrip(text: str, expected: Urn) -> None:
    urn = parse(text)
    assert urn == expected
    assert format(urn) == text
    assert str(urn) == text
    assert urn.format() == text
    assert parse(format(urn)) == urn


def test_spec_has_eight_examples() -> None:
    assert len(SPEC_EXAMPLES) == 8


def test_all_selector_kinds_covered() -> None:
    kinds = {type(u.selector) for _, u in SPEC_EXAMPLES}
    assert {CodeSel, DataSel, CellSel, TagSel, LexSel, type(None)} <= kinds


@pytest.mark.parametrize("kind", ["xpath", "jsonpath", "jq", "toml", "yaml"])
def test_all_data_selector_kinds(kind: str) -> None:
    text = f"lk://app/data/x.bin#{kind}:a.b%5B0%5D"
    urn = parse(text)
    assert urn.selector == DataSel(kind, "a.b[0]")  # type: ignore[arg-type]
    assert format(urn) == text


def test_cell_without_qualified() -> None:
    urn = parse("lk://app/nb.ipynb#cell:0")
    assert urn.selector == CellSel(0, None)
    assert format(urn) == "lk://app/nb.ipynb#cell:0"


def test_arity_and_sighash_independent() -> None:
    assert parse("lk://a/x.cpp#f/2").selector == CodeSel("f", 2, None)
    assert parse("lk://a/x.cpp#f~0123abcd").selector == CodeSel("f", None, "0123abcd")


def test_sighash_uppercase_is_normalized_on_parse() -> None:
    urn = parse("lk://a/x.cpp#f/1~9F2A4C10")
    assert urn.selector == CodeSel("f", 1, "9f2a4c10")
    assert format(urn) == "lk://a/x.cpp#f/1~9f2a4c10"


# --- パーセントエンコード --------------------------------------------------------------


def test_path_percent_encoding() -> None:
    urn = Urn("app", "dir/a b#c%d.py", CodeSel("f"))
    text = format(urn)
    assert text == "lk://app/dir/a%20b%23c%25d.py#f"
    assert parse(text) == urn


def test_path_keeps_non_ascii_and_encodes_unicode_space() -> None:
    urn = Urn("app", "docs/日本語 ファイル　.md")
    text = format(urn)
    assert text == "lk://app/docs/日本語%20ファイル%E3%80%80.md"
    assert parse(text) == urn


def test_parse_accepts_encoded_non_ascii() -> None:
    assert parse("lk://app/docs/%E6%97%A5%E6%9C%AC.md").path == "docs/日本.md"
    assert format(parse("lk://app/docs/%E6%97%A5%E6%9C%AC.md")) == "lk://app/docs/日本.md"


def test_qualified_encodes_delimiters() -> None:
    # デストラクタの ~ は sighash の区切りと、/ は arity の区切りと紛れないようにエンコードされる
    urn = Urn("app", "src/a.cpp", CodeSel("Foo.~Foo", 0, "deadbeef"))
    text = format(urn)
    assert text == "lk://app/src/a.cpp#Foo.%7EFoo/0~deadbeef"
    assert parse(text) == urn
    odd = Urn("app", "src/a.cpp", CodeSel("Foo.operator/"))
    assert format(odd) == "lk://app/src/a.cpp#Foo.operator%2F"
    assert parse(format(odd)) == odd


def test_qualified_non_ascii_identifier_is_kept() -> None:
    urn = Urn("app", "a.py", CodeSel("クラス.メソッド"))
    assert format(urn) == "lk://app/a.py#クラス.メソッド"
    assert parse(format(urn)) == urn


def test_tag_name_with_at_sign_is_encoded() -> None:
    urn = Urn("app", "a.cob", TagSel("A@B", 3))
    assert format(urn) == "lk://app/a.cob#ctags:A%40B@L3"
    assert parse(format(urn)) == urn


def test_expr_special_characters_roundtrip() -> None:
    for expr in ["$.store.book[?(@.price<10)]", ".a | .b", "//a[@id='x y']/b", "100% #1 ~ /"]:
        urn = Urn("app", "d.json", DataSel("jsonpath", expr))
        assert parse(format(urn)) == urn


def test_roundtrip_fuzz() -> None:
    rng = random.Random(20261029)
    alphabet = list("abcXYZ019 _-.~!$&'()*+,;=:@#%/?[]{}<>\\\"|^`\t日本語ー😀é́　")
    for _ in range(2000):
        segs = ["".join(rng.choices(alphabet, k=rng.randint(1, 8))) for _ in range(rng.randint(1, 3))]
        segs = [s for s in segs if s not in (".", "..") and "/" not in s and "\t" not in s]
        if not segs:
            continue
        path = "/".join(segs)
        parts = [
            "".join(rng.choices(alphabet, k=rng.randint(1, 6))).replace(".", "x")
            for _ in range(rng.randint(1, 3))
        ]
        qualified = ".".join(parts)
        expr = "".join(rng.choices(alphabet, k=rng.randint(1, 10)))
        selectors = [
            None,
            CodeSel(qualified),
            CodeSel(qualified, rng.randint(0, 9), "0123abcd"),
            DataSel("xpath", expr),
            CellSel(rng.randint(0, 9), qualified),
            TagSel(qualified, rng.randint(1, 99)),
            LexSel(qualified, rng.randint(1, 99)),
        ]
        for sel in selectors:
            urn = Urn("ws-1.x_", path, sel)
            text = format(urn)
            assert text.isprintable()
            assert " " not in text
            assert text.count("#") <= 1
            assert parse(text) == urn, (text, urn)


# --- 外部シンボル ------------------------------------------------------------------------


def test_external_urn() -> None:
    urn = parse("lk://_ext/requests@2.31#Session.get")
    assert urn.is_external
    assert urn.external_parts() == ("requests", "2.31")
    assert urn.selector == CodeSel("Session.get")
    assert urn == Urn.external("requests", "2.31", "Session.get")
    assert format(urn) == "lk://_ext/requests@2.31#Session.get"


def test_external_urn_without_version_and_scoped_package() -> None:
    assert parse("lk://_ext/numpy#linalg.norm").external_parts() == ("numpy", None)
    scoped = parse("lk://_ext/@types/node@20.1.0#Buffer")
    assert scoped.external_parts() == ("@types/node", "20.1.0")
    assert parse("lk://_ext/@types/node#Buffer").external_parts() == ("@types/node", None)
    assert format(scoped) == "lk://_ext/@types/node@20.1.0#Buffer"


def test_external_parts_rejects_workspace_urn() -> None:
    with pytest.raises(ValueError):
        parse("lk://app/a.py").external_parts()


# --- relax -------------------------------------------------------------------------------


def test_relax_drops_arity_and_sighash() -> None:
    urn = parse("lk://engine/src/math.cpp#clamp/3~9f2a4c10")
    assert format(relax(urn)) == "lk://engine/src/math.cpp#clamp"
    assert urn.relax() == relax(urn)
    assert relax(parse("lk://a/x.cpp#f/2")).selector == CodeSel("f")
    assert relax(parse("lk://a/x.cpp#f~0123abcd")).selector == CodeSel("f")


def test_relax_leaves_other_selectors_alone() -> None:
    for text, _ in SPEC_EXAMPLES:
        urn = parse(text)
        if not (isinstance(urn.selector, CodeSel) and (urn.selector.arity or urn.selector.sighash)):
            assert relax(urn) == urn


# --- sighash -----------------------------------------------------------------------------


def test_sighash_shape() -> None:
    h = sighash("(self, req: PaymentRequest) -> Receipt")
    assert len(h) == 8
    assert int(h, 16) >= 0
    assert h == h.lower()


def test_sighash_ignores_whitespace_commas_and_parens_spacing() -> None:
    base = sighash("(self, req: PaymentRequest) -> Receipt")
    variants = [
        "(self,req: PaymentRequest) -> Receipt",
        "( self , req: PaymentRequest ) -> Receipt",
        "(self,\n    req: PaymentRequest,\n) -> Receipt",
        "(self,  req:\tPaymentRequest)  ->  Receipt",
        "  (self, req: PaymentRequest, ) -> Receipt  ",
    ]
    for v in variants:
        assert sighash(v) == base, v


def test_sighash_distinguishes_different_signatures() -> None:
    assert sighash("(a: int)") != sighash("(a: str)")
    assert sighash("(a, b)") != sighash("(b, a)")
    assert sighash("(a: int, b: int)") != sighash("(a: int, b: int, c: int)")


def test_sighash_start_line_disambiguates_redefinitions() -> None:
    assert sighash("(x)", start_line=10) != sighash("(x)", start_line=20)
    assert sighash("(x)", start_line=10) == sighash(" ( x ) ", start_line=10)
    assert sighash("(x)", start_line=10) != sighash("(x)")


def test_normalize_signature_rules() -> None:
    assert normalize_signature("( a ,  b , )") == "(a,b)"
    assert normalize_signature("[ 1 , 2 ,]") == "[1,2]"
    assert normalize_signature("a\n\t b") == "a b"
    # -> の前後は正規化しない（仕様書 §7.2 の規則どおり）
    assert normalize_signature("(a) -> int") == "(a)-> int"
    assert normalize_signature("(a)->int") == "(a)->int"


# --- workspace_id ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Lucianke", "Lucianke"),
        ("my project", "my_project"),
        ("a.b-c_d", "a.b-c_d"),
        ("日本語", "___"),
        ("a/b:c", "a_b_c"),
        ("😀", "_"),
        ("", "_"),
    ],
)
def test_workspace_id_from_dirname(name: str, expected: str) -> None:
    assert workspace_id_from_dirname(name) == expected
    Urn(workspace_id_from_dirname(name), "a.py")  # 常に有効な workspace-id


# --- 不正な入力 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "app/src/a.py",
        "http://app/a.py",
        "lk://",
        "lk://app",
        "lk://app/",
        "lk:///a.py",
        "lk://ap p/a.py",
        "lk://app/a b.py",
        "lk://app/a.py#f g",
        "lk://app/a\tb.py",
        "lk://app/a.py#",
        "lk://app/a.py#a#b",
        "lk://app//a.py",
        "lk://app/./a.py",
        "lk://app/../a.py",
        "lk://app/a%2Fb.py",
        "lk://app/a/%2E%2E/b.py",
        "lk://app/a%zz.py",
        "lk://app/a%2.py",
        "lk://app/a%ff.py",
        "lk://app/a.py#f..g",
        "lk://app/a.py#.f",
        "lk://app/a.py#f/x",
        "lk://app/a.py#f/1/2",
        "lk://app/a.py#f~12345",
        "lk://app/a.py#f~1234567g",
        "lk://app/a.py#/3",
        "lk://app/a.py#~9f2a4c10",
        "lk://app/a.py#xpath:",
        "lk://app/a.py#cell:",
        "lk://app/a.py#cell:x",
        "lk://app/a.py#cell:3/",
        "lk://app/a.py#ctags:NAME",
        "lk://app/a.py#ctags:NAME@L",
        "lk://app/a.py#ctags:@L3",
        "lk://app/a.py#lex:NAME@L0",
        "lk://app/a.py#f%zz",
    ],
)
def test_parse_rejects_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse(text)


def test_parse_rejects_non_string() -> None:
    with pytest.raises(ValueError):
        parse(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Urn("a b", "x.py"),
        lambda: Urn("", "x.py"),
        lambda: Urn("app", ""),
        lambda: Urn("app", "a//b"),
        lambda: Urn("app", "../x"),
        lambda: Urn("app", "a\x00b"),
        lambda: CodeSel(""),
        lambda: CodeSel("a..b"),
        lambda: CodeSel("a", arity=-1),
        lambda: CodeSel("a", sighash="XYZ"),
        lambda: CodeSel("a", sighash="9F2A4C10"),
        lambda: DataSel("xpath", ""),
        lambda: DataSel("css", "a"),  # type: ignore[arg-type]
        lambda: CellSel(-1),
        lambda: TagSel("", 1),
        lambda: TagSel("a", 0),
        lambda: LexSel("a", 0),
    ],
)
def test_constructors_validate(factory) -> None:  # noqa: ANN001
    with pytest.raises(ValueError):
        factory()


def test_urn_is_hashable_and_frozen() -> None:
    a = parse("lk://app/a.py#f")
    b = parse("lk://app/a.py#f")
    assert hash(a) == hash(b)
    assert {a, b} == {a}
    with pytest.raises(AttributeError):
        a.path = "b.py"  # type: ignore[misc]
