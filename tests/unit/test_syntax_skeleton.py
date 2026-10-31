"""syntax.skeleton（ファイル骨格のレコード。設計書 §7.4.4、仕様書 §11 T-02・§6.1.1）のテスト。"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from lucianke.core.model import Confidence, Kind, SymbolRecord
from lucianke.core.records import Record
from lucianke.syntax import skeleton
from lucianke.syntax.parsers import parse_source
from lucianke.syntax.skeleton import build_file_record, default_estimate, extract_imports
from lucianke.syntax.support import SUPPORT
from lucianke.syntax.symbols import extract, load_query_source
from lucianke.syntax.types import ParsedFile, SkeletonOptions

_GRAMMAR_PATH = Path(__file__).resolve().parents[1] / "contract" / "compact_grammar.py"
_spec = importlib.util.spec_from_file_location("lucianke_test_compact_grammar_skeleton", _GRAMMAR_PATH)
assert _spec is not None and _spec.loader is not None
compact_grammar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compact_grammar)

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PY_REL = "src/shop/services/payment.py"
TS_REL = "web/src/payment.ts"


def parse(source: bytes, language_id: str, rel: str) -> tuple[ParsedFile, list[SymbolRecord]]:
    parsed, _ = parse_source(source, language_id, hashlib.sha256(source).hexdigest())
    symbols = extract(
        parsed,
        language_id=language_id,
        language=SUPPORT[language_id].grammar(),
        query_source=load_query_source(language_id),
        workspace_id="app",
        rel=rel,  # type: ignore[arg-type]
    )
    return parsed, symbols


def fixture(rel: str, language_id: str) -> tuple[ParsedFile, list[SymbolRecord], str]:
    source = (POLY / rel).read_bytes()
    parsed, symbols = parse(source, language_id, rel)
    return parsed, symbols, source.decode("utf-8")


def build(rel: str, language_id: str, **opts: Any) -> tuple[Record, ParsedFile, list[SymbolRecord], str]:
    parsed, symbols, text = fixture(rel, language_id)
    rec = build_file_record(parsed, symbols, rel=rel, opts=SkeletonOptions(**opts))
    return rec, parsed, symbols, text


def py(**opts: Any) -> Record:
    return build(PY_REL, "python", **opts)[0]


def ts(**opts: Any) -> Record:
    return build(TS_REL, "typescript", **opts)[0]


def text_of(rec: Record) -> str:
    return "\n".join(rec.lines)


def flatten(symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in symbols:
        out.append(s)
        out.extend(flatten(s["children"]))
    return out


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --- 形（仕様書 §6.1.1） ---------------------------------------------------------------------------


def test_record_identity_and_header() -> None:
    rec, parsed, _, text = build(PY_REL, "python")
    assert rec.kind == "file-block"
    assert rec.key == rec.group == PY_REL
    assert rec.confidence is Confidence.STRUCTURAL
    assert rec.shrink is not None
    sha12 = hashlib.sha256(text.encode()).hexdigest()[:12]
    body = rec.lines[1:]
    assert (
        rec.lines[0]
        == f"=== {PY_REL} ({len(text.splitlines())} lines, skeleton: {len(body)} lines, sha={sha12}) ==="
    )
    assert rec.data["file_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert parsed.sha256[:12] == sha12


@pytest.mark.parametrize(("rel", "lang"), [(PY_REL, "python"), (TS_REL, "typescript")])
def test_lines_match_compact_grammar(rel: str, lang: str) -> None:
    rec = build(rel, lang)[0]
    assert compact_grammar.validate(text_of(rec), require_header=False) == []
    shrunk = rec.shrink(10_000) if rec.shrink else None
    assert shrunk is not None
    assert compact_grammar.validate(text_of(shrunk), require_header=False) == []


def test_python_process_charge_block() -> None:
    lines = text_of(py()).splitlines()
    i = next(n for n, line in enumerate(lines) if "process_charge" in line)
    assert lines[i] == (
        "  [Method] pub async process_charge(self, req: PaymentRequest) -> Receipt @L38-52 { ...13 lines omitted }"
    )
    attrs = []
    for line in lines[i + 1 :]:
        if not line.startswith("    "):
            break
        attrs.append(line.strip())
    assert any(a.startswith("doc: 注文に対する決済") for a in attrs)
    assert "decorators: @transactional" in attrs
    assert "raises: ValueError, PaymentDeclined" in attrs  # 本体の raise の文書順
    # URN は lk://app/<ブロックのパス>#PaymentProcessor.process_charge で導けるので urn: 行は出さない（§6.1 規約 2。BUG-0069）
    assert not any(a.startswith("urn:") for a in attrs)


def test_python_nesting_and_imports() -> None:
    lines = text_of(py()).splitlines()
    # import はモジュールごとにまとめ、モジュール名を名前ごとに繰り返さない（§6.1 規約 10。BUG-0069）
    assert lines[1] == (
        "imports: [os; dataclasses: dataclass; decimal: Decimal; shop.models.receipt: Receipt; "
        "shop.services.limits: LimitExceeded, LimitTable]"
    )
    assert "annotations" not in lines[1]  # from __future__ は載せない
    assert "[Class] pub PaymentProcessor @L31-64" in lines
    # クラスの直下のメソッドは 1 段インデントで短い名前、最上位は修飾名
    assert any(
        line.startswith("  [Constructor] pub __init__(self, limits: LimitTable) -> None @L34-36")
        for line in lines
    )
    assert "  [Field] pub order_id @L22" in lines  # PaymentRequest の下の 1 段


def test_fold_lines_and_data_agree() -> None:
    rec = py()
    by_name = {s["qualified_name"]: s for s in flatten(rec.data["symbols"])}
    assert (
        by_name["PaymentProcessor.process_charge"]["fold_lines"] == 13
    )  # 15 行 − デコレータ 1 − シグネチャ 1
    assert by_name["PaymentProcessor.__init__"]["fold_lines"] == 2
    assert by_name["PaymentProcessor"]["fold_lines"] == 0  # 入れ物には付けない
    assert "PaymentProcessor.process_charge" in {
        c["qualified_name"] for c in by_name["PaymentProcessor"]["children"]
    }
    assert by_name["PaymentProcessor.process_charge"]["decorators"] == ["@transactional"]
    assert by_name["PaymentProcessor.process_charge"]["raises"] == ["ValueError", "PaymentDeclined"]
    assert by_name["PaymentRequest"]["decorators"] == ["@dataclass"]
    # compact に出した畳みの数と一致
    for line in rec.lines:
        m = re.search(r"\[(Method|Constructor|Function)\] .*?(\w+)\(.*\{ \.\.\.(\d+) lines omitted \}$", line)
        if m:
            assert by_name[next(q for q in by_name if q.endswith(m.group(2)))]["fold_lines"] == int(
                m.group(3)
            )


def test_json_item_shape() -> None:
    rec = py()
    data = rec.data
    assert data["ok"] is True
    assert (data["path"], data["language"], data["tier"]) == (PY_REL, "python", 1)
    assert data["line_count"] == 64
    assert data["skeleton_line_count"] == len(rec.lines) - 1
    assert len(data["file_sha256"]) == 64
    assert data["confidence_level"] == "structural"
    assert data["truncated_members"] == 0
    assert data["imports"][0] == "os"
    top = [s["name"] for s in data["symbols"]]
    assert top == ["transactional", "PaymentRequest", "PaymentDeclined", "PaymentProcessor"]
    first = data["symbols"][1]
    for key in (
        "urn",
        "path",
        "name",
        "qualified_name",
        "kind",
        "modifiers",
        "range",
        "signature",
        "children",
        "fold_lines",
    ):
        assert key in first


#: 仕様書 §6.2 の SymbolRecord のキー（null でも出す。例の ``"scip_symbol": null``）と、§11 T-02 で加えるキー。
SPEC_SYMBOL_KEYS = frozenset(
    {
        "urn",
        "scip_symbol",
        "path",
        "name",
        "qualified_name",
        "kind",
        "modifiers",
        "range",
        "selection_range",
        "signature",
        "return_type",
        "doc_summary",
        "confidence_level",
        "children",
        "fold_lines",
        "decorators",
        "raises",
    }
)


@pytest.mark.parametrize(("rel", "lang"), [(PY_REL, "python"), (TS_REL, "typescript")])
def test_json_symbols_keep_null_and_empty_keys(rel: str, lang: str) -> None:
    """BUG-0065: null・空の値でもキーを省かない（全段・縮退版も）。子の無い記号にも ``children: []`` がある。"""
    rec = build(rel, lang)[0]
    shrunk = rec.shrink(10_000) if rec.shrink else None
    assert shrunk is not None
    for r in (rec, shrunk):
        symbols = flatten(r.data["symbols"])
        assert symbols
        for s in symbols:
            assert set(s) >= SPEC_SYMBOL_KEYS, (s["qualified_name"], sorted(SPEC_SYMBOL_KEYS - set(s)))
    leaves = [s for s in flatten(rec.data["symbols"]) if s["kind"] in ("Field", "Property")]
    assert leaves  # null・空の値を持つ記号で確かめている
    assert all(s["children"] == [] and s["decorators"] == [] and s["raises"] == [] for s in leaves)
    assert any(s["signature"] is None for s in flatten(rec.data["symbols"]))


# --- オプション ------------------------------------------------------------------------------------


def test_include_private_false_drops_private() -> None:
    full, hidden = text_of(py()), text_of(py(include_private=False))
    assert "_validate_limit" in full
    assert "_validate_limit" not in hidden
    assert "truncated" not in hidden  # 注文による除外は予算の省略ではない
    assert "process_charge" in hidden
    names = {s["name"] for s in flatten(py(include_private=False).data["symbols"])}
    assert "_validate_limit" not in names


def test_urn_lines_only_for_urns_not_derivable_from_the_block() -> None:
    """``urn:`` 行は ``lk://<ws>/<ブロックのパス>#<入れ子をたどった修飾名>`` で導けない URN だけ（§6.1 規約 2。BUG-0069）。"""
    for mode in ("none", "public", "all"):
        assert "urn:" not in text_of(py(include_urns=mode))  # payment.py の URN はすべて導ける
    # 同名の再定義（arity・sighash 付き）は導けないので、include_urns の範囲で出す
    source = (
        b"def f(a):\n    return 1\n\n"
        b"def f(a, b):\n    return 2\n\n"
        b"class C:\n    def m(self):\n        def inner():\n            pass\n"
    )
    parsed, symbols = parse(source, "python", "m.py")
    lines = build_file_record(parsed, symbols, rel="m.py", opts=SkeletonOptions()).lines
    shown = [ln.strip() for ln in lines if ln.strip().startswith("urn:")]
    assert shown == ["urn: lk://app/m.py#f/1", "urn: lk://app/m.py#f/2"]
    # 入れ子は C → m → inner とたどった修飾名で導ける（C.m.inner）ので出さない
    assert {str(s.urn) for s in symbols} >= {"lk://app/m.py#C.m", "lk://app/m.py#C.m.inner"}
    assert "    [Function] pub inner() @L9-10 { ...1 lines omitted }" in lines
    none = build_file_record(parsed, symbols, rel="m.py", opts=SkeletonOptions(include_urns="none")).lines
    assert not any("urn:" in ln for ln in none)


