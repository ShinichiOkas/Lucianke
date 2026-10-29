"""T-13 ``apply_code_action``（仕様書 §11.5 T-13、第 10 章）。LSP の Code Action（QuickFix・organizeImports・抽出など）を 1 つ適用し、
解消した診断を同じ応答で返す。

流れ（処理規約 1〜7）:

1. ``path`` を ``confine`` し、ファイルを読む。``line`` / ``end_line`` がファイルの外なら ``E_INVALID_ARGUMENT``。
2. **ID 指定**（``qf_`` で始まる。T-08 が発行した ``code_action_id``、またはこのツールが候補として発行した ID）: 台帳
   （``ctx.services.code_action_store``）から引く。未知は ``E_TARGET_NOT_FOUND``、発行位置の ``path`` / ``line`` と食い違えば
   ``E_INVALID_ARGUMENT``、発行後にファイルが変われば ``E_PRECONDITION_FAILED``（``details.reason="code_action_id_expired"``）。
3. **種別指定**: 言語の ``ready`` な LSP（Python は ``lint`` の ruff と ``types`` の basedpyright の両方）に ``textDocument/codeAction``
   （``only: [種別]``、範囲 ``line``〜``end_line``。QuickFix には範囲に重なる診断を ``context.diagnostics`` で渡す）を問い合わせる。
   0 件は ``E_TARGET_NOT_FOUND``、2 件以上は ``E_TARGET_AMBIGUOUS``（候補に新たに発番した ID を付ける。エージェントはその ID で呼び直す）。
   候補は ``isPreferred`` → 供給元（lint → types）→ 種別 → タイトル → 編集の順に決定的に並べ、同じ供給元・種別・タイトルのものは
   1 件にする（台帳は同じ範囲・種別・タイトルに同じ ID を返す。1 行に同じ規則の診断が複数あると、サーバーは同じアクションを診断の数だけ返す）。
4. ``edit`` が無ければ ``codeAction/resolve``（``features.code_actions`` が行う）。それでも ``command`` しか無いアクションは
   ``E_UNSUPPORTED``（``details.reason="command_only"``）。**サーバー側で任意のコマンドを実行しない（MUST NOT）。**
5. ``WorkspaceEdit`` を ``edit.workspace_edit.plan_from_workspace_edit`` で ``Plan`` に変換し、``EditTransaction``（第 10 章の 8 段）に渡す。
   作成・削除・（トランザクションが対応するまで）改名を含めば ``E_UNSUPPORTED``。
6. ``target_diagnostic_resolved``: ID 指定で、その ID が付いていた診断が分かっており、診断差分が ``complete`` のときだけ真偽値。
   種別指定・診断が分からない・``pending`` / ``not_computed`` は ``null``。
7. 適用するアクションは 1 回の呼び出しで 1 つ。

``ctx.services`` に求めるもの（duck typing。``tools`` は ``mcp`` を import できない。結線は合成ルート）:

* ``workspace(workspace_id)``・``lines``（compact の行の組み立て）: 必須（``ToolServices``）。
* ``lsp_pool``（``LspPool``）: 必須。無い・言語の LSP が無い・``ready`` でないときは ``E_LSP_UNAVAILABLE``。
* ``code_action_store``（``lsp.action_store.CodeActionStore``。``put`` / ``get`` だけを使う）: ID 指定に必須。T-08 と同じ台帳。
  無いときは ID 指定が ``E_TARGET_NOT_FOUND``、種別指定は候補が 1 件なら動くが、曖昧のときは ID を発行できない。
* ``edit_transaction(ws, *, lsp=handle)`` → ``EditTransaction``: あればこれを使う（``lsp`` を受けない定義なら ``edit_transaction(ws)``）。
  ``documents`` / ``diagnostics_hub`` には **アクションを提案した LSP サーバーのもの** を入れること（その版の診断を待つため）。
  無ければ ``locks`` / ``writer_index`` / ``journal_dir``（必須）と ``indexer`` / ``watcher`` / ``reader_pool`` / ``trees`` /
  ``has_ctags`` / ``lock_wait_ms``（任意）から組む（T-10 と同じ属性名）。``documents`` / ``diagnostics_hub`` はハンドルのものを使う。

実装で決めたこと（仕様書との差）:

* 範囲は ``(line, character → 既定は行の最初の非空白)`` から ``end_line``（既定 ``line``）の行末まで。
* 種別指定で ``isPreferred`` が 1 件だけでも曖昧として扱う（仕様どおり 2 件以上は ``E_TARGET_AMBIGUOUS``）。
* LSP が ``ready`` になるのを最大 ``LSP_READY_WAIT_MS`` 待つ（起動直後の呼び出しを救う。仕様は待ちを定めていない）。
* ``codeAction`` / ``resolve`` の応答が 5,000 ms を超えれば ``E_TIMEOUT``。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import (
    CANDIDATES_MAX,
    DIAGNOSTICS_WAIT_DEFAULT_MS,
    DIAGNOSTICS_WAIT_MAX_MS,
    MAX_FILE_BYTES,
)
from lucianke.core.model import Confidence, EngineChoice, LspStatus, Position, Range, RelPath, TargetRef, Tier
from lucianke.core.records import Record, ToolOutput
from lucianke.core.textpos import LineIndex, strip_bom
from lucianke.edit.plan import Plan
from lucianke.edit.transaction import EditResult, EditTransaction, PlanContext
from lucianke.edit.workspace_edit import plan_from_workspace_edit
from lucianke.lsp import features
from lucianke.lsp.pool import LspHandle, LspState
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._services import ToolServices, estimate_tokens, services_of
from lucianke.workspace.languages import HEAD_BYTES, detect
from lucianke.workspace.paths import confine, to_abs
from lucianke.workspace.types import Workspace

__all__ = ["SPEC", "ApplyCodeActionArgs", "handler"]

_log = logging.getLogger("lucianke.tools.t13")

ID_PREFIX = "qf_"
LSP_READY_WAIT_MS = 2000
"""起動直後の LSP が ``ready`` になるのを待つ上限。"""
CONTEXT_DIAGNOSTICS_WAIT_MS = 2000
"""QuickFix の問い合わせの前に、LSP が最新の内容の診断を出すのを待つ上限（診断が無いと QuickFix は返らない）。"""
_ROLE_RANK = {"lint": 0, "types": 1}
_REQUIRED_SERVICES = ("locks", "writer_index", "journal_dir")
_OPTIONAL_SERVICES = ("indexer", "watcher", "reader_pool", "trees", "has_ctags", "lock_wait_ms")
_SEVERITY_LETTER = {"error": "E", "warning": "W", "information": "I", "info": "I", "hint": "H"}
_SHA_SHOWN = 12


class ApplyCodeActionArgs(CommonArgs):
    path: str = Field(description="Workspace-relative path of the file.")
    line: int = Field(
        ge=1,
        description="1-based line. With a code_action_id it must be the line the id was issued for (the diagnostic's line).",
    )
    character: int | None = Field(
        default=None,
        ge=1,
        description="1-based column (code points). Default: the first non-blank character of the line.",
    )
    end_line: int | None = Field(
        default=None, ge=1, description="Last line of the range (refactor.extract etc.). Default: line."
    )
    action_kind_or_id: str = Field(
        min_length=1,
        description="A code_action_id from get_diagnostics (starts with 'qf_'), or an LSP CodeActionKind "
        "(quickfix, source.organizeImports, source.fixAll, refactor.extract, ...).",
    )
    dry_run: bool = Field(
        default=False, description="Do not write; return the diff and the syntax check only."
    )
    allow_heuristic_mutation: bool = Field(
        default=False, description="Accepted for compatibility; it has no effect (code actions need an LSP)."
    )
    expected_file_sha256: str | None = Field(
        default=None,
        description="sha256 of the file as you read it (64 hex, or a prefix of 12+). Mismatch is E_PRECONDITION_FAILED.",
    )
    diagnostics_wait_ms: int = Field(
        default=DIAGNOSTICS_WAIT_DEFAULT_MS,
        ge=0,
        le=DIAGNOSTICS_WAIT_MAX_MS,
        description="How long to wait for the language server's diagnostics after the write.",
    )


# --- 小さな部品 ---------------------------------------------------------------------------


def _invalid(message: str, **details: object) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, message, details=details)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _server_of(handle: LspHandle) -> str:
    return handle.server_name or handle.language_id


def _text_of(data: bytes) -> str:
    return strip_bom(data).decode("utf-8", "replace")


def _read_file(ws: Workspace, rel: RelPath) -> tuple[bytes, str, Tier]:
    """ファイルのバイト列・言語・Tier。無ければ ``E_TARGET_NOT_FOUND``、編集できない内容は ``E_UNSUPPORTED``。"""
    path = to_abs(ws, rel)
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"file too large to edit: {size} bytes > {MAX_FILE_BYTES}",
                details={"reason": "file_too_large", "path": str(rel), "size": size},
            )
        data = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND, f"file not found: {rel}", details={"path": str(rel)}
        ) from None
    det = detect(rel, data[:HEAD_BYTES])
    if not det.index_content:
        reason = "secret_file" if det.is_secret else "binary_file"
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED, f"{rel}: content is not editable ({reason})", details={"reason": reason}
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"{rel}: not a UTF-8 file",
            details={"reason": "non_utf8_file", "path": str(rel)},
        ) from None
    return data, det.language_id, det.tier


def _internal_range(index: LineIndex, args: ApplyCodeActionArgs) -> Range:
    """``line`` / ``character`` / ``end_line`` → 内部の ``Range``（0 始まり・コードポイント。列は行の長さに丸める）。"""
    count = index.content_line_count
    if args.line > count:
        raise _invalid(
            f"line {args.line} is beyond the end of the file ({count} lines)", param="line", lines=count
        )
    end_line = args.end_line if args.end_line is not None else args.line
    if end_line < args.line:
        raise _invalid("end_line must be >= line", param="end_line")
    if end_line > count:
        raise _invalid(
            f"end_line {end_line} is beyond the end of the file ({count} lines)",
            param="end_line",
            lines=count,
        )
    line0, end0 = args.line - 1, end_line - 1
    text = index.line_text(line0)
    if args.character is None:
        col = len(text) - len(text.lstrip())
        if col >= len(text):
            col = 0  # 空白だけの行
    else:
        col = min(args.character - 1, len(text))
    start = Position(line0, col)
    end = Position(end0, index.line_length(end0))
    if end < start:
        end = start
    return Range(start, end)


def _overlaps(a: Range, b: Range) -> bool:
    return a.start <= b.end and b.start <= a.end


def _kind_matches(action_kind: object, wanted: str) -> bool:
    kind = action_kind if isinstance(action_kind, str) and action_kind else "quickfix"
    return kind == wanted or kind.startswith(wanted + ".")


# --- LSP の選択 -----------------------------------------------------------------------------


def _unavailable(language_id: str, reason: str, message: str, **extra: object) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_LSP_UNAVAILABLE, message, details={"reason": reason, "language": language_id, **extra}
    )


async def _ready_handles(pool: Any, language_id: str, ws: Workspace) -> list[LspHandle]:
    """言語の ``ready`` な LSP（Code Action を公開しているものだけ）。lint → types の順。standby なら起動し、短く待つ。"""
    if pool is None:
        raise _unavailable(language_id, "no_pool", "no language server pool is configured")
    await pool.ensure_started(language_id, ws)
    roles = sorted({h.role for h in pool.handles(ws, language_id)})
    if not roles:
        raise _unavailable(language_id, "no_server", f"no language server is defined for {language_id!r}")
    await asyncio.gather(*(pool.wait_ready(language_id, ws, LSP_READY_WAIT_MS, role=r) for r in roles))
    ready = [
        h
        for h in pool.handles(ws, language_id)
        if h.state is LspState.READY and h.client is not None and h.client.is_connected
    ]
    providers = [
        h for h in ready if h.client is not None and h.client.server_capabilities.get("codeActionProvider")
    ]
    if not providers:
        states = {_server_of(h): h.state.value for h in pool.handles(ws, language_id)}
        reason = "no_code_action_provider" if ready else "not_ready"
        raise _unavailable(
            language_id,
            reason,
            f"no ready language server offers code actions for {language_id!r}",
            servers=states,
        )
    return sorted(providers, key=lambda h: (_ROLE_RANK.get(h.role, 2), _server_of(h)))


async def _context_diagnostics(
    handle: LspHandle, rel: RelPath, data: bytes, index: LineIndex, rng: Range
) -> list[dict[str, Any]]:
    """範囲に重なる LSP の診断（QuickFix の ``context.diagnostics``）。最新の内容を LSP に見せ、診断が追いつくのを待つ。"""
    client = handle.client
    assert client is not None
    language_id = handle.defn.document_language_id if handle.defn is not None else handle.language_id
    try:
        version = handle.documents.sync(rel, _text_of(data), language_id)
    except ConnectionError:
        return []
    sent_at = handle.documents.sent_at_of(rel)
    await handle.diagnostics.wait_for(
        rel,
        min_version=version,
        sent_at=sent_at if sent_at is not None else 0.0,
        timeout_ms=CONTEXT_DIAGNOSTICS_WAIT_MS,
    )
    entry = handle.diagnostics.get(rel)
    if entry is None:
        return []
    out: list[dict[str, Any]] = []
    for raw in entry.diagnostics:
        raw_range = raw.get("range")
        if not isinstance(raw_range, Mapping):
            continue
        try:
            drange = client.from_lsp_range(index, raw_range)
        except (ValueError, KeyError, TypeError):
            continue
        if _overlaps(drange, rng):
            out.append(dict(raw))
    return out


async def _query(
    handle: LspHandle,
    rel: RelPath,
    rng: Range,
    *,
    only: Sequence[str],
    diagnostics: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    try:
        return await features.code_actions(handle, rel, rng, only=only, diagnostics=diagnostics)
    except TimeoutError:
        raise LuciankeError(
            ErrorCode.E_TIMEOUT,
            f"{_server_of(handle)} did not answer textDocument/codeAction in time",
            details={"reason": "code_action_timeout", "server": _server_of(handle)},
        ) from None


# --- 選ばれたアクション ---------------------------------------------------------------------


@dataclass(slots=True)
class _Chosen:
    handle: LspHandle
    action: dict[str, Any]
    action_id: str | None
    diagnostic: Mapping[str, Any] | None
    from_id: bool
    target_path: RelPath
    is_preferred: bool = False

    @property
    def kind(self) -> str:
        return str(self.action.get("kind") or "quickfix")

    @property
    def title(self) -> str:
        return str(self.action.get("title") or "")

    @property
    def server(self) -> str:
        return _server_of(self.handle)


def _command_only(chosen_title: str, action: Mapping[str, Any]) -> LuciankeError:
    command = action.get("command")
    name = command.get("command") if isinstance(command, Mapping) else None
    return LuciankeError(
        ErrorCode.E_UNSUPPORTED,
        f"the code action {chosen_title!r} has no edit, only a command; Lucianke does not run server commands",
        details={"reason": "command_only", "title": chosen_title, "command": name},
    )


def _lsp_range_of(handle: LspHandle, index: LineIndex, rng: Range) -> dict[str, Any]:
    assert handle.client is not None
    return handle.client.to_lsp_range(index, rng)


def _put(
    store: Any,
    *,
    rel: RelPath,
    sha: str,
    handle: LspHandle,
    action: Mapping[str, Any],
    lsp_range: Mapping[str, Any],
    diagnostic: Mapping[str, Any] | None,
) -> str | None:
    if store is None:
        return None
    stored = store.put(
        path=rel,
        sha256=sha,
        language_id=handle.language_id,
        server=_server_of(handle),
        action=action,
        range=lsp_range,
        diagnostic=diagnostic,
    )
    return str(stored.id)


def _diagnostic_for(
    action: Mapping[str, Any], context: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """アクションが直す診断。アクションが ``diagnostics`` を持てばその先頭、無ければ範囲の診断がちょうど 1 件のときのそれ。"""
    listed = action.get("diagnostics")
    if isinstance(listed, Sequence) and listed and isinstance(listed[0], Mapping):
        return listed[0]
    return context[0] if len(context) == 1 else None


def _candidate_json(c: _Chosen, rel: RelPath, line: int) -> dict[str, Any]:
    """``E_TARGET_AMBIGUOUS`` の候補。仕様の ``{id, title, kind}`` に、compact の候補行（``urn`` / ``signature`` / ``path`` /
    ``line``）の材料と供給元を足す。"""
    return {
        "id": c.action_id,
        "title": c.title,
        "kind": c.kind,
        "server": c.server,
        "is_preferred": c.is_preferred,
        "urn": c.action_id or "-",
        "signature": f'{c.kind} "{c.title}"',
        "path": str(rel),
        "line": line,
    }


async def _choose_by_id(
    store: Any, args: ApplyCodeActionArgs, rel: RelPath, sha: str, index: LineIndex, pool: Any, ws: Workspace,
    language_id: str,
) -> _Chosen:  # fmt: skip
    stored = store.get(args.action_kind_or_id) if store is not None else None
    if stored is None:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"unknown code action id {args.action_kind_or_id!r} (ids are kept for the session only; call get_diagnostics again)",
            details={"reason": "unknown_code_action_id", "id": args.action_kind_or_id},
        )
    issued_line = int(stored.range["start"]["line"]) + 1
    if str(stored.path) != str(rel) or issued_line != args.line:
        raise _invalid(
            f"code action id {stored.id} was issued for {stored.path}:{issued_line}, not {rel}:{args.line}",
            reason="id_position_mismatch",
            id=stored.id,
            issued_path=str(stored.path),
            issued_line=issued_line,
        )
    if stored.sha256 != sha:
        raise LuciankeError(
            ErrorCode.E_PRECONDITION_FAILED,
            f"the file changed after code action id {stored.id} was issued; call get_diagnostics again",
            details={
                "reason": "code_action_id_expired",
                "id": stored.id,
                "path": str(rel),
                "expected": stored.sha256,
                "actual": sha,
            },
        )
    handles = await _ready_handles(pool, stored.language_id or language_id, ws)
    handle = next((h for h in handles if _server_of(h) == stored.server), None)
    if handle is None:
        raise _unavailable(
            language_id,
            "server_not_ready",
            f"{stored.server} (the server that offered code action {stored.id}) is not ready",
            server=stored.server,
        )
    action = dict(stored.action)
    if action.get(features.COMMAND_ONLY_KEY):
        raise _command_only(stored.title, action)
    if action.get("edit") is None:
        action = await _re_resolve(handle, rel, stored, index)
        if action.get("edit") is None:
            raise _command_only(stored.title, action)
    return _Chosen(
        handle, action, str(stored.id), stored.diagnostic, True, rel, bool(action.get("isPreferred"))
    )


async def _re_resolve(handle: LspHandle, rel: RelPath, stored: Any, index: LineIndex) -> dict[str, Any]:
    """台帳のアクションに ``edit`` が無いとき、同じ範囲で問い合わせ直して（``codeAction/resolve`` を含む）同じタイトルを探す。"""
    assert handle.client is not None
    rng = handle.client.from_lsp_range(index, stored.range)
    context = [stored.diagnostic] if stored.diagnostic is not None else []
    for action in await _query(handle, rel, rng, only=[stored.kind], diagnostics=context):
        if action.get("title") == stored.title and _kind_matches(action.get("kind"), stored.kind):
            return action
    return dict(stored.action)


def _unique_candidates(found: Sequence[_Chosen]) -> list[_Chosen]:
    """候補を決定的に並べ、同じ供給元・種別・タイトルのものを 1 件にする（BUG-0041）。

    台帳（``CodeActionStore``）は同じ（ファイル・内容・供給元・範囲・種別・タイトル）に同じ ID を返すので、これが「同じアクション」。
    1 行に同じ規則の診断が複数ある（``import os, sys`` の F401 が ``os`` と ``sys`` の 2 つ）と、サーバーは診断ごとに同じタイトルの
    アクションを返すため、重複を除かないと同じ ID・タイトルの候補が並び、件数も重複の数だけ増える。並びはサーバーが診断を返す順に
    依らないよう、最後に編集の中身で決める。先頭を残し、編集が違うものを落としたときはデバッグログに残す。
    """
    ordered = sorted(
        found,
        key=lambda c: (
            not c.is_preferred,
            _ROLE_RANK.get(c.handle.role, 2),
            c.kind,
            c.title,
            _server_of(c.handle),
            json.dumps(c.action.get("edit"), sort_keys=True, ensure_ascii=False, default=str),
        ),
    )
    seen: dict[tuple[str, str, str], _Chosen] = {}
    for c in ordered:
        key = (_server_of(c.handle), c.kind, c.title)
        kept = seen.setdefault(key, c)
        if kept is not c and kept.action.get("edit") != c.action.get("edit"):
            _log.debug("dropped a code action with the same title but a different edit: %r", c.title)
    return list(seen.values())


async def _choose_by_kind(
    store: Any, args: ApplyCodeActionArgs, rel: RelPath, data: bytes, sha: str, index: LineIndex, rng: Range,
    pool: Any, ws: Workspace, language_id: str,
) -> _Chosen:  # fmt: skip
    wanted = args.action_kind_or_id
    handles = await _ready_handles(pool, language_id, ws)
    found: list[_Chosen] = []
    last_error: LuciankeError | None = None
    answered = 0
    for handle in handles:
        context = (
            await _context_diagnostics(handle, rel, data, index, rng) if wanted.startswith("quickfix") else []
        )
        try:
            actions = await _query(handle, rel, rng, only=[wanted], diagnostics=context)
        except LuciankeError as exc:
            if exc.code is not ErrorCode.E_LSP_UNAVAILABLE:
                raise
            last_error = exc
            continue
        answered += 1
        for action in actions:
            if (
                action.get("disabled")
                or not action.get("title")
                or not _kind_matches(action.get("kind"), wanted)
            ):
                continue
            found.append(
                _Chosen(
                    handle,
                    action,
                    None,
                    _diagnostic_for(action, context),
                    False,
                    rel,
                    bool(action.get("isPreferred")),
                )
            )
    if not answered and last_error is not None:
        raise last_error
    found = _unique_candidates(found)
    if not found:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"no code action of kind {wanted!r} is offered at {rel}:{args.line}",
            details={"reason": "no_code_action", "kind": wanted, "path": str(rel), "line": args.line},
        )
    for c in found:  # 1 件でも ID を発行する（応答の action=… が次の呼び出しで使える）
        c.action_id = _put(
            store, rel=rel, sha=sha, handle=c.handle, action=c.action, lsp_range=_lsp_range_of(c.handle, index, rng),
            diagnostic=c.diagnostic,
        )  # fmt: skip
    if len(found) > 1:
        raise LuciankeError(
            ErrorCode.E_TARGET_AMBIGUOUS,
            f"{len(found)} code actions of kind {wanted!r} are offered at {rel}:{args.line}; "
            "call again with the id of one of the candidates",
            details={"reason": "ambiguous_code_action", "kind": wanted},
            candidates=[_candidate_json(c, rel, args.line) for c in found[:CANDIDATES_MAX]],
        )
    only = found[0]
    if only.action.get(features.COMMAND_ONLY_KEY) or only.action.get("edit") is None:
        raise _command_only(only.title, only.action)
    return only


# --- トランザクション -----------------------------------------------------------------------


def _accepts_lsp(factory: Any) -> bool:
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return False
    return "lsp" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _transaction(svc: ToolServices, ws: Workspace, handle: LspHandle) -> EditTransaction:
    factory = getattr(svc, "edit_transaction", None)
    if callable(factory):
        return factory(ws, lsp=handle) if _accepts_lsp(factory) else factory(ws)  # type: ignore[no-any-return]
    missing = [name for name in _REQUIRED_SERVICES if getattr(svc, name, None) is None]
    if missing:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            "editing is not available in this build (the edit services are not wired)",
            details={"reason": "edit_services_missing", "missing": missing},
        )
    optional = {
        name: getattr(svc, name) for name in _OPTIONAL_SERVICES if getattr(svc, name, None) is not None
    }
    return EditTransaction(
        ws,
        locks=svc.locks,  # type: ignore[attr-defined]
        writer_index=svc.writer_index,  # type: ignore[attr-defined]
        journal_dir=svc.journal_dir,  # type: ignore[attr-defined]
        documents=handle.documents,
        diagnostics_hub=handle.diagnostics,
        **optional,
    )


def _plan_builder(ws: Workspace, chosen: _Chosen, rel: RelPath, sha: str) -> Any:
    """``WorkspaceEdit`` → ``Plan`` を、トランザクションの解決の直後（ファイルを読んだ直後）に行うビルダー。"""
    client = chosen.handle.client
    assert client is not None
    edit = chosen.action["edit"]

    async def build(ctx: PlanContext) -> Plan:
        if ctx.sha256 != sha:
            raise LuciankeError(
                ErrorCode.E_PRECONDITION_FAILED,
                f"file changed since it was read: {rel}",
                details={"path": str(rel), "expected": sha, "actual": ctx.sha256},
            )
        return await plan_from_workspace_edit(ws, edit, encoding=client.negotiated_encoding.value)

    return build


# --- 診断の突き合わせ -----------------------------------------------------------------------


def _same_diag(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """LSP の診断どうし（編集前に保持していたものと、編集後のハブの診断）。位置は編集でずれるので使わない。"""
    return (
        str(a.get("code") or "") == str(b.get("code") or "")
        and str(a.get("message") or "") == str(b.get("message") or "")
        and str(a.get("source") or "").lower() == str(b.get("source") or "").lower()
    )


def _resolved_match(obj: Mapping[str, Any], raw: Mapping[str, Any], server: str) -> bool:
    """診断差分の ``resolved[]`` の 1 件が ``raw``（ID が付いていた診断）か。"""
    sources = {str(raw.get("source") or "").lower(), server.lower()} - {""}
    return (
        str(obj.get("code") or "") == str(raw.get("code") or "")
        and str(obj.get("message") or "") == str(raw.get("message") or "")
        and (not sources or str(obj.get("source") or "").lower() in sources)
    )


def _target_resolved(chosen: _Chosen, result: EditResult) -> bool | None:
    """``data.target_diagnostic_resolved``（モジュールの docstring の 6）。"""
    raw = chosen.diagnostic
    delta = result.new_diagnostics
    if not chosen.from_id or raw is None or not result.applied or delta.status != "complete":
        return None
    edited = {str(c.path) for c in result.files_changed}
    if str(chosen.target_path) in edited:
        entry = chosen.handle.diagnostics.get(chosen.target_path)
        if entry is not None:  # 直後の get_diagnostics が見るものと同じ（ハブの最新）に、その診断が無いか
            return not any(_same_diag(d, raw) for d in entry.diagnostics)
    return any(_resolved_match(o, raw, chosen.server) for o in delta.resolved)


# --- 応答の整形 -----------------------------------------------------------------------------


def _diff_counts_by_path(diff: str) -> dict[str, tuple[int, int]]:
    out: dict[str, list[int]] = {}
    current: list[int] | None = None
    rows = diff.splitlines()
    for i, row in enumerate(rows):
        if row.startswith("--- a/") and i + 1 < len(rows) and rows[i + 1].startswith("+++ b/"):
            current = out.setdefault(row[len("--- a/") :], [0, 0])
        elif current is not None and row.startswith("+") and not row.startswith("+++ b/"):
            current[0] += 1
        elif current is not None and row.startswith("-") and not row.startswith("--- a/"):
            current[1] += 1
    return {p: (v[0], v[1]) for p, v in out.items()}


def _diag_line(svc: ToolServices, obj: Mapping[str, Any]) -> str:
    sev = _SEVERITY_LETTER.get(str(obj.get("severity", "")).lower(), "I")
    source, code = obj.get("source"), obj.get("code")
    pair: dict[str, Any] = {}
    if source:
        pair = {"source": str(source), "code": str(code) if code else "-"}
    return svc.lines.diag_line(  # type: ignore[no-any-return,attr-defined]
        sev,
        str(obj["path"]),
        int(obj["start_line"]),
        int(obj["start_col"]),
        message=str(obj["message"]),
        **pair,
    )


def _diff_record(svc: ToolServices, diff: str, confidence: Confidence) -> Record:
    rows = diff.rstrip("\n").split("\n")
    hunk_starts = [i for i, row in enumerate(rows) if row.startswith("@@")]

    def make(count: int | None) -> Record:
        shown = rows if count is None or count >= len(hunk_starts) else rows[: hunk_starts[count]]
        lines = svc.lines.code_block(shown, lang="diff", with_line_numbers=False)  # type: ignore[attr-defined]
        omitted = 0 if count is None else max(0, len(hunk_starts) - count)
        if omitted:
            lines.append(svc.lines.note(f"diff truncated: {omitted} of {len(hunk_starts)} hunks omitted"))  # type: ignore[attr-defined]
        data: dict[str, Any] = {"section": "diff", "hunks": len(hunk_starts), "omitted_hunks": omitted}
        return Record(
            kind="code", key="diff", priority=0, lines=tuple(lines), data=data, confidence=confidence, group="diff",
            shrink=shrink if count is None and len(hunk_starts) > 1 else None,
        )  # fmt: skip

    def shrink(budget: int) -> Record | None:
        for count in range(len(hunk_starts) - 1, 0, -1):
            rec = make(count)
            if estimate_tokens("\n".join(rec.lines)) <= budget:
                return rec
        return None

    return make(None)


def _diag_records(
    svc: ToolServices, name: str, items: Sequence[Mapping[str, Any]], priority: int, confidence: Confidence
) -> list[Record]:
    if not items:
        return []
    objs = [dict(o) for o in items]
    lines = (
        f"{name}:",
        *(_diag_line(svc, o) for o in objs),
    )  # 値のない kv 行（"resolved:"）の後ろに診断行が続く
    return [
        Record(
            kind="diag",
            key=name,
            priority=priority,
            lines=lines,
            data={"section": name, "count": len(objs)},
            confidence=confidence,
            group=name,
        )  # fmt: skip
    ]


def _short(sha: str) -> str:
    return sha[:_SHA_SHOWN] + "…"


@dataclass(slots=True)
class _Seen:
    language_id: str = "n/a"
    tier: Tier = Tier.T1
    extra: dict[str, Any] = field(default_factory=dict)


def _build_output(
    svc: ToolServices, chosen: _Chosen, result: EditResult, seen: _Seen, target_resolved: bool | None
) -> ToolOutput:
    conf = result.confidence
    delta = result.new_diagnostics
    counts = _diff_counts_by_path(result.diff)
    action_id = chosen.action_id
    title = chosen.title
    action_json = {"id": action_id, "title": title, "kind": chosen.kind, "source": chosen.server}

    kv: list[tuple[str, str]] = [
        (
            "applied",
            f"{str(result.applied).lower()}  edit_id={result.edit_id or '-'}  action={action_id or '-'} "
            f'kind={chosen.kind} "{title}"',
        )
    ]
    for c in result.files_changed:
        plus, minus = counts.get(str(c.path), (0, 0))
        kv.append(
            (
                "files_changed",
                f"{c.path} (+{plus} -{minus})  sha256 {_short(c.sha256_before)}→{_short(c.sha256_after)}",
            )
        )
    if not result.files_changed:
        kv.append(("files_changed", "none"))
    kv.append(("syntax_check", result.syntax_check))
    if delta.status == "not_computed":
        kv.append(("new_diagnostics", "not_computed"))
    else:
        tail = "" if target_resolved is None else f"  target_resolved={str(target_resolved).lower()}"
        kv.append(
            ("new_diagnostics",
             f"{delta.status} (+{len(delta.added)} -{len(delta.resolved)}) checked={len(delta.checked_files)} files{tail}")
        )  # fmt: skip

    records: list[Record] = []
    if result.diff:
        records.append(_diff_record(svc, result.diff, conf))
    records += _diag_records(svc, "added", delta.added, 1, conf)
    records += _diag_records(svc, "resolved", delta.resolved, 2, conf)

    return ToolOutput(
        records=records,
        head_kv=kv,
        data_static={
            "applied": result.applied,
            "edit_id": result.edit_id,
            "action": action_json,
            "target_diagnostic_resolved": target_resolved,
            "files_changed": [
                {
                    "path": str(c.path),
                    "sha256_before": c.sha256_before,
                    "sha256_after": c.sha256_after,
                    "hunks": c.hunks,
                }
                for c in result.files_changed
            ],
            "diff": result.diff,
            "syntax_check": result.syntax_check,
            "new_diagnostics": {
                "status": delta.status,
                "added": [dict(o) for o in delta.added],
                "resolved": [dict(o) for o in delta.resolved],
                "checked_files": [dict(o) for o in delta.checked_files],
            },
        },
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine=chosen.server, confidence=conf, lsp_status=LspStatus.READY.value),
        page_unit="section",
        tier=seen.tier,
        detected_language=seen.language_id,
        warnings=(*result.warnings, *delta.warnings),
    )


# --- ハンドラ -------------------------------------------------------------------------------


async def handler(ctx: ToolContext, args: ApplyCodeActionArgs) -> ToolOutput:
    svc = services_of(ctx.services)
    ws = svc.workspace(args.workspace_id)
    rel = confine(ws, args.path, for_write=True, param="path")
    data, language_id, tier = await asyncio.to_thread(_read_file, ws, rel)
    sha = _sha256(data)
    index = LineIndex(data)
    rng = _internal_range(index, args)
    pool = getattr(svc, "lsp_pool", None)
    store = getattr(svc, "code_action_store", None)

    if args.action_kind_or_id.startswith(ID_PREFIX):
        chosen = await _choose_by_id(store, args, rel, sha, index, pool, ws, language_id)
    else:
        chosen = await _choose_by_kind(store, args, rel, data, sha, index, rng, pool, ws, language_id)

    seen = _Seen(language_id=language_id, tier=tier)
    tx = _transaction(svc, ws, chosen.handle)
    result: EditResult = await tx.run(
        _plan_builder(ws, chosen, rel, sha),
        target=TargetRef(path=str(rel)),
        dry_run=args.dry_run,
        allow_heuristic_mutation=args.allow_heuristic_mutation,
        expected_shas={str(rel): args.expected_file_sha256} if args.expected_file_sha256 else None,
        diagnostics_wait_ms=args.diagnostics_wait_ms,
        revision=args.revision,
    )
    return _build_output(svc, chosen, result, seen, _target_resolved(chosen, result))


SPEC = ToolSpec(
    name="apply_code_action",
    tid="T-13",
    args_model=ApplyCodeActionArgs,
    handler=handler,
    default_max_tokens=1500,
    description=(
        "Apply ONE language-server code action (QuickFix, organize imports, fix all, refactor.extract ...) and get the diff, "
        "the syntax check and the diagnostics that went away in the same response. action_kind_or_id is either a "
        "code_action_id from get_diagnostics (fix=qf_...; pass the line of that diagnostic) or an LSP CodeActionKind "
        "(quickfix, source.organizeImports, source.fixAll, refactor.extract, ...). A kind that matches several actions "
        "returns E_TARGET_AMBIGUOUS with candidate ids; call again with one id. Needs a ready language server "
        "(E_LSP_UNAVAILABLE otherwise). Actions that only run a server command are refused (E_UNSUPPORTED). "
        "Pass expected_file_sha256 to refuse a stale write; use dry_run to preview."
    ),
    writes=True,
    max_confidence=Confidence.EXACT,
    milestone="M1",
    page_unit="section",
    records_key="records",
)
