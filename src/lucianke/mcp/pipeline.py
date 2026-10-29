"""全ツール共通のパイプライン（設計書 §7.1.2、§9.1）。

``tools/call`` 1 回分の前後処理: 引数の検証 → 共通パラメータの検査 → カーソル復号 → ハンドラ → パッカー → 精度メタ →
レンダラ。``LuciankeError`` は同じ経路で ``isError`` の結果として描画し、想定外の例外は ``E_INTERNAL``（スタックはログだけ）にする。

* 未知のツール名だけは ``UnknownToolError``（``server.py`` が JSON-RPC エラーにする。プロトコル違反）。
* スキーマ違反の引数も JSON-RPC エラーではなく ``E_INVALID_ARGUMENT``（``details.param``）の ``isError`` 結果で返す
  （エージェントが自己修正できる。S-1 の判断。SDK は引数を検証しない）。
* ``BaseException``（キャンセルなど）は握りつぶさない。

SDK には依存しない。パッカー・カーソルコーデック・データ版・レンダラは注入できる（既定は本実装）。
"""

from __future__ import annotations

import dataclasses
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import MIN_MAX_TOKENS, SNIPPET_MAX_CHARS
from lucianke.core.records import ResolutionMeta
from lucianke.mcp.budget import CursorCodec, Packer
from lucianke.mcp.envelope import build_meta
from lucianke.mcp.registry import MILESTONES, ToolRegistry
from lucianke.mcp.render import compact as compact_render
from lucianke.mcp.render import json_out as json_render
from lucianke.mcp.types import CallInfo, Rendered, RenderInput
from lucianke.tools._base import GRAPH_FORMATS, WORKING_TREE, CommonArgs, Services, ToolContext, ToolSpec

Renderer = Callable[[RenderInput], Rendered]

_log = logging.getLogger("lucianke.pipeline")
_SUMMARY_KEYS = ("path", "paths", "urn", "urns", "symbol_name", "revision", "format", "workspace_id", "max_tokens")
_CURSOR_EXCLUDED = frozenset({"cursor", "max_tokens", "format"})  # ページごとに変えてよい引数はカーソルの照合から外す
INTERNAL_MESSAGE = "Internal error. Quote details.log_ref when reporting."


class UnknownToolError(LookupError):
    """``tools/call`` の名前が登録表に無い（プロトコル違反。JSON-RPC エラーにする）。"""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class CallLogger(logging.LoggerAdapter):
    """呼び出しごとの文脈（``request_id``・``tool``）を、呼び出し側の ``extra`` と合成して付ける。"""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        kwargs["extra"] = {**(self.extra or {}), **kwargs.get("extra", {})}
        return msg, kwargs


def default_renderers() -> dict[str, Renderer]:
    return {"compact": compact_render.render, "json": json_render.render}


def summarize_args(raw: Mapping[str, Any]) -> dict[str, Any]:
    """ログ用の引数要約（仕様書 §12.6）。パス・URN など識別子だけを 200 文字まで。コード本文などは載せない（キー名のみ）。"""

    def clip(value: Any) -> Any:
        if isinstance(value, str):
            return value if len(value) <= SNIPPET_MAX_CHARS else value[: SNIPPET_MAX_CHARS - 1] + "…"
        if isinstance(value, (list, tuple)):
            return [clip(v) for v in value[:5]]
        return value if isinstance(value, (int, float, bool)) or value is None else type(value).__name__

    summary = {k: clip(raw[k]) for k in _SUMMARY_KEYS if k in raw}
    other = sorted(k for k in raw if k not in _SUMMARY_KEYS)
    if other:
        summary["other_keys"] = other
    return summary


def invalid_argument(param: str, message: str, **details: Any) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, message, details={"param": param, **details})


def _from_validation_error(exc: ValidationError) -> LuciankeError:
    errors = exc.errors(include_url=False, include_context=False, include_input=False)

    def describe(err: Mapping[str, Any]) -> tuple[str, str]:
        param = ".".join(str(p) for p in err["loc"]) or "arguments"
        msg = "unknown property" if err["type"] == "extra_forbidden" else str(err["msg"])
        return param, f"{param}: {msg}"

    param, message = describe(errors[0])
    extra: dict[str, Any] = {}
    if len(errors) > 1:
        extra["errors"] = [dict(zip(("param", "message"), describe(e), strict=True)) for e in errors[:5]]
    return invalid_argument(param, message, **extra)