def test_urn_modes_choose_which_non_derivable_urns_are_shown() -> None:
    source = b"def _f(a):\n    return 1\n\ndef _f(a, b):\n    return 2\n"
    parsed, symbols = parse(source, "python", "m.py")

    def text(mode: str) -> str:
        return text_of(build_file_record(parsed, symbols, rel="m.py", opts=SkeletonOptions(include_urns=mode)))  # type: ignore[arg-type]

    assert "urn:" not in text("public")  # 非公開
    assert "urn: lk://app/m.py#_f/1" in text("all") and "urn: lk://app/m.py#_f/2" in text("all")


def test_doc_and_imports_switches() -> None:
    rec = py(include_doc_summary=False, include_imports=False)
    assert "doc:" not in text_of(rec)
    assert "imports:" not in text_of(rec)
    assert rec.data["imports"] == []


def test_doc_line_is_cut_at_200_chars() -> None:
    source = ("def f():\n    '''" + "あ" * 300 + "'''\n    return 1\n").encode()
    parsed, symbols = parse(source, "python", "m.py")
    rec = build_file_record(parsed, symbols, rel="m.py", opts=SkeletonOptions())
    doc = next(line for line in rec.lines if line.strip().startswith("doc:"))
    assert len(doc) <= 200
    assert doc.endswith("…") or len(doc) < 200


