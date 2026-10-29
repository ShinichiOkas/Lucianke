"""core.records のテスト（設計書 ADR-4・ADR-5、仕様書 §5.5・§8.3）。"""

from __future__ import annotations

import dataclasses
import json

import pytest

from lucianke.core.model import Confidence, EngineChoice, Tier
from lucianke.core.records import Record, ResolutionMeta, ToolOutput, aggregate_confidence

E, S, H, L = Confidence.EXACT, Confidence.STRUCTURAL, Confidence.HEURISTIC, Confidence.LEXICAL_FALLBACK


def rec(conf: Confidence, key: str = "k", **kw: object) -> Record:
    base: dict[str, object] = {
        "kind": "symbol",
        "key": key,
        "priority": 0,
        "lines": ("  [Function] f @L1",),
        "data": {"name": "f"},
        "confidence": conf,
    }
    base.update(kw)
    return Record(**base)  # type: ignore[arg-type]


def test_aggregate_all_exact() -> None:
    level, breakdown, mixed, degraded = aggregate_confidence([rec(E)] * 12, E)
    assert level is E
    assert breakdown == {E: 12}
    assert breakdown == {"exact": 12}
    assert mixed is False
    assert degraded is False


def test_aggregate_mixed_takes_lowest_and_orders_breakdown() -> None:
    records = [rec(S), rec(E), rec(L), rec(E), rec(E), rec(H)]
    level, breakdown, mixed, degraded = aggregate_confidence(records, E)
    assert level is L
    assert list(breakdown.items()) == [(E, 3), (S, 1), (H, 1), (L, 1)]
    assert mixed is True
    assert degraded is True


def test_aggregate_uniformly_degraded_is_not_mixed() -> None:
    level, breakdown, mixed, degraded = aggregate_confidence([rec(S), rec(S)], E)
    assert (level, breakdown, mixed, degraded) == (S, {S: 2}, False, True)


def test_aggregate_structural_tool_is_not_degraded_at_structural() -> None:
    # T-12 structural_search_replace: 最高水準が structural。structural は降格ではない（仕様書 §8.3）
    level, breakdown, mixed, degraded = aggregate_confidence([rec(S), rec(S), rec(S)], S)
    assert level is S
    assert breakdown == {S: 3}
    assert mixed is False
    assert degraded is False


def test_aggregate_structural_tool_is_degraded_below_structural() -> None:
    _, _, mixed, degraded = aggregate_confidence([rec(S), rec(H)], S)
    assert mixed is True
    assert degraded is True


def test_aggregate_two_levels_exact_and_structural() -> None:
    level, _, mixed, degraded = aggregate_confidence([rec(E), rec(S)], E)
    assert (level, mixed, degraded) == (S, True, True)


def test_aggregate_empty() -> None:
    assert aggregate_confidence([], E) == (E, {}, False, False)
    assert aggregate_confidence([], S) == (S, {}, False, False)
    assert aggregate_confidence(iter(()), H) == (H, {}, False, False)


def test_aggregate_empty_with_empty_level() -> None:
    # 単語境界 grep の結果が 0 件でも「降格した答え」であることは伝えたい
    assert aggregate_confidence([], E, empty_level=L) == (L, {}, False, True)
    assert aggregate_confidence([], S, empty_level=S) == (S, {}, False, False)


def test_aggregate_accepts_one_shot_iterables_and_symbol_like_objects() -> None:
    class Item:
        def __init__(self, c: Confidence) -> None:
            self.confidence = c

    level, breakdown, _, _ = aggregate_confidence((Item(c) for c in (E, H)), E)
    assert level is H
    assert breakdown == {E: 1, H: 1}


def test_record_defaults_and_immutability() -> None:
    r = rec(E)
    assert r.group is None
    assert r.tokens is None
    assert r.shrink is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.priority = 5  # type: ignore[misc]
    r2 = r.with_tokens(42)
    assert r2.tokens == 42
    assert r.tokens is None
    assert r2.key == r.key


def test_record_shrink_contract() -> None:
    def shrink(budget: int) -> Record | None:
        if budget < 5:
            return None
        return rec(E, key="k", lines=("  [Function] f",))

    r = rec(E, shrink=shrink)
    assert r.shrink is not None
    assert r.shrink(1) is None
    smaller = r.shrink(10)
    assert smaller is not None
    assert smaller.lines == ("  [Function] f",)


def test_tool_output_defaults() -> None:
    eng = EngineChoice(engine="tree-sitter", confidence=S)
    out = ToolOutput(records=[rec(S)], max_confidence=S, engine=eng, page_unit="1 symbol")
    assert out.head_kv == ()
    assert dict(out.data_static) == {}
    assert out.summarize is None
    assert out.page_unit == "1 symbol"
    full = ToolOutput(
        records=(),
        head_kv=(("target", "x"),),
        data_static={"total": 0},
        max_confidence=E,
        engine=eng,
        page_unit="1 ref",
        summarize=lambda rest: f"{len(rest)} more",
    )
    assert full.summarize is not None
    assert full.summarize([rec(E)]) == "1 more"
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.page_unit = "x"  # type: ignore[misc]


def test_resolution_meta_to_json_matches_spec_example() -> None:
    # 仕様書 §5.5 の例
    meta = ResolutionMeta(
        confidence_level=E,
        is_degraded=False,
        tier=Tier.T1,
        detected_language="python",
        engine_used="pyright+tree-sitter",
        lsp_status="ready",
        lsp_progress=None,
        mixed=False,
        confidence_breakdown={E: 12},
        accuracy_warning=None,
    )
    assert meta.to_json() == {
        "confidence_level": "exact",
        "is_degraded": False,
        "tier": 1,
        "detected_language": "python",
        "engine_used": "pyright+tree-sitter",
        "lsp_status": "ready",
        "lsp_progress": None,
        "mixed": False,
        "confidence_breakdown": {"exact": 12},
        "accuracy_warning": None,
    }
    assert list(meta.to_json()) == [
        "confidence_level",
        "is_degraded",
        "tier",
        "detected_language",
        "engine_used",
        "lsp_status",
        "lsp_progress",
        "mixed",
        "confidence_breakdown",
        "accuracy_warning",
    ]


def test_resolution_meta_from_aggregate_and_json_serializable() -> None:
    level, breakdown, mixed, degraded = aggregate_confidence([rec(E), rec(E), rec(L)], E)
    meta = ResolutionMeta(
        confidence_level=level,
        is_degraded=degraded,
        tier=Tier.T4,
        detected_language="unknown (.cob)",
        engine_used="internal_ripgrep_word_boundary",
        lsp_status="n/a",
        mixed=mixed,
        confidence_breakdown=breakdown,
        accuracy_warning="字句一致です",
    )
    out = meta.to_json()
    assert out["confidence_level"] == "lexical_fallback"
    assert out["is_degraded"] is True
    assert out["mixed"] is True
    assert out["confidence_breakdown"] == {"exact": 2, "lexical_fallback": 1}
    assert list(out["confidence_breakdown"]) == ["exact", "lexical_fallback"]
    json.dumps(out, ensure_ascii=False)


def test_resolution_meta_drops_zero_counts() -> None:
    meta = ResolutionMeta(
        confidence_level=S,
        is_degraded=True,
        tier=Tier.T2,
        detected_language="python",
        engine_used="tree-sitter",
        lsp_status="standby",
        confidence_breakdown={E: 0, S: 4},
    )
    assert meta.to_json()["confidence_breakdown"] == {"structural": 4}
    assert meta.to_json()["lsp_progress"] is None
