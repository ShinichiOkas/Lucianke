"""core.model のテスト（設計書 §5、仕様書 §5.3・§5.10・§6.1・§6.2・§8.3）。"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from lucianke.core.model import (
    Capability,
    Confidence,
    EngineChoice,
    FileInfo,
    Kind,
    LspStatus,
    Position,
    Range,
    RelPath,
    SymbolRecord,
    TargetRef,
    Tier,
)
from lucianke.core.urn import CodeSel, Urn, parse

SPEC = Path(__file__).resolve().parents[2] / "docs" / "Lucianke仕様書.md"

# --- Confidence --------------------------------------------------------------------------


def test_confidence_order() -> None:
    assert Confidence.EXACT > Confidence.STRUCTURAL > Confidence.HEURISTIC > Confidence.LEXICAL_FALLBACK
    assert Confidence.LEXICAL_FALLBACK < Confidence.HEURISTIC < Confidence.STRUCTURAL < Confidence.EXACT
    assert Confidence.EXACT >= Confidence.EXACT
    assert Confidence.EXACT <= Confidence.EXACT
    assert not Confidence.EXACT < Confidence.EXACT
    assert [c.rank for c in Confidence] == [3, 2, 1, 0]


def test_confidence_min_max_sorted_use_level_order_not_alphabetical() -> None:
    # 辞書順なら "exact" < "heuristic" < "lexical_fallback" < "structural" になってしまう
    levels = [Confidence.STRUCTURAL, Confidence.EXACT, Confidence.LEXICAL_FALLBACK, Confidence.HEURISTIC]
    assert min(levels) is Confidence.LEXICAL_FALLBACK
    assert max(levels) is Confidence.EXACT
    assert sorted(levels) == [
        Confidence.LEXICAL_FALLBACK,
        Confidence.HEURISTIC,
        Confidence.STRUCTURAL,
        Confidence.EXACT,
    ]
    assert min([Confidence.EXACT, Confidence.STRUCTURAL]) is Confidence.STRUCTURAL


def test_confidence_is_still_a_str() -> None:
    assert Confidence.EXACT == "exact"
    assert Confidence("lexical_fallback") is Confidence.LEXICAL_FALLBACK
    assert json.dumps({Confidence.EXACT: 1}) == '{"exact": 1}'
    assert {Confidence.EXACT: 1} == {"exact": 1}
    assert str(Confidence.STRUCTURAL) == "structural"


def test_confidence_compare_with_foreign_type() -> None:
    with pytest.raises(TypeError):
        _ = Confidence.EXACT < 3  # type: ignore[operator]
    with pytest.raises(TypeError):
        _ = Confidence.EXACT > "certain"  # 水準でない文字列は辞書順比較に落とさない
    # 水準の値の文字列は水準として比較する（辞書順なら "exact" < "structural" になってしまう）
    assert Confidence.EXACT > "structural"
    assert "structural" < Confidence.EXACT
    assert Confidence.LEXICAL_FALLBACK < "heuristic"


# --- Tier / Capability / Kind -----------------------------------------------------------


def test_tier() -> None:
    assert [t.value for t in Tier] == [1, 2, 3, 4]
    assert Tier(1) is Tier.T1
    assert Tier.T1 < Tier.T4
    assert max(Tier.T1, Tier.T3) is Tier.T3


def test_capability_has_the_twelve_from_design() -> None:
    assert {c.name for c in Capability} == {
        "SKELETON",
        "SEARCH",
        "DEFINITION",
        "BODY",
        "REFERENCES",
        "IMPLEMENTATIONS",
        "CALL_HIERARCHY",
        "DIAGNOSTICS",
        "RENAME",
        "CODE_ACTION",
        "STRUCTURAL_EDIT",
        "SYMBOL_EDIT",
    }
    assert len(Capability) == 12
    assert Capability.CALL_HIERARCHY == "call_hierarchy"


SPEC_KINDS = {
    "Module", "Class", "Interface", "Struct", "Enum", "Trait", "Type",
    "Function", "Method", "Constructor", "Field", "Property", "Variable", "Constant",
    "Table", "Column", "Endpoint", "Message", "Service", "Node", "Edge",
    "Cell", "Tag", "Key", "Section",
}  # fmt: skip


def test_kind_vocabulary_matches_spec() -> None:
    assert {k.value for k in Kind} == SPEC_KINDS
    assert len(Kind) == 25


def test_kind_vocabulary_matches_spec_document() -> None:
    if not SPEC.exists():
        pytest.skip("spec document not available")
    text = SPEC.read_text(encoding="utf-8")
    m = re.search(r"^kind\s+=\s+(.*?);", text, re.MULTILINE | re.DOTALL)
    assert m, "kind rule not found in §6.1"
    in_spec = set(re.findall(r'"(\w+)"', m.group(1)))
    assert in_spec == {k.value for k in Kind}


def test_kind_parse_is_case_insensitive() -> None:
    assert Kind.parse("method") is Kind.METHOD
    assert Kind.parse("Method") is Kind.METHOD
    assert Kind.parse("CLASS") is Kind.CLASS
    assert Kind.METHOD.value.lower() == "method"
    with pytest.raises(ValueError):
        Kind.parse("File")
    with pytest.raises(ValueError):
        Kind("method")  # 厳密な変換は外部表現（先頭大文字）だけ


def test_lsp_status_values() -> None:
    assert {s.value for s in LspStatus} == {"ready", "indexing", "standby", "unavailable", "n/a"}


# --- Position / Range --------------------------------------------------------------------


def test_position_external_is_one_based() -> None:
    assert Position(0, 0).to_external() == (1, 1)
    assert Position(41, 8).to_external() == (42, 9)
    assert Position.from_external(42, 9) == Position(41, 8)
    assert Position.from_external(*Position(3, 7).to_external()) == Position(3, 7)


def test_position_validation_and_order() -> None:
    with pytest.raises(ValueError):
        Position(-1, 0)
    with pytest.raises(ValueError):
        Position(0, -1)
    with pytest.raises(ValueError):
        Position.from_external(0, 1)
    with pytest.raises(ValueError):
        Position.from_external(1, 0)
    assert Position(1, 5) < Position(2, 0)
    assert Position(1, 5) < Position(1, 6)
    assert sorted([Position(2, 0), Position(1, 9), Position(1, 2)]) == [
        Position(1, 2),
        Position(1, 9),
        Position(2, 0),
    ]


def test_range_external() -> None:
    r = Range(Position(39, 4), Position(114, 12))
    assert r.to_external() == {"start_line": 40, "start_col": 5, "end_line": 115, "end_col": 13}
    assert Range.from_external(40, 5, 115, 13) == r
    assert r.line_span_external() == (40, 115)


def test_range_line_span_with_end_at_column_zero() -> None:
    # 行全体を選ぶ LSP 流の範囲: 終端は次行の 0 列（排他的）なので最後の行は 1 つ前
    r = Range(Position(39, 0), Position(115, 0))
    assert r.line_span_external() == (40, 115)
    assert Range.from_line_span_external(40, 115) == r
    # 1 行だけの行範囲
    one = Range.from_line_span_external(7, 7)
    assert one.line_span_external() == (7, 7)
    # 空の範囲（同一行の 0 列）は 1 行
    assert Range(Position(5, 0), Position(5, 0)).line_span_external() == (6, 6)


def test_range_from_line_span_validation() -> None:
    with pytest.raises(ValueError):
        Range.from_line_span_external(0, 3)
    with pytest.raises(ValueError):
        Range.from_line_span_external(5, 4)


def test_range_contains() -> None:
    r = Range(Position(1, 2), Position(3, 4))
    assert r.contains(Position(1, 2))
    assert r.contains(Position(2, 0))
    assert r.contains(Position(3, 3))
    assert not r.contains(Position(3, 4))  # 半開区間
    assert not r.contains(Position(1, 1))
    empty = Range(Position(5, 5), Position(5, 5))
    assert empty.is_empty
    assert empty.contains(Position(5, 5))
    assert not empty.contains(Position(5, 6))
    assert r.contains_range(Range(Position(1, 2), Position(2, 0)))
    assert not r.contains_range(Range(Position(0, 0), Position(2, 0)))


def test_range_rejects_reversed() -> None:
    with pytest.raises(ValueError):
        Range(Position(2, 0), Position(1, 0))


# --- SymbolRecord ------------------------------------------------------------------------

# 仕様書 §6.2 の例そのもの
SPEC_SYMBOL_JSON = {
    "urn": "lk://app/src/services/payment.py#PaymentProcessor.process_charge",
    "scip_symbol": None,
    "path": "src/services/payment.py",
    "name": "process_charge",
    "qualified_name": "PaymentProcessor.process_charge",
    "kind": "Method",
    "modifiers": ["pub", "async"],
    "range": {"start_line": 40, "end_line": 115},
    "selection_range": {"line": 40, "start_col": 15, "end_col": 29},
    "signature": "(self, req: PaymentRequest) -> Receipt",
    "return_type": "Receipt",
    "doc_summary": "注文に対する決済を実行し領収書を発行する",
    "confidence_level": "exact",
}


def _symbol(**overrides: object) -> SymbolRecord:
    base: dict[str, object] = {
        "urn": Urn("app", "src/a.py", CodeSel("A.f")),
        "path": RelPath("src/a.py"),
        "name": "f",
        "qualified_name": "A.f",
        "kind": Kind.METHOD,
        "modifiers": ("pub",),
        "range": Range(Position(9, 4), Position(19, 20)),
        "selection": Range(Position(9, 8), Position(9, 9)),
        "signature": "(self)",
        "return_type": None,
        "doc_summary": None,
        "declared_in": "source",
        "confidence": Confidence.EXACT,
    }
    base.update(overrides)
    return SymbolRecord(**base)  # type: ignore[arg-type]


def test_symbol_record_spec_example_json_roundtrip() -> None:
    rec = SymbolRecord.from_json(SPEC_SYMBOL_JSON)
    assert rec.urn == parse("lk://app/src/services/payment.py#PaymentProcessor.process_charge")
    assert rec.kind is Kind.METHOD
    assert rec.modifiers == ("pub", "async")
    assert rec.confidence is Confidence.EXACT
    # 内部表現は 0 始まり
    assert rec.selection == Range(Position(39, 14), Position(39, 28))
    assert rec.range.line_span_external() == (40, 115)
    assert rec.to_json() == SPEC_SYMBOL_JSON
    assert SymbolRecord.from_json(rec.to_json()) == rec
    json.dumps(rec.to_json(), ensure_ascii=False)  # JSON として直列化できる


def test_symbol_record_object_roundtrip_line_granular() -> None:
    rec = _symbol(range=Range.from_line_span_external(10, 20))
    assert SymbolRecord.from_json(rec.to_json()) == rec


def test_symbol_record_to_json_loses_columns_of_range_only() -> None:
    rec = _symbol()  # range: (9,4)-(19,20)
    out = rec.to_json()
    assert out["range"] == {"start_line": 10, "end_line": 20}
    assert out["selection_range"] == {"line": 10, "start_col": 9, "end_col": 10}
    back = SymbolRecord.from_json(out)
    assert back.range == Range.from_line_span_external(10, 20)
    assert back.to_json() == out


def test_symbol_record_selection_none_and_declared_in() -> None:
    rec = _symbol(
        range=Range.from_line_span_external(10, 20),
        selection=None,
        declared_in="stub",
        scip_symbol="scip-python python pkg 1.0 A#f().",
    )
    out = rec.to_json()
    assert out["selection_range"] is None
    assert out["declared_in"] == "stub"
    assert out["scip_symbol"] == "scip-python python pkg 1.0 A#f()."
    assert SymbolRecord.from_json(out) == rec
    assert "declared_in" not in _symbol().to_json()
    assert SymbolRecord.from_json(SPEC_SYMBOL_JSON).declared_in == "source"


def test_symbol_record_from_json_minimal_required_keys() -> None:
    minimal = {
        "urn": "lk://app/a.py#f",
        "path": "a.py",
        "name": "f",
        "qualified_name": "f",
        "kind": "Function",
        "range": {"start_line": 1, "end_line": 1},
        "confidence_level": "structural",
    }
    rec = SymbolRecord.from_json(minimal)
    assert rec.modifiers == ()
    assert rec.selection is None
    assert rec.signature is None
    assert rec.confidence is Confidence.STRUCTURAL


def test_symbol_record_rejects_bad_json() -> None:
    bad_unknown = {**SPEC_SYMBOL_JSON, "extra": 1}
    with pytest.raises(ValueError):
        SymbolRecord.from_json(bad_unknown)
    with pytest.raises(ValueError):
        SymbolRecord.from_json({k: v for k, v in SPEC_SYMBOL_JSON.items() if k != "kind"})
    with pytest.raises(ValueError):
        SymbolRecord.from_json({**SPEC_SYMBOL_JSON, "kind": "method"})
    with pytest.raises(ValueError):
        SymbolRecord.from_json({**SPEC_SYMBOL_JSON, "confidence_level": "certain"})
    with pytest.raises(ValueError):
        SymbolRecord.from_json({**SPEC_SYMBOL_JSON, "urn": "not-a-urn"})
    with pytest.raises(ValueError):
        SymbolRecord.from_json({**SPEC_SYMBOL_JSON, "range": {"start_line": 0, "end_line": 3}})
    with pytest.raises(ValueError):
        SymbolRecord.from_json(
            {**SPEC_SYMBOL_JSON, "selection_range": {"line": 0, "start_col": 1, "end_col": 2}}
        )


def test_symbol_record_multiline_selection_cannot_be_serialized() -> None:
    rec = _symbol(selection=Range(Position(1, 0), Position(2, 3)))
    with pytest.raises(ValueError):
        rec.to_json()


def test_symbol_record_is_frozen_hashable_keyword_only() -> None:
    rec = _symbol()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.name = "g"  # type: ignore[misc]
    assert hash(rec) == hash(_symbol())
    with pytest.raises(TypeError):
        SymbolRecord(rec.urn, rec.path)  # type: ignore[misc]


# --- TargetRef ---------------------------------------------------------------------------

SPEC_TARGET_EXAMPLES = [
    {"urn": "lk://app/src/services/auth.py#AuthService.login"},
    {"path": "src/services/auth.py", "symbol_name": "AuthService.login"},
    {"path": "src/services/auth.py", "line": 42, "character": 9},
]


@pytest.mark.parametrize("data", SPEC_TARGET_EXAMPLES)
def test_target_ref_spec_examples_roundtrip(data: dict[str, object]) -> None:
    ref = TargetRef.from_json(data)
    assert ref.to_json() == data
    assert TargetRef.from_json(ref.to_json()) == ref


def test_target_ref_line_and_character_are_zero_based_internally() -> None:
    ref = TargetRef.from_json({"path": "a.py", "line": 42, "character": 9})
    assert ref.line == 41
    assert ref.character == 8
    assert TargetRef(path="a.py", line=0, character=0).to_json() == {
        "path": "a.py",
        "line": 1,
        "character": 1,
    }


def test_target_ref_variable_name() -> None:
    data = {"urn": "lk://app/a.py#f", "variable_name": "x"}
    assert TargetRef.from_json(data).to_json() == data


@pytest.mark.parametrize(
    "data",
    [
        {"path": "a.py", "line": 0},
        {"path": "a.py", "line": -3},
        {"path": "a.py", "line": 1, "character": 0},
        {"path": "a.py", "line": 1.5},
        {"path": "a.py", "line": True},
        {"path": "a.py", "line": "3"},
        {"path": "a.py", "character": 3},
        {"path": "a.py", "colon": 3},
        {"path": ""},
        {"urn": 5},
    ],
)
def test_target_ref_rejects_invalid(data: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        TargetRef.from_json(data)


def test_target_ref_resolution_form_precedence() -> None:
    # 仕様書 §5.3 規則 4: urn > line > symbol_name。残りは無視（warnings 用に名前を返す）
    both = TargetRef(urn="lk://a/x.py#f", path="x.py", line=3, symbol_name="f")
    assert both.resolution_form() == ("urn", ("line", "symbol_name"))
    assert TargetRef(path="x.py", line=3, symbol_name="f").resolution_form() == ("line", ("symbol_name",))
    assert TargetRef(path="x.py", symbol_name="f").resolution_form() == ("symbol_name", ())
    assert TargetRef(urn="lk://a/x.py#f").resolution_form() == ("urn", ())
    assert TargetRef(path="x.py").resolution_form() == ("path", ())


def test_target_ref_resolution_form_errors() -> None:
    with pytest.raises(ValueError):
        TargetRef().resolution_form()
    with pytest.raises(ValueError):
        TargetRef(symbol_name="f").resolution_form()
    with pytest.raises(ValueError):
        TargetRef(line=3).resolution_form()


# --- FileInfo / EngineChoice -------------------------------------------------------------


def test_file_info_and_engine_choice() -> None:
    fi = FileInfo(
        file_id=1,
        workspace_id="app",
        path=RelPath("src/a.py"),
        language="python",
        tier=Tier.T1,
        sha256="0" * 64,
        line_count=10,
        byte_size=100,
        is_test=False,
        confidence=Confidence.EXACT,
    )
    assert fi.tier is Tier.T1
    with pytest.raises(dataclasses.FrozenInstanceError):
        fi.file_id = 2  # type: ignore[misc]
    ec = EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL, lsp_status="standby")
    assert ec.lsp_progress is None
    assert ec.warning is None
    assert EngineChoice(engine="x", confidence=Confidence.EXACT).lsp_status == "n/a"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ec.engine = "y"  # type: ignore[misc]
