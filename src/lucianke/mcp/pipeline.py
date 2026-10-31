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
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ValidationError

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import SNIPPET_MAX_CHARS
from lucianke.core.records import ResolutionMeta
from lucianke.mcp.budget import CursorCodec, Packer
from lucianke.mcp.envelope import build_meta
from lucianke.mcp.fit import pack_and_render
from lucianke.mcp.registry import MILESTONES, ToolRegistry
from lucianke.mcp.render import compact as compact_render
from lucianke.mcp.render import graph as graph_render
from lucianke.mcp.render import json_out as json_render
from lucianke.mcp.types import CallInfo, Rendered, RenderInput
from lucianke.tools._base import GRAPH_FORMATS, WORKING_TREE, Services, ToolContext, ToolSpec
from lucianke.tools._building import building_of, building_warning

Renderer = Callable[[RenderInput], Rendered]

_log = logging.getLogger("lucianke.pipeline")
_SUMMARY_KEYS = ("path", "paths", "urn", "urns", "symbol_name", "revision", "format", "workspace_id", "max_tokens")
_CURSOR_EXCLUDED = frozenset({"cursor", "max_tokens", "format"})  # ページごとに変えてよい引数はカーソルの照合から外す
_FORMATS: tuple[str, ...] = ("compact", "json", *GRAPH_FORMATS)  # 失敗の描画で要求の形式を尊重できる形式（§5.4.3）
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
    return {
        "compact": compact_render.render,
        "json": json_render.render,
        "dot": graph_render.render_dot,
        "mermaid": graph_render.render_mermaid,
    }


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
        shutdown_guard: Callable[[], None] | None = None,
        ready_gate: Callable[[str, Mapping[str, Any]], Awaitable[object]] | None = None,
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
        self._ready_gate = ready_gate  # 起動直後の呼び出しが、索引の束の準備より先に着く競合を埋める（上限つきで待つ）。ツール名と生の引数を受ける（BUG-0042）
        self._shutdown_guard = shutdown_guard  # 終了が始まっていれば LuciankeError を投げる（lifecycle.make_shutdown_guard）

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
        error_code: str | None = None
        try:
            rendered, meta = await self._run(spec, raw, request_id, log, state, t0)
            confidence = str(meta.confidence_level)
        except LuciankeError as err:
            error_code = err.code.value
            rendered = self._render_failure(spec, state.call, err, t0, log)
        except Exception as exc:  # noqa: BLE001  想定外の例外は E_INTERNAL。スタックはログだけ（設計書 §9.1）
            log.error("internal error: %s", type(exc).__name__, exc_info=True,
                      extra={"event": "tool_internal_error", "error_code": ErrorCode.E_INTERNAL.value, "log_ref": request_id})  # fmt: skip
            error_code = ErrorCode.E_INTERNAL.value
            err = LuciankeError(ErrorCode.E_INTERNAL, INTERNAL_MESSAGE, details={"log_ref": request_id})
            rendered = self._render_failure(spec, state.call, err, t0, log)
        timing_ms = self._elapsed_ms(t0)
        log.info(
            "tool call finished",
            extra={"event": "tool_call", "timing_ms": timing_ms, "confidence": confidence,
                   "error_code": error_code, "call_args": summarize_args(raw), "workspace_id": state.call.workspace_id},
        )  # fmt: skip
        return rendered

    # --- 成功経路（設計書 §7.1.2 の 2〜7） -------------------------------------------------

    async def _run(
        self, spec: ToolSpec, raw: dict[str, Any], request_id: str, log: CallLogger, state: _CallState, t0: float
    ) -> tuple[Rendered, ResolutionMeta]:
        if self._shutdown_guard is not None:
            self._shutdown_guard()  # 終了処理中の新規 tools/call は E_UNSUPPORTED（details.reason=shutting_down）
        if self._ready_gate is not None:
            await self._ready_gate(spec.name, raw)
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

        # max_tokens に固定の下限は置かない（BUG-0063）。正の整数ならそのまま受け、前置きすら入らなければ出口
        # （fit.py・budget.py の 0 件経路）が必要な実測値を warnings で返す。pydantic の gt=0 で非正は既に弾く。
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

        # ワークスペースの解決（設計書 §7.1.2 手順 3）。省略は実在の ID にして、ヘッダ・カーソル・ハンドラが同じ値を見る
        workspace_id = self.services.resolve_workspace_id(args.workspace_id)
        if workspace_id != args.workspace_id:
            args = args.model_copy(update={"workspace_id": workspace_id})
            call = dataclasses.replace(call, workspace_id=workspace_id)
            state.call = call
        cursor_args = args.model_dump(mode="json", exclude=_CURSOR_EXCLUDED)
        data_version = self.data_version_provider()
        cursor_offset = 0
        if args.cursor is not None:
            cursor_offset = self.cursor_codec.decode(
                args.cursor, tool=spec.name, args=cursor_args, data_version=data_version, workspace_id=workspace_id
            ).offset

        ctx = ToolContext(call=call, services=self.services, args=args, log=log, request_id=request_id,
                          cursor_offset=cursor_offset)  # fmt: skip
        # 索引を読むツールは、走査（初回・差分）の途中なら進みつきの警告を添える（0 件と「まだ索引していない」を見分ける。BUG-0070）
        building = building_of(self.services, workspace_id) if spec.reads_index else None
        out = await spec.handler(ctx, args)
        if building is not None:
            warnings.append(building_warning(building_of(self.services, workspace_id) or building))
        warnings.extend(out.warnings)  # ハンドラの警告（§5.4.4 の失敗件数など）はパイプラインの警告の後ろへ
        if out.workspace_id is not None and out.workspace_id != call.workspace_id:
            call = dataclasses.replace(call, workspace_id=out.workspace_id)  # ハンドラが確定した ID（set_roots の登録後。BUG-0019）
            state.call = call

        minted: dict[str, int] = {}  # パッカーが見積もりで作ったカーソル → オフセット（表には入れない）

        def cursor_factory(offset: int) -> str:
            token = self.cursor_codec.peek(tool=spec.name, args=cursor_args, offset=offset,
                                           data_version=data_version, workspace_id=workspace_id)  # fmt: skip
            minted[token] = offset
            return token

        # 詰めて描く。ヘッダ・警告・head_kv・json の封筒もトークン予算に数え、``~used`` ・``used_tokens_est`` は描いた全文の
        # 数え直しに合わせる（仕様書 §5.6-4・§5.7。``mcp/fit.py``。BUG-0014・BUG-0017）
        fitted = pack_and_render(packer=self.packer, renderer=self.renderers[fmt], fmt=fmt, out=out, call=call,
                                 offset=cursor_offset, cursor_factory=cursor_factory, warnings=tuple(warnings),
                                 records_key=spec.records_key, timing_ms=lambda: self._elapsed_ms(t0))  # fmt: skip
        shown = minted.get(fitted.page.next_cursor) if fitted.page.next_cursor is not None else None
        if shown is not None:
            # 表に入れる（発行する）のは応答に出したカーソルだけ。見積もりで作った分まで入れると表の上限を無駄に使う（BUG-0115）
            self.cursor_codec.encode(tool=spec.name, args=cursor_args, offset=shown,
                                     data_version=data_version, workspace_id=workspace_id)  # fmt: skip
        return fitted.rendered, fitted.meta

    # --- 失敗経路 -----------------------------------------------------------------------

    def _render_failure(self, spec: ToolSpec, call: CallInfo, err: LuciankeError, t0: float, log: CallLogger) -> Rendered:
        """``LuciankeError`` を成功と同じ経路で描画する（``isError`` を立てる）。部分結果があれば詰めて添える。

        失敗は要求された形式で返す。dot / mermaid はコメント形式のヘッダとメッセージ（``// `` / ``%% ``。仕様書 §5.4.3）。
        グラフ非対応のツールに dot / mermaid が指定された ``E_INVALID_ARGUMENT`` も同じ（要求の形式のコメントで返す）。
        要求の形式のレンダラが無いとき（未知の形式・差し替えで外れているとき）だけ compact にする。
        """
        fmt = call.format if call.format in _FORMATS and call.format in self.renderers else "compact"
        call = dataclasses.replace(call, format=fmt, timing_ms=self._elapsed_ms(t0))
        renderer = self.renderers.get(fmt) or default_renderers()[fmt]
        if err.partial is not None:
            try:
                shown = LuciankeError(err.code, err.message, details={**err.details, "partial": True},
                                      candidates=err.candidates, partial=err.partial)  # fmt: skip
                fitted = pack_and_render(packer=self.packer, renderer=renderer, fmt=fmt, out=err.partial, call=call,
                                         offset=0, cursor_factory=None, warnings=(), records_key=spec.records_key,
                                         timing_ms=lambda: self._elapsed_ms(t0), error=shown)  # fmt: skip
                return dataclasses.replace(fitted.rendered, is_error=True)
            except Exception:  # noqa: BLE001  部分結果が詰められなくても、エラー自体は返す
                log.warning("could not pack partial result", exc_info=True, extra={"event": "partial_pack_failed"})
        try:
            rendered = renderer(RenderInput(call=call, meta=build_meta(None, None), out=None, page=None, error=err,
                                            records_key=spec.records_key))  # fmt: skip
            return dataclasses.replace(rendered, is_error=True)
        except Exception:  # noqa: BLE001  レンダラ自体の不具合でも、結果は必ず返す（SDK に例外を漏らさない）
            log.error("renderer failed on error result", exc_info=True, extra={"event": "render_failed"})
            return _minimal_error(spec.name, err)

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
            format=fmt if isinstance(fmt, str) and fmt in _FORMATS else "compact",
            max_tokens=max_tokens if isinstance(max_tokens, int) and max_tokens > 0
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


__all__ = ["INTERNAL_MESSAGE", "CallLogger", "Pipeline", "UnknownToolError", "invalid_argument", "summarize_args"]