# --- TypeScript ------------------------------------------------------------------------------------


def test_typescript_skeleton() -> None:
    rec = ts()
    lines = text_of(rec).splitlines()
    assert "[Interface] pub export PaymentRequest @L1-5" in lines
    assert "[Class] pub export PaymentClient @L7-19" in lines
    assert (
        "  [Method] pub async charge(req: PaymentRequest, retries = 0): Promise<string> @L13-18 { ...5 lines omitted }"
        in lines
    )
    # オーバーロード宣言（本体なし）は畳まない
    assert "  [Method] pub charge(req: PaymentRequest): Promise<string> @L10" in lines
    impl = lines.index(
        "  [Method] pub async charge(req: PaymentRequest, retries = 0): Promise<string> @L13-18 { ...5 lines omitted }"
    )
    assert lines[impl + 1].strip().startswith("doc: 決済を要求する")
    assert "    raises: Error" in lines
    assert rec.data["tier"] == 1
    assert rec.data["language"] == "typescript"


def test_typescript_imports_and_private_toplevel() -> None:
    source = (
        b'import X, { A, B as C } from "mod";\nimport * as ns from "./ns";\nimport "side";\n'
        b"function helper(): void {}\nexport function pub(a: number): number { return a; }\n"
    )
    parsed, symbols = parse(source, "typescript", "m.ts")
    assert extract_imports(parsed) == [
        "X from mod",
        "A from mod",
        "B as C from mod",
        "* as ns from ./ns",
        "side",
    ]
    shown = build_file_record(parsed, symbols, rel="m.ts", opts=SkeletonOptions(include_private=False))
    assert "helper" not in text_of(shown)  # 非 export の最上位は非公開
    assert "pub(a: number): number" in text_of(shown)


