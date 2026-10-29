"""engine.selector の LSP 失敗の降格（``on_lsp_failure`` / ``is_lsp_degradation``。設計書 ADR-6、§7.8.3、仕様書 §8.4）。

``lsp.features`` が上げる 2 種類の失敗（``TimeoutError`` と ``E_LSP_UNAVAILABLE``、``position_clamped`` を含む）を、
読み取り系の能力は右隣の列（構文木・索引の ``structural``）へ 1 段だけ降格させる。期待する降格先は設計書 §7.8.2 の表から
テスト側で別に起こしたリテラル。
"""

from __future__ import annotations

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Capability, Confidence, EngineChoice, FileInfo, LspStatus, RelPath, Tier
from lucianke.engine import selector
from lucianke.engine.selector import (
    ENGINE_LSP_CACHE,
    LSP_TIMEOUT_MS,
    POSITION_CLAMPED_WARNING,
    WaitPolicy,
    is_degraded,
    is_lsp_degradation,
    on_lsp_failure,
    on_lsp_timeout,
    select,
)

C = Capability
E, S = Confidence.EXACT, Confidence.STRUCTURAL

# 設計書 §7.8.2 の「列 2（索引中）」のエンジン。LSP を使う能力だけ
DOWN_ENGINE = {
    C.DEFINITION: "tree-sitter+imports",
    C.REFERENCES: "relations",
    C.IMPLEMENTATIONS: "relations(IMPLEMENTS, INHERITS)",
    C.CALL_HIERARCHY: "relations(CALLS)",
    C.RENAME: "lsp_unavailable",
    C.CODE_ACTION: "lsp_unavailable",
}
READ_CAPS = [C.DEFINITION, C.REFERENCES, C.IMPLEMENTATIONS, C.CALL_HIERARCHY]
STRUCTURAL = "LSP が利用できないため構文木のみで解決しました。型・オーバーロード・再エクスポート越しの参照は区別されていません。"


def _file() -> FileInfo:
    return FileInfo(
        file_id=1,
        workspace_id="ws",
        path=RelPath("a/b.py"),
        language="python",
        tier=Tier.T1,
        sha256="0" * 64,
        line_count=10,
        byte_size=100,
        is_test=False,
        confidence=Confidence.EXACT,
    )


def _ready(cap: Capability, **kw: object) -> EngineChoice:
    return select(
        _file(),
        cap,
        lsp_status=LspStatus.READY,
        lsp_progress=None,
        has_grammar=True,
        has_ctags=False,
        **kw,  # type: ignore[arg-type]
    )


def _lsp_error(reason: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_LSP_UNAVAILABLE, f"LSP is not available ({reason})", details={"reason": reason}
    )


# --- 降格の合図かどうか ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    ["indexing", "standby", "unavailable", "disconnected", "position_clamped", "lsp_error"],
)
def test_lsp_unavailable_reasons_are_degradations(reason: str) -> None:
    assert is_lsp_degradation(_lsp_error(reason))


def test_timeout_is_a_degradation() -> None:
    assert is_lsp_degradation(TimeoutError())


@pytest.mark.parametrize(
    "exc",
    [
        _lsp_error("prepare_rename_rejected"),  # LSP の判断の結果（降格ではない）
        _lsp_error("rename_rejected"),
        LuciankeError(ErrorCode.E_TARGET_NOT_FOUND, "no file"),
        LuciankeError(ErrorCode.E_INVALID_ARGUMENT, "bad position"),
        LuciankeError(ErrorCode.E_LSP_UNAVAILABLE, "no reason"),  # reason 無し
        ValueError("x"),
        RuntimeError("x"),
    ],
)
def test_other_errors_are_not_degradations_and_are_reraised(exc: BaseException) -> None:
    assert not is_lsp_degradation(exc)
    with pytest.raises(type(exc)) as got:
        on_lsp_failure(_ready(C.DEFINITION), C.DEFINITION, exc)
    assert got.value is exc


# --- TimeoutError は on_lsp_timeout と同じ -------------------------------------------------------


@pytest.mark.parametrize("cap", list(LSP_TIMEOUT_MS), ids=lambda c: c.value)
def test_timeout_equals_on_lsp_timeout(cap: Capability) -> None:
    choice = _ready(cap)
    assert on_lsp_failure(choice, cap, TimeoutError()) == on_lsp_timeout(choice, cap)


def test_timeout_warning_text() -> None:
    got = on_lsp_failure(_ready(C.DEFINITION), C.DEFINITION, TimeoutError())
    assert got.warning == "LSP の応答が 3000 ms 内に得られなかったため構文木で解決しました。"
    assert (got.engine, got.confidence, got.lsp_status) == ("tree-sitter+imports", S, "ready")


# --- position_clamped ----------------------------------------------------------------------


@pytest.mark.parametrize("cap", READ_CAPS, ids=lambda c: c.value)
def test_position_clamped_demotes_to_structural_and_keeps_lsp_ready(cap: Capability) -> None:
    got = on_lsp_failure(_ready(cap), cap, _lsp_error("position_clamped"))
    assert (got.engine, got.confidence) == (DOWN_ENGINE[cap], S)
    assert got.lsp_status == "ready"  # LSP は動いている。位置を送らなかっただけ
    assert got.lsp_progress is None
    assert got.warning == POSITION_CLAMPED_WARNING
    assert is_degraded(got.confidence, E)


