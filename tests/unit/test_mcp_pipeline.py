"""``mcp/pipeline.py`` と ``mcp/registry.py`` の単体テスト（設計書 §7.1、§9.1）。

偽のツールを登録してパイプラインを直接呼ぶ。レンダラ・パッカー・カーソルコーデックは差し替え、並行して置き換え中の
本実装（budget・render）の中身に依存しない。既定の本実装を通す煙テストだけ、仕様書 §5.4 で固定の外形（ヘッダ行の先頭、
json の ``ok`` / ``tool`` / ``error.code``）を見る。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import MIN_MAX_TOKENS
from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput
from lucianke.mcp.budget import CursorState
from lucianke.mcp.pipeline import INTERNAL_MESSAGE, Pipeline, UnknownToolError
from lucianke.mcp.registry import CommonArgs, ToolRegistry, ToolSpec, build_input_schema
from lucianke.mcp.types import Page, Rendered, RenderInput

SECRET = "SECRET-INTERNAL-DETAIL"


class EchoArgs(CommonArgs):
    n: int = 1
    label: str | None = None


def make_output(n: int = 1) -> ToolOutput:
    recs = [
        Record(kind="kv", key=f"k{i}", priority=i, lines=(f"k{i}: v",), data={"i": i}, confidence=Confidence.EXACT)
        for i in range(n)
    ]
    return ToolOutput(
        records=recs,
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="none", confidence=Confidence.EXACT),
        page_unit="line",
    )


class Capture:
    """レンダラの代役。受け取った ``RenderInput`` を記録する。"""

    def __init__(self) -> None:
        self.inputs: list[RenderInput] = []

    def __call__(self, inp: RenderInput) -> Rendered:
        self.inputs.append(inp)
        return Rendered(text=f"R:{inp.call.format}:{inp.error.code.value if inp.error else 'ok'}", is_error=False)

    @property
    def last(self) -> RenderInput:
        return self.inputs[-1]


class FakeCodec:
    def __init__(self) -> None:
        self.decoded: list[dict[str, Any]] = []
        self.encoded: list[dict[str, Any]] = []

    def encode(self, *, tool: str, args: dict, offset: int, data_version: int, workspace_id: str | None) -> str:
        self.encoded.append(dict(tool=tool, args=args, offset=offset, data_version=data_version, workspace_id=workspace_id))
        return f"c{offset}"

    def decode(self, token: str, *, tool: str, args: dict, data_version: int, workspace_id: str | None) -> CursorState:
        self.decoded.append(dict(token=token, tool=tool, args=args, data_version=data_version, workspace_id=workspace_id))
        return CursorState(tool=tool, args_hash="x", offset=int(token[1:]), data_version=data_version, workspace_id=workspace_id)


class FakePacker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def pack(self, out: ToolOutput, *, max_tokens: int, offset: int = 0, reserved_tokens: int = 0, cursor_factory=None) -> Page:
        self.calls.append(dict(max_tokens=max_tokens, offset=offset))
        nxt = cursor_factory(offset + 1) if cursor_factory else None
        return Page(records=tuple(out.records[offset:]), next_cursor=nxt, summary=None, total_estimate=len(out.records),
                    remaining=0, used_tokens_est=1, max_tokens=max_tokens, truncated=False)  # fmt: skip


class Harness:
    """1 つの偽ツール ``echo_tool`` を載せたパイプライン。ハンドラの呼び出しを記録する。"""

    def __init__(self, *, behavior=None, **spec_overrides: Any) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.capture = Capture()
        self.log_records: list[logging.LogRecord] = []
        self.logger: logging.Logger = logging.getLogger("test.pipeline.unset")
        behavior = behavior or (lambda ctx, args: make_output(2))

        async def handler(ctx, args):
            self.calls.append((ctx, args))
            return behavior(ctx, args)

        fields: dict[str, Any] = dict(name="echo_tool", tid="T-XX", args_model=EchoArgs, handler=handler, default_max_tokens=1234)
        fields.update(spec_overrides)
        self.spec = ToolSpec(**fields)
        self.registry = ToolRegistry([self.spec])

    def pipeline(self, **kwargs: Any) -> Pipeline:
        records = self.log_records

        class _H(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger(f"test.pipeline.{id(self)}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers[:] = [_H()]
        self.logger = logger
        kwargs.setdefault("renderers", {"compact": self.capture, "json": self.capture})
        kwargs.setdefault("logger", logger)
        return Pipeline(self.registry, **kwargs)


# --- 正常系 ---------------------------------------------------------------------------------


async def test_success_fills_defaults_and_builds_render_input():
    h = Harness(records_key="things")
    rendered = await h.pipeline().call_tool("echo_tool", {"n": 3})
    assert rendered.is_error is False
    (ctx, args), = h.calls
    assert args.n == 3 and args.max_tokens == 1234  # ツール別の既定が埋まる
    assert ctx.args is args and ctx.call.tool == "echo_tool" and ctx.call.max_tokens == 1234
    assert ctx.call.revision == "WORKING_TREE" and ctx.call.format == "compact" and ctx.cursor_offset == 0
    inp = h.capture.last
    assert inp.error is None and inp.out is not None and inp.page is not None
    assert inp.records_key == "things" and inp.call.timing_ms >= 0
    assert len(inp.page.records) == 2


async def test_none_arguments_are_empty():
    h = Harness()
    assert (await h.pipeline().call_tool("echo_tool", None)).is_error is False


async def test_explicit_max_tokens_and_json_format():
    h = Harness()
    await h.pipeline().call_tool("echo_tool", {"max_tokens": MIN_MAX_TOKENS, "format": "json"})
    assert h.capture.last.call.max_tokens == MIN_MAX_TOKENS and h.capture.last.call.format == "json"


async def test_default_renderers_smoke():
    h = Harness()
    p = Pipeline(h.registry)  # 本実装のパッカー・レンダラ
    compact = await p.call_tool("echo_tool", {})
    assert compact.is_error is False and compact.text.startswith("# echo_tool ok")
    j = await p.call_tool("echo_tool", {"format": "json"})
    assert j.is_error is False and j.structured is not None and j.structured["ok"] is True and j.structured["tool"] == "echo_tool"
    assert json.loads(j.text)["tool"] == "echo_tool"


async def test_unknown_tool_raises_for_the_transport_layer():
    with pytest.raises(UnknownToolError) as ei:
        await Harness().pipeline().call_tool("nope", {})
    assert ei.value.name == "nope"


# --- 引数の検証（スキーマ違反も isError 結果。JSON-RPC エラーにしない） ------------------------------


async def test_unknown_property_is_invalid_argument_result():
    h = Harness()
    rendered = await h.pipeline().call_tool("echo_tool", {"bogus": 1})
    inp = h.capture.last
    assert inp.error is not None and inp.error.code is ErrorCode.E_INVALID_ARGUMENT
    assert inp.error.details["param"] == "bogus" and "unknown property" in inp.error.message
    assert rendered.is_error is True and h.calls == []


async def test_wrong_type_reports_the_parameter():
    h = Harness()
    await h.pipeline().call_tool("echo_tool", {"n": "abc"})
    assert h.capture.last.error.details["param"] == "n"  # type: ignore[union-attr]


async def test_multiple_violations_list_the_first_and_summarize_the_rest():
    h = Harness()
    await h.pipeline().call_tool("echo_tool", {"n": "x", "bogus": 1})
    err = h.capture.last.error
    assert err is not None and len(err.details["errors"]) == 2


async def test_invalid_argument_does_not_echo_values():
    h = Harness()
    await h.pipeline().call_tool("echo_tool", {"n": SECRET})
    err = h.capture.last.error
    assert err is not None and SECRET not in json.dumps(err.to_json())


@pytest.mark.parametrize("value", [0, 199, -5])
async def test_max_tokens_below_minimum(value):
    h = Harness()
    rendered = await h.pipeline().call_tool("echo_tool", {"max_tokens": value})
    err = h.capture.last.error
    assert err is not None and err.code is ErrorCode.E_INVALID_ARGUMENT and err.details["param"] == "max_tokens"
    assert rendered.is_error and h.calls == []


async def test_max_tokens_boundary_is_accepted():
    h = Harness()
    assert (await h.pipeline().call_tool("echo_tool", {"max_tokens": 200})).is_error is False


async def test_graph_format_on_non_graph_tool_is_invalid_argument():
    h = Harness(allows_graph=False)
    rendered = await h.pipeline().call_tool("echo_tool", {"format": "dot"})
    err = h.capture.last.error
    assert err is not None and err.code is ErrorCode.E_INVALID_ARGUMENT and err.details["param"] == "format"
    assert rendered.is_error and h.calls == []
    assert h.capture.last.call.format == "compact"  # 失敗は読める形式で返す


async def test_graph_format_without_renderer_is_unsupported():
    h = Harness(allows_graph=True)
    await h.pipeline().call_tool("echo_tool", {"format": "mermaid"})
    err = h.capture.last.error
    assert err is not None and err.code is ErrorCode.E_UNSUPPORTED
    assert err.details["reason"] == "format_not_implemented" and h.calls == []


async def test_graph_format_with_injected_renderer_works():
    h = Harness(allows_graph=True)
    graph = Capture()
    rendered = await h.pipeline(renderers={"compact": h.capture, "dot": graph}).call_tool("echo_tool", {"format": "dot"})
    assert rendered.is_error is False and graph.last.call.format == "dot"


async def test_writes_tool_rejects_other_revisions():
    h = Harness(writes=True)
    await h.pipeline().call_tool("echo_tool", {"revision": "HEAD"})
    err = h.capture.last.error
    assert err is not None and err.code is ErrorCode.E_INVALID_ARGUMENT and err.details["param"] == "revision"
    assert h.calls == []
    assert (await h.pipeline().call_tool("echo_tool", {"revision": "WORKING_TREE"})).is_error is False


async def test_tool_without_revision_support_warns_instead_of_failing():
    h = Harness(accepts_revision=False)
    rendered = await h.pipeline().call_tool("echo_tool", {"revision": "HEAD"})
    assert rendered.is_error is False
    assert len(h.capture.last.warnings) == 1 and "revision" in h.capture.last.warnings[0]
    await h.pipeline().call_tool("echo_tool", {})
    assert h.capture.last.warnings == ()


# --- マイルストーン ---------------------------------------------------------------------------


async def test_tool_of_a_later_milestone_is_unsupported():
    h = Harness(milestone="M2")
    rendered = await h.pipeline(current_milestone="M1").call_tool("echo_tool", {})
    err = h.capture.last.error
    assert rendered.is_error and err is not None and err.code is ErrorCode.E_UNSUPPORTED
    assert err.details == {"reason": "not_in_milestone", "milestone": "M2"} and h.calls == []
    assert (await h.pipeline(current_milestone="M2").call_tool("echo_tool", {})).is_error is False


# --- ハンドラの失敗 ---------------------------------------------------------------------------


async def test_luciankeerror_from_handler_becomes_error_result():
    def boom(ctx, args):
        raise LuciankeError(ErrorCode.E_TARGET_AMBIGUOUS, "3 matches", details={"param": "symbol_name"},
                            candidates=[{"urn": "u1", "signature": "s", "path": "a.py", "line": 1}])  # fmt: skip

    h = Harness(behavior=boom)
    rendered = await h.pipeline().call_tool("echo_tool", {"format": "json"})
    inp = h.capture.last
    assert rendered.is_error is True and inp.call.format == "json"
    assert inp.error is not None and inp.error.code is ErrorCode.E_TARGET_AMBIGUOUS
    assert inp.error.candidates[0]["urn"] == "u1" and inp.out is None and inp.page is None
    assert any(getattr(r, "error_code", None) == "E_TARGET_AMBIGUOUS" for r in h.log_records)


async def test_partial_result_is_packed_and_rendered_with_the_error():
    partial = make_output(3)

    def timeout(ctx, args):
        raise LuciankeError(ErrorCode.E_TIMEOUT, "too slow", partial=partial)

    h = Harness(behavior=timeout)
    rendered = await h.pipeline().call_tool("echo_tool", {})
    inp = h.capture.last
    assert rendered.is_error is True
    assert inp.out is partial and inp.page is not None and len(inp.page.records) == 3
    assert inp.error is not None and inp.error.details["partial"] is True


async def test_unexpected_exception_becomes_internal_error_without_leaking():
    def boom(ctx, args):
        raise RuntimeError(SECRET)

    h = Harness(behavior=boom)
    h.pipeline()  # ログの受け皿を用意する
    p = Pipeline(h.registry, logger=h.logger)  # 本実装のレンダラで外形を見る
    for fmt in ("compact", "json"):
        rendered = await p.call_tool("echo_tool", {"format": fmt})
        assert rendered.is_error is True
        assert SECRET not in rendered.text and "Traceback" not in rendered.text
        assert SECRET not in json.dumps(rendered.structured or {}, default=str)
        if fmt == "json":
            assert rendered.structured is not None and rendered.structured["error"]["code"] == "E_INTERNAL"
            log_ref = rendered.structured["error"]["details"]["log_ref"]
            logged = [r for r in h.log_records if getattr(r, "log_ref", None) == log_ref]
            assert logged and logged[0].exc_info is not None  # スタックはログだけ
            assert logged[0].request_id == log_ref
    assert INTERNAL_MESSAGE in rendered.text or rendered.structured["error"]["message"] == INTERNAL_MESSAGE


async def test_internal_error_through_capture_has_log_ref_and_fixed_message():
    def boom(ctx, args):
        raise RuntimeError(SECRET)

    h = Harness(behavior=boom)
    await h.pipeline().call_tool("echo_tool", {})
    err = h.capture.last.error
    assert err is not None and err.code is ErrorCode.E_INTERNAL and err.message == INTERNAL_MESSAGE
    assert set(err.details) == {"log_ref"} and SECRET not in json.dumps(err.to_json())


async def test_renderer_failure_on_success_is_internal_error():
    def bad_renderer(inp: RenderInput) -> Rendered:
        if inp.error is None:
            raise ValueError(SECRET)
        return Rendered(text=f"E:{inp.error.code.value}", is_error=True)

    h = Harness()
    rendered = await h.pipeline(renderers={"compact": bad_renderer}).call_tool("echo_tool", {})
    assert rendered.is_error is True and rendered.text == "E:E_INTERNAL"


async def test_renderer_failure_on_error_path_still_returns_a_result():
    def always_bad(inp: RenderInput) -> Rendered:
        raise ValueError(SECRET)

    h = Harness()
    rendered = await h.pipeline(renderers={"compact": always_bad}).call_tool("echo_tool", {"bogus": 1})
    assert rendered.is_error is True and "E_INVALID_ARGUMENT" in rendered.text and SECRET not in rendered.text
    assert rendered.structured is not None and rendered.structured["error"]["code"] == "E_INVALID_ARGUMENT"


async def test_cancellation_is_not_swallowed():
    def cancelled(ctx, args):
        raise asyncio.CancelledError

    h = Harness(behavior=cancelled)
    with pytest.raises(asyncio.CancelledError):
        await h.pipeline().call_tool("echo_tool", {})


# --- カーソルとパッカー -----------------------------------------------------------------------


async def test_cursor_is_decoded_and_handed_to_the_packer_and_handler():
    h = Harness()
    codec, packer = FakeCodec(), FakePacker()
    p = h.pipeline(cursor_codec=codec, packer=packer, data_version_provider=lambda: 7)
    await p.call_tool("echo_tool", {"cursor": "c1", "workspace_id": "ws1", "n": 2, "max_tokens": 900, "format": "json"})
    (dec,) = codec.decoded
    assert dec["tool"] == "echo_tool" and dec["data_version"] == 7 and dec["workspace_id"] == "ws1"
    # ページごとに変えてよい引数（cursor / max_tokens / format）は照合の対象にしない
    assert dec["args"]["n"] == 2 and not {"cursor", "max_tokens", "format"} & set(dec["args"])
    assert packer.calls == [{"max_tokens": 900, "offset": 1}]
    assert h.calls[0][0].cursor_offset == 1
    # next_cursor は同じ照合引数・同じデータ版で符号化される
    (enc,) = codec.encoded
    assert enc["offset"] == 2 and enc["data_version"] == 7 and enc["args"] == dec["args"]
    assert h.capture.last.page.next_cursor == "c2"  # type: ignore[union-attr]


async def test_cursor_errors_are_rendered_as_results():
    class Expired(FakeCodec):
        def decode(self, token, **kw):
            raise LuciankeError(ErrorCode.E_CURSOR_EXPIRED, "stale")

    h = Harness()
    rendered = await h.pipeline(cursor_codec=Expired()).call_tool("echo_tool", {"cursor": "c1"})
    assert rendered.is_error and h.capture.last.error.code is ErrorCode.E_CURSOR_EXPIRED  # type: ignore[union-attr]
    assert h.calls == []


async def test_no_cursor_never_touches_the_codec_decode():
    class NoDecode(FakeCodec):
        def decode(self, *a, **kw):
            raise AssertionError("decode must not be called without a cursor")

    h = Harness()
    assert (await h.pipeline(cursor_codec=NoDecode(), packer=FakePacker()).call_tool("echo_tool", {})).is_error is False


# --- ログ -------------------------------------------------------------------------------------


async def test_each_call_logs_event_request_id_and_timing():
    h = Harness()
    await h.pipeline().call_tool("echo_tool", {"n": 1, "label": "ここは本文っぽい長い文字列"})
    (rec,) = [r for r in h.log_records if getattr(r, "event", None) == "tool_call"]
    assert rec.request_id and rec.tool == "echo_tool" and rec.timing_ms >= 0
    assert rec.call_args == {"other_keys": ["label", "n"]}  # 識別子以外は値を残さない


# --- レジストリとスキーマ ----------------------------------------------------------------------


def _spec(model: type[CommonArgs] = EchoArgs, **kw: Any) -> ToolSpec:
    async def handler(ctx, args):
        return make_output()

    base: dict[str, Any] = dict(name="t", tid="T-XX", args_model=model, handler=handler, default_max_tokens=2000)
    base.update(kw)
    return ToolSpec(**base)


def test_registry_rejects_a_model_that_does_not_inherit_common_args():
    from pydantic import BaseModel, ConfigDict

    class Bare(BaseModel):
        model_config = ConfigDict(extra="forbid")

    with pytest.raises(TypeError, match="CommonArgs"):
        ToolRegistry([_spec(Bare)])  # type: ignore[arg-type]


def test_registry_rejects_a_model_that_allows_extra():
    from pydantic import ConfigDict

    class Loose(CommonArgs):
        model_config = ConfigDict(extra="allow")

    with pytest.raises(TypeError, match="forbid"):
        ToolRegistry([_spec(Loose)])


def test_registry_rejects_duplicates_and_bad_defaults():
    reg = ToolRegistry([_spec()])
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_spec())
    with pytest.raises(ValueError, match="default_max_tokens"):
        ToolRegistry([_spec(default_max_tokens=100)])
    with pytest.raises(ValueError, match="milestone"):
        ToolRegistry([_spec(milestone="M9")])


def test_input_schema_is_closed_and_carries_per_tool_defaults():
    reg = ToolRegistry([_spec(name="a", default_max_tokens=2000), _spec(name="b", default_max_tokens=3500, allows_graph=True)])
    a, b = reg.input_schema("a"), reg.input_schema("b")
    for schema in (a, b):
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["properties"]) >= {"workspace_id", "revision", "format", "max_tokens", "cursor", "n", "label"}
        assert schema["properties"]["max_tokens"]["minimum"] == MIN_MAX_TOKENS
        assert schema["properties"]["revision"]["default"] == "WORKING_TREE"
        assert schema["properties"]["format"]["default"] == "compact"
        assert "required" not in schema  # 共通パラメータはすべて省略可能
    assert a["properties"]["max_tokens"]["default"] == 2000 and b["properties"]["max_tokens"]["default"] == 3500
    assert a["properties"]["format"]["enum"] == ["compact", "json"]
    assert b["properties"]["format"]["enum"] == ["compact", "json", "dot", "mermaid"]


def test_input_schema_has_no_titles_and_unwraps_optionals():
    schema = build_input_schema(_spec())

    def titles(node: Any) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            found += [k for k in node if k == "title" and isinstance(node[k], str)]
            for k, v in node.items():
                found += titles(v) if k != "properties" else [t for sub in v.values() for t in titles(sub)]
        elif isinstance(node, list):
            found += [t for v in node for t in titles(v)]
        return found

    assert titles(schema) == []
    assert schema["properties"]["workspace_id"]["type"] == "string" and "anyOf" not in schema["properties"]["label"]


def test_input_schema_is_a_copy():
    reg = ToolRegistry([_spec()])
    reg.input_schema("t")["properties"].clear()
    assert reg.input_schema("t")["properties"]


def test_descriptors_keep_registration_order_and_read_only_flag():
    reg = ToolRegistry([_spec(name="r"), _spec(name="w", writes=True, description="edits")])
    d = reg.descriptors()
    assert [x.name for x in d] == ["r", "w"] and [x.read_only for x in d] == [True, False]
    assert d[1].description == "edits" and d[0].description == "r"  # 説明が無ければ名前