def test_compact_import_line_groups_by_module() -> None:
    """compact の ``imports:`` はモジュールごと（``m: a, b``、``import m`` は ``m``）。別名は残す（§6.1 規約 10。BUG-0069）。"""
    py_src = (
        b"import a.b as c, d\nfrom . import y\nfrom .m import (p as q, r)\nfrom x import *\n"
        b"from .m import s\nimport d\nif TYPE_CHECKING:\n    from t import T\n"
    )
    parsed, symbols = parse(py_src, "python", "m.py")
    rec = build_file_record(parsed, symbols, rel="m.py", opts=SkeletonOptions())
    # 同じモジュールの from 文は 1 つにまとめ（.m）、同じ import の重複（d）は 1 回
    assert rec.lines[1] == "imports: [a.b as c; d; .: y; .m: p as q, r, s; x: *; t: T]"
    assert rec.data["imports"][:3] == ["a.b as c", "d", "y from ."]  # json の形は変えない
    ts_src = (
        b'import X, { A, B as C } from "mod";\nimport * as ns from "./ns";\nimport "side";\n'
        b'import { D } from "mod";\n'
    )
    parsed, symbols = parse(ts_src, "typescript", "m.ts")
    rec = build_file_record(parsed, symbols, rel="m.ts", opts=SkeletonOptions())
    assert rec.lines[1] == "imports: [mod: X, A, B as C, D; ./ns: * as ns; side]"
    assert compact_grammar.validate("\n".join(rec.lines), require_header=False) == []


def test_python_imports_summary() -> None:
    source = (
        b"import a.b as c, d\nfrom . import y\nfrom .m import (p as q, r)\nfrom x import *\n"
        b"from __future__ import annotations\nif TYPE_CHECKING:\n    from t import T\n"
        b"def f():\n    import inside\n"
    )
    parsed, _ = parse(source, "python", "m.py")
    assert extract_imports(parsed) == [
        "a.b as c",
        "d",
        "y from .",
        "p as q from .m",
        "r from .m",
        "* from x",
        "T from t",
    ]


def test_raises_do_not_leak_across_nested_definitions() -> None:
    source = (
        b"def outer(x):\n    def inner():\n        raise KeyError('k')\n    raise ValueError('v')\n"
        b"def one(): return 1\n"
    )
    parsed, symbols = parse(source, "python", "m.py")
    rec = build_file_record(parsed, symbols, rel="m.py", opts=SkeletonOptions())
    by_name = {s["qualified_name"]: s for s in flatten(rec.data["symbols"])}
    assert by_name["outer"]["raises"] == ["ValueError"]
    assert by_name["outer.inner"]["raises"] == ["KeyError"]
    assert by_name["outer"]["fold_lines"] == 1  # 4 行 − シグネチャ 1 − inner の 2 行
    assert by_name["one"]["fold_lines"] == 0
    assert "[Function] pub one() @L5" in rec.lines