class Pipeline:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        services: Services | None = None,
        packer: Packer | None = None,
        cursor_codec: CursorCodec | None = None,
        data_version_provider: Callable[[], int] | None = None,
        renderers: Mapping[str, Renderer] | None = None,
        current_milestone: str = "M1",
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if current_milestone not in MILESTONES:
            raise ValueError(f"unknown milestone {current_milestone!r}")
        self.registry = registry
        self.services = services or Services()
        self.packer = packer or Packer()
        self.cursor_codec = cursor_codec or CursorCodec()
        self.data_version_provider = data_version_provider or (lambda: 0)
        self.renderers: dict[str, Renderer] = dict(renderers) if renderers is not None else default_renderers()
        self.current_milestone = current_milestone
        self._logger = logger or _log
        self._clock = clock

    # --- 入口 ---------------------------------------------------------------------------

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Rendered:
        spec = self.registry.get(name)
        if spec is None:
            raise UnknownToolError(name)
        raw: dict[str, Any] = dict(arguments or {})
        request_id = uuid.uuid4().hex[:12]
        log = CallLogger(self._logger, {"request_id": request_id, "tool": name})
        t0 = self._clock()
        state = _CallState(call=self._fallback_call(spec, raw))
        confidence: str | None = None
        try:
            rendered, meta = await self._run(spec, raw, request_id, log, state, t0)
            confidence = str(meta.confidence_level)
        except LuciankeError as err:
            rendered = self._render_failure(spec, state.call, err, t0, log)
        except Exception as exc:  # noqa: BLE001  想定外の例外は E_INTERNAL。スタックはログだけ（設計書 §9.1）
            log.error("internal error: %s", type(exc).__name__, exc_info=True,
                      extra={"event": "tool_internal_error", "error_code": ErrorCode.E_INTERNAL.value, "log_ref": request_id})  # fmt: skip
            err = LuciankeError(ErrorCode.E_INTERNAL, INTERNAL_MESSAGE, details={"log_ref": request_id})
            rendered = self._render_failure(spec, state.call, err, t0, log)
        timing_ms = self._elapsed_ms(t0)
        error_code = rendered.structured["error"]["code"] if rendered.is_error and rendered.structured else None
        log.info(
            "tool call finished",
            extra={"event": "tool_call", "timing_ms": timing_ms, "confidence": confidence,
                   "error_code": error_code, "args": summarize_args(raw), "workspace_id": state.call.workspace_id},
        )  # fmt: skip
        return rendered

    # --- 成功経路（設計書 §7.1.2 の 2〜7） -------------------------------------------------

    async def _run(
        self, spec: ToolSpec, raw: dict[str, Any], request_id: str, log: CallLogger, state: _CallState, t0: float
    ) -> tuple[Rendered, ResolutionMeta]:
        if MILESTONES.index(spec.milestone) > MILESTONES.index(self.current_milestone):
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"{spec.name} is planned for {spec.milestone} and is not available yet",
                details={"reason": "not_in_milestone", "milestone": spec.milestone},
            )
        try:
            args = spec.args_model.model_validate(raw)
        except ValidationError as exc:
            raise _from_validation_error(exc) from None

        if args.max_tokens is not None and args.max_tokens < MIN_MAX_TOKENS:
            raise invalid_argument("max_tokens", f"max_tokens: must be >= {MIN_MAX_TOKENS}", minimum=MIN_MAX_TOKENS)
        max_tokens = args.max_tokens if args.max_tokens is not None else spec.default_max_tokens
        args = args.model_copy(update={"max_tokens": max_tokens})
        call = CallInfo(tool=spec.name, revision=args.revision, workspace_id=args.workspace_id,
                        format=args.format, max_tokens=max_tokens)  # fmt: skip
        state.call = call

        fmt = args.format
        if fmt in GRAPH_FORMATS and not spec.allows_graph:
            raise invalid_argument("format", f"format: {fmt!r} is not supported by {spec.name} (use compact or json)")
        if spec.writes and args.revision != WORKING_TREE:
            raise invalid_argument("revision", f"revision: {spec.name} edits the working tree only (WORKING_TREE)")
        if fmt not in self.renderers:
            raise LuciankeError(ErrorCode.E_UNSUPPORTED, f"format {fmt!r} is not implemented yet",
                                details={"reason": "format_not_implemented", "format": fmt})  # fmt: skip
        warnings: list[str] = []
        if not spec.accepts_revision and args.revision != WORKING_TREE:
            warnings.append(f"revision {args.revision!r} ignored: {spec.name} has no revision support")

        workspace_id = args.workspace_id
        cursor_args = args.model_dump(mode="json", exclude=_CURSOR_EXCLUDED)
        data_version = self.data_version_provider()
        cursor_offset = 0
        if args.cursor is not None:
            cursor_offset = self.cursor_codec.decode(
                args.cursor, tool=spec.name, args=cursor_args, data_version=data_version, workspace_id=workspace_id
            ).offset

        ctx = ToolContext(call=call, services=self.services, args=args, log=log, request_id=request_id,
                          cursor_offset=cursor_offset)  # fmt: skip
        out = await spec.handler(ctx, args)

        def cursor_factory(offset: int) -> str:
            return self.cursor_codec.encode(tool=spec.name, args=cursor_args, offset=offset,
                                            data_version=data_version, workspace_id=workspace_id)  # fmt: skip

        page = self.packer.pack(out, max_tokens=max_tokens, offset=cursor_offset, cursor_factory=cursor_factory)
        meta = build_meta(out, page)
        call = dataclasses.replace(call, timing_ms=self._elapsed_ms(t0))
        rendered = self.renderers[fmt](RenderInput(call=call, meta=meta, out=out, page=page,
                                                   warnings=tuple(warnings), records_key=spec.records_key))  # fmt: skip
        return rendered, meta

    # --- 失敗経路 -----------------------------------------------------------------------

    def _render_failure(self, spec: ToolSpec, call: CallInfo, err: LuciankeError, t0: float, log: CallLogger) -> Rendered:
        """``LuciankeError`` を成功と同じ経路で描画する（``isError`` を立てる）。部分結果があれば詰めて添える。"""
        fmt = call.format if call.format in ("compact", "json") and call.format in self.renderers else "compact"
        call = dataclasses.replace(call, format=fmt, timing_ms=self._elapsed_ms(t0))
        out = page = None
        shown = err
        if err.partial is not None:
            try:
                page = self.packer.pack(err.partial, max_tokens=call.max_tokens, offset=0)
                out = err.partial
                shown = LuciankeError(err.code, err.message, details={**err.details, "partial": True},
                                      candidates=err.candidates, partial=err.partial)  # fmt: skip
            except Exception:  # noqa: BLE001  部分結果が詰められなくても、エラー自体は返す
                log.warning("could not pack partial result", exc_info=True, extra={"event": "partial_pack_failed"})
        try:
            meta = build_meta(out, page)
            renderer = self.renderers.get(fmt) or default_renderers()[fmt]
            rendered = renderer(RenderInput(call=call, meta=meta, out=out, page=page, error=shown,
                                            records_key=spec.records_key))  # fmt: skip
            return dataclasses.replace(rendered, is_error=True)
        except Exception:  # noqa: BLE001  レンダラ自体の不具合でも、結果は必ず返す（SDK に例外を漏らさない）
            log.error("renderer failed on error result", exc_info=True, extra={"event": "render_failed"})
            return _minimal_error(spec.name, shown)

    # --- 補助 ---------------------------------------------------------------------------

    def _elapsed_ms(self, t0: float) -> int:
        return max(0, round((self._clock() - t0) * 1000))

    @staticmethod
    def _fallback_call(spec: ToolSpec, raw: Mapping[str, Any]) -> CallInfo:
        """引数が検証前（または不正）でも失敗の描画に使える ``CallInfo``。信用できる値だけを拾う。"""
        revision = raw.get("revision")
        workspace_id = raw.get("workspace_id")
        fmt = raw.get("format")
        max_tokens = raw.get("max_tokens")
        return CallInfo(
            tool=spec.name,
            revision=revision if isinstance(revision, str) else WORKING_TREE,
            workspace_id=workspace_id if isinstance(workspace_id, str) else None,
            format=fmt if fmt in ("compact", "json") else "compact",
            max_tokens=max_tokens if isinstance(max_tokens, int) and max_tokens >= MIN_MAX_TOKENS
            and not isinstance(max_tokens, bool) else spec.default_max_tokens,
        )  # fmt: skip


@dataclasses.dataclass(slots=True)
class _CallState:
    call: CallInfo


def _minimal_error(tool: str, err: LuciankeError) -> Rendered:
    body = err.to_json()
    return Rendered(
        text=f"# {tool} error {err.code.value}\n! {err.message}\n",
        structured={"ok": False, "tool": tool, "error": body},
        is_error=True,
    )


__all__ = ["INTERNAL_MESSAGE", "CallLogger", "CommonArgs", "Pipeline", "UnknownToolError", "invalid_argument",
           "summarize_args"]  # fmt: skip
