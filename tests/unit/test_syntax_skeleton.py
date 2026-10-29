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
    assert any(
        a.startswith("urn: lk://app/src/shop/services/payment.py#PaymentProcessor.process_charge")
        for a in attrs
    )


def test_python_nesting_and_imports() -> None:
    lines = text_of(py()).splitlines()
    assert lines[1].startswith("imports: [")
    assert "Decimal from decimal" in lines[1]
    assert "LimitTable from shop.services.limits" in lines[1]
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


# --- オプション ------------------------------------------------------------------------------------


def test_include_private_false_drops_private() -> None:
    full, hidden = text_of(py()), text_of(py(include_private=False))
    assert "_validate_limit" in full
    assert "_validate_limit" not in hidden
    assert "truncated" not in hidden  # 注文による除外は予算の省略ではない
    assert "process_charge" in hidden
    names = {s["name"] for s in flatten(py(include_private=False).data["symbols"])}
    assert "_validate_limit" not in names


def test_urn_modes() -> None:
    none, public, every = (text_of(py(include_urns=m)) for m in ("none", "public", "all"))
    assert "urn:" not in none
    assert "urn: lk://app/src/shop/services/payment.py#PaymentProcessor.process_charge" in public
    assert "#PaymentProcessor._validate_limit" not in public
    assert "urn: lk://app/src/shop/services/payment.py#PaymentProcessor._validate_limit" in every


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


def test_shrink_drops_in_order_and_notes_truncation() -> None:
    rec = py()
    full_tokens = default_estimate(text_of(rec))
    sizes = []
    for budget in (full_tokens - 1, 400, 250, 150):
        small = rec.shrink(budget) if rec.shrink else None
        assert small is not None, budget
        assert default_estimate(text_of(small)) <= budget
        assert small.shrink is None  # 縮退は一度だけ
        assert any(line.startswith("- truncated: ") and " members omitted" in line for line in small.lines)
        assert small.data["skeleton_line_count"] == len(small.lines) - 1
        assert f"skeleton: {len(small.lines) - 1} lines" in small.lines[0]
        sizes.append(len(small.lines))
    assert sizes == sorted(sizes, reverse=True) and sizes[0] < len(rec.lines) + 1 and sizes[-1] < sizes[0]

    no_doc = rec.shrink(full_tokens - 1)
    assert no_doc is not None
    assert "doc:" not in text_of(no_doc) and "urn:" in text_of(no_doc)  # 最初に落ちるのは doc:
    no_urn = rec.shrink(default_estimate(text_of(no_doc)) - 1)
    assert no_urn is not None and "urn:" not in text_of(no_urn) and "_validate_limit" in text_of(no_urn)

    tiny = rec.shrink(150)
    assert tiny is not None
    assert "_validate_limit" not in text_of(tiny)  # 非公開メンバーが落ちる
    for top in ("transactional(fn) @L13-15", "PaymentRequest @L18-24", "PaymentProcessor @L31-64"):
        assert any(line.startswith("[") and top in line for line in tiny.lines)  # 最上位は残る
    assert not any(line.startswith("  [") for line in tiny.lines)  # 入れ子メンバーは落ちる
    m = re.search(r"truncated: (\d+) members omitted", text_of(tiny))
    assert m is not None
    assert int(m.group(1)) == tiny.data["truncated_members"] == 7  # Field 3 + メソッド 4


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