# --- 事後条件 2: 骨格のテキストは元ファイルの対応行と一致 ----------------------------------------------------


@pytest.mark.parametrize(("rel", "lang"), [(PY_REL, "python"), (TS_REL, "typescript")])
def test_signature_and_doc_text_come_from_source(rel: str, lang: str) -> None:
    rec, _, _, text = build(rel, lang)
    src_lines = text.splitlines()
    checked = 0
    for s in flatten(rec.data["symbols"]):
        if s["kind"] not in ("Function", "Method", "Constructor"):
            continue
        start, end = s["range"]["start_line"], s["range"]["end_line"]
        window = squash(" ".join(src_lines[start - 1 : end]))
        sig = s["signature"]
        assert f"{s['name']}{sig.split(' -> ')[0]}" in window, (s["qualified_name"], sig)
        if s["doc_summary"]:
            # Doc コメントは範囲の直前にあることがある（TS の JSDoc。範囲への取り込みは boundaries の仕事）
            doc_window = squash(" ".join(src_lines[max(0, start - 6) : end]))
            assert s["doc_summary"].rstrip("。") in doc_window
        checked += 1
    assert checked >= 4
    # import 行: 各項目の名前と元のモジュールが import 文に現れる
    import_text = squash(" ".join(line for line in src_lines if "import" in line))
    for item in rec.data["imports"]:
        name, _, module = item.partition(" from ")
        assert name.split(" as ")[0].split(".")[0] in import_text
        if module:
            assert module in import_text


# --- 縮退 ------------------------------------------------------------------------------------------


def _chain(rec: Record) -> list[tuple[int, Record]]:
    """縮退の鎖: 予算を丸ごとの大きさから 1 ずつ下げて、版が変わるところの ``(予算, 版)``（浅い段から深い段へ）。

    丸ごとの大きさちょうどの予算から始める（注の分で、最初の段が丸ごとと同じ大きさになることがある）。"""
    assert rec.shrink is not None
    out: list[tuple[int, Record]] = []
    for budget in range(default_estimate(text_of(rec)), 0, -1):
        small = rec.shrink(budget)
        if small is None:
            break
        if not out or small.lines != out[-1][1].lines:
            out.append((budget, small))
    return out


def _docs(rec: Record) -> list[str]:
    return [ln for ln in rec.lines if ln.lstrip().startswith("doc: ")]


def _names(rec: Record) -> list[str]:
    return [s["qualified_name"] for s in flatten(rec.data["symbols"])]