def test_position_clamped_warning_is_a_structural_warning_without_the_status_hint() -> None:
    assert POSITION_CLAMPED_WARNING.endswith(
        "型・オーバーロード・再エクスポート越しの参照は区別されていません。"
    )
    assert "文字の境界" in POSITION_CLAMPED_WARNING and "要求を送らず" in POSITION_CLAMPED_WARNING
    assert "configure_workspace" not in POSITION_CLAMPED_WARNING  # LSP は使える状態なので案内しない


# --- LSP が選択の後に使えなくなった競合 ------------------------------------------------------------


@pytest.mark.parametrize("cap", READ_CAPS, ids=lambda c: c.value)
def test_indexing_reason_goes_to_the_indexing_column(cap: Capability) -> None:
    got = on_lsp_failure(_ready(cap), cap, _lsp_error("indexing"))
    assert (got.engine, got.confidence, got.lsp_status) == (DOWN_ENGINE[cap], S, "indexing")
    assert got.lsp_progress is None
    assert got.warning is not None
    assert got.warning.startswith(
        "LSP が起動・索引中のため構文木で解決しました。"
    )  # 進捗は不明なので括弧を出さない


@pytest.mark.parametrize("reason", ["standby", "unavailable", "disconnected"])
@pytest.mark.parametrize("cap", READ_CAPS, ids=lambda c: c.value)
def test_down_reasons_go_to_the_unavailable_column(cap: Capability, reason: str) -> None:
    got = on_lsp_failure(_ready(cap), cap, _lsp_error(reason))
    assert (got.engine, got.confidence, got.lsp_status) == (DOWN_ENGINE[cap], S, "unavailable")
    assert got.warning == STRUCTURAL + "`configure_workspace status` で LSP の状態を確認してください。"


def test_lsp_error_keeps_ready_and_uses_the_unavailable_text() -> None:
    got = on_lsp_failure(_ready(C.DEFINITION), C.DEFINITION, _lsp_error("lsp_error"))
    assert (got.engine, got.confidence, got.lsp_status) == ("tree-sitter+imports", S, "ready")
    assert got.warning is not None and got.warning.startswith(STRUCTURAL)


@pytest.mark.parametrize("cap", [C.RENAME, C.CODE_ACTION], ids=lambda c: c.value)
@pytest.mark.parametrize("reason", ["position_clamped", "indexing", "unavailable", "lsp_error"])
def test_rename_and_code_action_fall_to_lsp_unavailable_without_a_warning(
    cap: Capability, reason: str
) -> None:
    got = on_lsp_failure(_ready(cap), cap, _lsp_error(reason))
    assert got.engine == "lsp_unavailable" and got.engine in selector.NOT_PROVIDED_ENGINES
    assert got.warning is None  # ツールが E_LSP_UNAVAILABLE で返す


def test_lsp_name_is_dropped_from_the_demoted_engine() -> None:
    choice = _ready(C.DEFINITION, lsp_name="basedpyright")
    assert choice.engine == "basedpyright+tree-sitter"
    got = on_lsp_failure(choice, C.DEFINITION, _lsp_error("position_clamped"))
    assert got.engine == "tree-sitter+imports"


# --- 降格は 1 段だけ・LSP を待っていない選択には何もしない ----------------------------------------------


@pytest.mark.parametrize(
    "reason", ["position_clamped", "indexing", "unavailable", "lsp_error", "disconnected", "standby"]
)
def test_demotion_is_one_step_only(reason: str) -> None:
    once = on_lsp_failure(_ready(C.REFERENCES), C.REFERENCES, _lsp_error(reason))
    assert on_lsp_failure(once, C.REFERENCES, _lsp_error(reason)) is once
    assert on_lsp_failure(once, C.REFERENCES, TimeoutError()) is once


def test_no_op_when_lsp_was_not_asked() -> None:
    err = _lsp_error("position_clamped")
    for status in (LspStatus.INDEXING, LspStatus.UNAVAILABLE, LspStatus.STANDBY):
        choice = select(
            _file(), C.DEFINITION, lsp_status=status, lsp_progress=None, has_grammar=True, has_ctags=False
        )
        assert on_lsp_failure(choice, C.DEFINITION, err) is choice
    skipped = _ready(C.DEFINITION, wait_policy=WaitPolicy.NO_WAIT)
    assert on_lsp_failure(skipped, C.DEFINITION, err) is skipped


@pytest.mark.parametrize(
    "cap", [C.SKELETON, C.SEARCH, C.BODY, C.DIAGNOSTICS, C.STRUCTURAL_EDIT, C.SYMBOL_EDIT]
)
def test_no_op_for_capabilities_that_never_call_lsp(cap: Capability) -> None:
    choice = _ready(cap)
    assert on_lsp_failure(choice, cap, _lsp_error("position_clamped")) is choice


# --- 関連: LSP の結果を書き戻したもの（キャッシュ）から答えるエンジン名 ---------------------------------------


def test_lsp_cache_engine_name_is_shared_with_diagnostics() -> None:
    assert ENGINE_LSP_CACHE == "lsp-cache+tree-sitter"
    assert _ready(C.DIAGNOSTICS).engine == ENGINE_LSP_CACHE
