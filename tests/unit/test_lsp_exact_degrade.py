"""T-06・T-07 の LSP ``exact`` 経路の降格は ``engine.selector.on_lsp_failure`` に従う（20e）。

``tools/_lsp_exact.degraded_choice`` は独自の規則を持たず、セレクターの 1 か所に寄せる。特に ``reason=indexing``
（LSP が 0 件と答えたが索引に参照がある、のとき両ツールが上げる）は ``lsp_status=indexing`` と ``lsp_indexing`` の警告になる。
"""

from __future__ import annotations

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Capability, Confidence, FileInfo, LspStatus, RelPath, Tier
from lucianke.engine.selector import WarningReason, on_lsp_failure, select, warning_text
from lucianke.tools import _lsp_exact as lx

CAPS = [Capability.REFERENCES, Capability.CALL_HIERARCHY]


def _route(cap: Capability) -> lx.LspRoute:
    file = FileInfo(
        file_id=1,
        workspace_id="ws",
        path=RelPath("a.py"),
        language="python",
        tier=Tier.T1,
        sha256="0" * 64,
        line_count=3,
        byte_size=30,
        is_test=False,
        confidence=Confidence.EXACT,
    )
    choice = select(
        file, cap, lsp_status=LspStatus.READY, lsp_progress=None, has_grammar=True, has_ctags=False
    )
    return lx.LspRoute(choice, object(), file, cap)  # type: ignore[arg-type]


def _err(reason: str) -> LuciankeError:
    return LuciankeError(ErrorCode.E_LSP_UNAVAILABLE, reason, details={"reason": reason})


@pytest.mark.parametrize("cap", CAPS)
@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        _err("indexing"),
        _err("standby"),
        _err("unavailable"),
        _err("disconnected"),
        _err("position_clamped"),
        _err("lsp_error"),
    ],
    ids=lambda e: type(e).__name__ + str(getattr(e, "details", {}).get("reason", "")),
)
def test_degraded_choice_is_exactly_the_selectors_rule(cap: Capability, exc: BaseException) -> None:
    route = _route(cap)
    assert route.exact
    assert lx.is_lsp_failure(exc)
    assert lx.degraded_choice(route, exc) == on_lsp_failure(route.choice, cap, exc)


@pytest.mark.parametrize("cap", CAPS)
def test_zero_hits_with_an_index_is_demoted_as_indexing(cap: Capability) -> None:
    """両ツールが「LSP が 0 件、索引に参照あり」で上げる ``reason=indexing`` の結果（``lsp_status`` と警告）。"""
    got = lx.degraded_choice(_route(cap), _err("indexing"))
    assert got.confidence is Confidence.STRUCTURAL
    assert got.lsp_status == LspStatus.INDEXING.value
    assert got.warning == warning_text(WarningReason.LSP_INDEXING)


@pytest.mark.parametrize("cap", CAPS)
def test_timeout_keeps_the_lsp_ready_with_the_timeout_warning(cap: Capability) -> None:
    got = lx.degraded_choice(_route(cap), TimeoutError())
    assert got.confidence is Confidence.STRUCTURAL and got.lsp_status == LspStatus.READY.value
    assert got.warning is not None and "ms 内に得られなかった" in got.warning


@pytest.mark.parametrize("cap", CAPS)
def test_standby_is_unavailable_not_indexing(cap: Capability) -> None:
    """選択のあとに LSP が落ちた競合は、セレクターの規則どおり ``unavailable``（以前の独自の降格は ``indexing`` にしていた）。"""
    assert lx.degraded_choice(_route(cap), _err("standby")).lsp_status == LspStatus.UNAVAILABLE.value


@pytest.mark.parametrize(
    "exc",
    [
        LuciankeError(ErrorCode.E_INVALID_ARGUMENT, "x", details={"param": "path"}),
        LuciankeError(ErrorCode.E_INVALID_ARGUMENT, "x"),
        LuciankeError(ErrorCode.E_LSP_UNAVAILABLE, "x", details={"reason": "rename_rejected"}),
        ValueError("bug"),
    ],
)
def test_other_errors_are_not_demotions(exc: BaseException) -> None:
    """呼び手の誤りや LSP の判断の結果は握らない。"""
    assert not lx.is_lsp_failure(exc)


def test_a_stale_index_position_is_demoted_like_position_clamped() -> None:
    """索引の位置がディスクのファイルの範囲を越える（索引が古い）と ``lsp.features`` は ``E_INVALID_ARGUMENT`` / ``position`` を上げる。
    利用者の誤りではないので、位置を LSP に送れなかった降格（``position_clamped``）として答える（以前の挙動を保つ）。"""
    stale = LuciankeError(ErrorCode.E_INVALID_ARGUMENT, "x", details={"param": "position"})
    assert lx.is_lsp_failure(stale)
    route = _route(Capability.REFERENCES)
    got = lx.degraded_choice(route, stale)
    assert got == on_lsp_failure(route.choice, route.capability, _err("position_clamped"))
    assert got.lsp_status == LspStatus.READY.value and got.confidence is Confidence.STRUCTURAL


@pytest.mark.parametrize("reason", ["indexing", "standby", "unavailable", "ready", "not_applicable", ""])
def test_down_reason_is_a_degrading_reason(reason: str) -> None:
    assert lx.is_lsp_failure(_err(lx._down_reason(reason)))  # noqa: SLF001