def test_shrink_drops_one_by_one_in_order_and_notes_truncation() -> None:
    """処理規約 8（BUG-0078）: doc: 行を 1 件ずつ（深い順・後ろから）→ 非公開メンバー → 入れ子の公開メンバーを 1 件ずつ
    （後ろのクラスの後ろから）落とし、最上位は残す。どの段も予算に収まり、注に落とした件数・入れ物ごとの件数・丸ごと取り直す
    目安を書く。以前は 6,324 → 441 のように入れ子を一度に落とし、予算の 1 割しか使わなかった。"""
    rec = py()
    chain = _chain(rec)
    for budget, small in chain:
        assert default_estimate(text_of(small)) <= budget
        assert small.shrink is None and small.shrink_json is None  # 縮退は一度だけ
        assert small.data["skeleton_line_count"] == len(small.lines) - 1
        assert f"skeleton: {len(small.lines) - 1} lines" in small.lines[0]
        assert small.lines[-1].startswith("- truncated: ") and " members omitted" in small.lines[-1]
        assert small.data["truncated_note"] == small.lines[-1][2:].replace(
            small.lines[-1].rsplit("max_tokens>=", 1)[1].split()[0],
            small.data["truncated_note"].rsplit("max_tokens>=", 1)[1].split()[0],
        )  # json の注は同じ文で、目安だけ json の大きさから
    # doc: 行は 1 件ずつ、入れ子（process_charge・apply_discount）から後ろ向きに、最上位は最後に落ちる
    docs = [len(_docs(small)) for _, small in chain]
    assert docs[:6] == [4, 3, 2, 1, 0, 0]
    assert not any(ln.startswith("    doc: ") for ln in _docs(chain[1][1]))  # 入れ子の 2 件が先に落ちる
    assert "- truncated: 0 members omitted (2 doc lines omitted)" in chain[1][1].lines[-1]
    assert "(doc omitted)" in chain[4][1].lines[-1]
    # 次に非公開メンバー、その次に入れ子の公開メンバーを 1 件ずつ（後ろのクラスの後ろから）
    gone = [
        sorted(set(_names(a)) - set(_names(b))) for (_, a), (_, b) in zip(chain[4:], chain[5:], strict=False)
    ]
    assert gone == [
        ["PaymentProcessor._validate_limit"],
        ["PaymentProcessor.apply_discount"],
        ["PaymentProcessor.process_charge"],
        ["PaymentProcessor.__init__"],
        ["PaymentRequest.currency"],
        ["PaymentRequest.amount"],
        ["PaymentRequest.order_id"],
    ]
    tiny = chain[-1][1]
    for top in ("transactional(fn) @L13-15", "PaymentRequest @L18-24", "PaymentProcessor @L31-64"):
        assert any(line.startswith("[") and top in line for line in tiny.lines)  # 最上位は残る
    assert not any(line.startswith("  [") for line in tiny.lines)  # 入れ子メンバーは落ちる
    assert tiny.data["truncated_members"] == 7  # Field 3 + メソッド 4
    assert tiny.data["truncated_by_container"] == [
        {"container": "PaymentRequest", "omitted": 3},
        {"container": "PaymentProcessor", "omitted": 4},
    ]
    whole = default_estimate(text_of(rec) + "\n")
    m = re.fullmatch(
        r"- truncated: 7 members omitted \(doc omitted\): PaymentRequest 3, PaymentProcessor 4; "
        r"whole file: max_tokens>=(\d+) with this path alone",
        tiny.lines[-1],
    )
    assert m is not None, tiny.lines[-1]
    assert int(m[1]) >= whole + 100 and int(m[1]) % 100 == 0  # 丸ごと + 前置きの分
    assert rec.data["truncated_by_container"] == [] and rec.data["truncated_note"] is None  # 丸ごとは空


def test_shrink_drops_non_derivable_urns_after_doc() -> None:
    """導けない ``urn:`` 行（オーバーロード）がある TS では、doc → urn の順に落ち、注に両方を挙げる。"""
    rec = ts()
    assert "urn: " in text_of(rec) and "doc: " in text_of(rec)
    chain = [small for _, small in _chain(rec)]
    first_without_urn = next(i for i, small in enumerate(chain) if "urn: " not in text_of(small))
    assert first_without_urn > 0
    assert all(
        "doc:" not in text_of(small) for small in chain[first_without_urn - 1 :]
    )  # doc が先に全部落ちる
    assert "urn: " in text_of(chain[first_without_urn - 1])
    no_urn = chain[first_without_urn]
    assert no_urn.lines[-1].startswith("- truncated: 0 members omitted (doc, urn omitted); ")
    # json は urn: 行を持たない（どのシンボルも urn を持つ）ので、json の注は urn を挙げない
    assert no_urn.data["truncated_note"].startswith("truncated: 0 members omitted (doc omitted); ")


@pytest.mark.parametrize(
    "make",
    [
        lambda: py(),
        lambda: py(include_private=False),
        lambda: py(include_doc_summary=False, include_urns="none"),
        lambda: ts(),
        lambda: ts(include_urns="all"),
    ],
)
def test_shrink_is_monotone_in_the_budget(make: Any) -> None:
    """``Record.shrink`` は予算について単調（BUG-0067・BUG-0078）: 予算を 1 ずつ上げても、見える行・シンボル・doc は
    減らず、版も小さくならない。json の ``shrink_json`` も同じ（実際の json の大きさで予算に収まる）。"""
    from lucianke.mcp.render import json_out

    rec = make()
    full = default_estimate(text_of(rec))
    previous: Record | None = None
    for budget in range(1, full + 1):
        small = rec.shrink(budget)
        if small is None:
            assert previous is None
            continue
        assert default_estimate(text_of(small)) <= budget
        if previous is not None:
            assert set(_names(small)) >= set(_names(previous)), budget
            assert set(_docs(small)) >= set(_docs(previous)), budget
            assert default_estimate(text_of(small)) >= default_estimate(text_of(previous)), budget
        previous = small
    whole_json = default_estimate(json_out.record_text(rec))
    previous = None
    for budget in range(1, whole_json + 1, 3):
        small = rec.shrink_json(budget) if rec.shrink_json else None
        if small is None:
            assert previous is None
            continue
        assert default_estimate(json_out.record_text(small)) <= budget
        if previous is not None:
            assert set(_names(small)) >= set(_names(previous)), budget
        previous = small


