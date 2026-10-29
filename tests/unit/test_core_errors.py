"""core.errors のテスト（仕様書 §5.8）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError

SPEC = Path(__file__).resolve().parents[2] / "docs" / "Lucianke仕様書.md"

SPEC_CODES = {
    "E_INVALID_ARGUMENT",
    "E_WORKSPACE_NOT_CONFIGURED",
    "E_REVISION_NOT_FOUND",
    "E_TARGET_NOT_FOUND",
    "E_TARGET_AMBIGUOUS",
    "E_LSP_UNAVAILABLE",
    "E_PRECONDITION_FAILED",
    "E_SYNTAX_INVALID",
    "E_MUTATION_BLOCKED",
    "E_QUERY_REJECTED",
    "E_CURSOR_EXPIRED",
    "E_TIMEOUT",
    "E_UNSUPPORTED",
    "E_INTERNAL",
}


def test_error_codes_match_spec() -> None:
    assert {c.value for c in ErrorCode} == SPEC_CODES
    assert len(ErrorCode) == 14
    for c in ErrorCode:
        assert c.name == c.value
        assert c.value.startswith("E_")


def test_error_codes_match_spec_document() -> None:
    if not SPEC.exists():
        pytest.skip("spec document not available")
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("## 5.8 エラー分類")
    end = text.index("## 5.9", start)
    in_spec = set(re.findall(r"^\| `(E_[A-Z_]+)` \|", text[start:end], re.MULTILINE))
    assert in_spec == {c.value for c in ErrorCode}


def test_error_code_is_str() -> None:
    assert ErrorCode.E_TIMEOUT == "E_TIMEOUT"
    assert ErrorCode("E_INTERNAL") is ErrorCode.E_INTERNAL
    assert json.dumps({"code": ErrorCode.E_TIMEOUT}) == '{"code": "E_TIMEOUT"}'


def test_to_json_shape() -> None:
    err = LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT, "max_tokens は 200 以上", details={"param": "max_tokens"}
    )
    assert err.to_json() == {
        "code": "E_INVALID_ARGUMENT",
        "message": "max_tokens は 200 以上",
        "details": {"param": "max_tokens"},
        "candidates": [],
    }
    assert list(err.to_json()) == ["code", "message", "details", "candidates"]
    json.dumps(err.to_json(), ensure_ascii=False)


def test_defaults_and_attributes() -> None:
    err = LuciankeError(ErrorCode.E_TARGET_NOT_FOUND, "no such symbol")
    assert err.code is ErrorCode.E_TARGET_NOT_FOUND
    assert err.message == "no such symbol"
    assert err.details == {}
    assert err.candidates == []
    assert err.partial is None
    assert err.to_json()["details"] == {}
    assert err.to_json()["candidates"] == []


def test_candidates_for_ambiguous() -> None:
    cands = [{"urn": f"lk://app/a.py#f{i}", "signature": "()", "path": "a.py", "line": i} for i in range(3)]
    err = LuciankeError(ErrorCode.E_TARGET_AMBIGUOUS, "3 件に一致します", candidates=cands)
    assert err.to_json()["candidates"] == cands


def test_accepts_string_code_and_rejects_unknown() -> None:
    assert LuciankeError("E_TIMEOUT", "slow").code is ErrorCode.E_TIMEOUT
    with pytest.raises(ValueError):
        LuciankeError("E_BOGUS", "x")


def test_is_an_exception() -> None:
    with pytest.raises(LuciankeError) as ei:
        raise LuciankeError(ErrorCode.E_CURSOR_EXPIRED, "stale")
    assert ei.value.code is ErrorCode.E_CURSOR_EXPIRED
    assert str(ei.value) == "E_CURSOR_EXPIRED: stale"
    assert isinstance(ei.value, Exception)


def test_details_are_copied() -> None:
    d = {"param": "x"}
    err = LuciankeError(ErrorCode.E_INVALID_ARGUMENT, "m", details=d)
    d["param"] = "changed"
    assert err.details == {"param": "x"}
    out = err.to_json()
    out["details"]["param"] = "mutated"
    assert err.details == {"param": "x"}


def test_partial_is_kept_but_not_serialized() -> None:
    sentinel = object()
    err = LuciankeError(ErrorCode.E_TIMEOUT, "slow", details={"partial": True}, partial=sentinel)  # type: ignore[arg-type]
    assert err.partial is sentinel
    assert "partial" not in err.to_json()