def test_shrink_json_picks_the_deepest_version_that_fits_exactly() -> None:
    """``shrink_json`` の見積もり（JSON の部品の和）は、実際に JSON を作って数えた値と一致する: 各段の実際の json の大きさ
    ちょうどの予算で、その段か、より浅い（json では大きくない）段が返る。見積もりが小さすぎれば予算を超える段が返り
    （``test_shrink_is_monotone_in_the_budget`` が全予算で検める）、大きすぎればより深い段が返る（パッカーの二分探索の代わり。
    BUG-0078）。"""
    from lucianke.mcp.render import json_out

    for rec in (py(), ts(), py(include_urns="all")):
        assert rec.shrink_json is not None
        sizes: list[int] = []
        for _, small in _chain(rec):
            size = default_estimate(json_out.record_text(small))
            sizes.append(size)
            got = rec.shrink_json(size)  # 見積もりが大きすぎれば、より深い段が返る
            assert got is not None and default_estimate(json_out.record_text(got)) <= size
            assert set(_names(got)) >= set(_names(small)) and len(got.lines) >= len(small.lines)
        assert len(sizes) > 3


def test_truncated_containers_are_capped_and_top_level_is_named() -> None:
    """落としたメンバーの入れ物は文書順に 8 件まで（残りは ``(others)``）、最上位で落とした非公開は ``(top level)``。"""
    classes = "".join(f"class C{i}:\n    def m(self) -> None:\n        pass\n\n" for i in range(12))
    source = (classes + "def _helper() -> int:\n    return 1\n").encode("utf-8")
    parsed, symbols = parse(source, "python", "many.py")
    rec = build_file_record(parsed, symbols, rel="many.py", opts=SkeletonOptions())
    tiny = _chain(rec)[-1][1]
    assert tiny.data["truncated_members"] == 13
    by = tiny.data["truncated_by_container"]
    assert [b["container"] for b in by] == [f"C{i}" for i in range(7)] + ["(others)"]
    assert (
        sum(b["omitted"] for b in by) == 13 and by[-1]["omitted"] == 6
    )  # C7〜C11 の 5 件 + 最上位の _helper
    assert ": C0 1, C1 1, C2 1, C3 1, C4 1, C5 1, C6 1, (others) 6; " in tiny.lines[-1]
    first = _chain(rec)[0][1]  # 最初に落ちるのは文書の最後の非公開（最上位の _helper）
    assert "_helper" not in _names(first) and all(f"C{i}" in _names(first) for i in range(12))
    assert first.data["truncated_by_container"][-1] == {"container": "(top level)", "omitted": 1}


def test_type_literal_members_are_children_of_the_alias() -> None:
    """BUG-0114: TS の型リテラルのメンバーは別名・メンバーの子（複数行の別名も、1 行の別名・入れ子も）。1 行に書いたものも最上位に
    並べない（最上位の公開シンボルは縮退で落とせない）ので、縮退ではほかの入れ子のメンバーと同じく落とせる。"""
    source = (
        b"export type KyOptions = {\n  /** Retry. */\n  retry?: number;\n  inner: { a: number; b?: { c: string } };\n};\n"
        b"export type U = { kind: 'a'; x: number } | { kind: 'b'; y: string };\n"
    )
    parsed, symbols = parse(source, "typescript", "t.ts")
    rec = build_file_record(parsed, symbols, rel="t.ts", opts=SkeletonOptions())
    assert list(rec.lines[1:]) == [
        "[Type] pub export KyOptions @L1-5",
        "  [Property] pub retry @L3",
        "    doc: Retry.",
        "  [Property] pub inner @L4",
        "    [Property] pub a @L4",
        "    [Property] pub b @L4",
        "      [Property] pub c @L4",
        "[Type] pub export U @L6",
        "  [Property] pub kind @L6",
        f"    urn: {_urn_of(symbols, 'U.kind', first=True)}",
        "  [Property] pub x @L6",
        "  [Property] pub kind @L6",
        "  [Property] pub y @L6",
    ]
    [ky] = [s for s in rec.data["symbols"] if s["qualified_name"] == "KyOptions"]
    assert [c["name"] for c in ky["children"]] == ["retry", "inner"]
    assert [c["name"] for c in ky["children"][1]["children"]] == ["a", "b"]
    tiny = _chain(rec)[-1][1]
    assert _names(tiny) == ["KyOptions", "U"]  # 入れ子のメンバーはすべて落とせる


def _urn_of(symbols: list[SymbolRecord], qualified: str, *, first: bool) -> str:
    found = sorted((s for s in symbols if s.qualified_name == qualified), key=lambda s: s.range.start)
    return str(found[0 if first else -1].urn)


def test_shrink_returns_none_when_nothing_fits() -> None:
    rec = py()
    assert rec.shrink is not None
    assert rec.shrink(5) is None


def test_shrink_uses_injected_estimator() -> None:
    parsed, symbols, _ = fixture(PY_REL, "python")
    calls: list[str] = []

    def est(text: str) -> int:
        calls.append(text)
        return len(text.splitlines())  # 1 行 1 トークン

    rec = build_file_record(parsed, symbols, rel=PY_REL, opts=SkeletonOptions(), estimate=est)
    assert rec.shrink is not None
    small = rec.shrink(20)
    assert small is not None and len(small.lines) <= 20 and calls


def test_shrink_skips_levels_that_change_nothing() -> None:
    # doc も urn も出していないので、最初の 2 段は同じ本文。非公開が落ちる段から効く
    rec = py(include_doc_summary=False, include_urns="none")
    assert rec.shrink is not None
    small = rec.shrink(default_estimate(text_of(rec)) - 1)
    assert small is not None
    assert "_validate_limit" not in text_of(small)
    assert "(doc" not in text_of(small)


# --- compact.py の同名関数との突き合わせ（複製が離れないように） ----------------------------------------------


def test_local_line_builders_match_compact() -> None:
    compact = pytest.importorskip("lucianke.mcp.render.compact")
    assert skeleton.file_block_header("a/b.py", lines=245, skeleton_lines=21, sha12="3F9C1A2B4C5D00") == (
        compact.file_block_header("a/b.py", lines=245, skeleton_lines=21, sha12="3F9C1A2B4C5D00")
    )
    assert skeleton.import_line(["os", "A from b"]) == compact.import_line(["os", "A from b"])
    cases = [
        dict(
            modifiers=["pub", "async"],
            sig="(self, x: int) -> Receipt",
            start_line=40,
            end_line=115,
            fold_lines=75,
            indent=1,
        ),
        dict(modifiers=[], sig="x: int", start_line=3),
        dict(modifiers=["priv"], sig="(a)", ret="int", start_line=7, end_line=7),
        dict(start_line=1, end_line=9),
    ]
    for kw in cases:
        assert skeleton.symbol_line(Kind.METHOD, "m", **kw) == compact.symbol_line(Kind.METHOD, "m", **kw)  # type: ignore[arg-type]
    long_doc = "あ" * 300
    assert skeleton.attr_line("doc", long_doc, indent=2) == compact.attr_line("doc", long_doc, indent=2)
    assert skeleton.attr_line("urn", "lk://a/b#c", indent=0) == compact.attr_line(
        "urn", "lk://a/b#c", indent=0
    )
    assert skeleton.note("truncated: 3 members omitted") == compact.note("truncated: 3 members omitted")


def test_default_estimate_matches_budget_estimator_closely() -> None:
    budget = pytest.importorskip("lucianke.mcp.budget")
    sample = text_of(py()) + "\n" + text_of(ts())
    exact = budget.TokenEstimator().count(sample)
    approx = default_estimate(sample)
    assert abs(approx - exact) <= max(2, exact * 0.05)
