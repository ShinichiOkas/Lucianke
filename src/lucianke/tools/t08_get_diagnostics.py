"""T-08 ``get_diagnostics``（仕様書 §11.4）。LSP と構文木の最新の診断を、ビルドもリンターも走らせずに返す。

経路（ファイルごとに ``engine.selector`` の判定表で決める。``Capability.DIAGNOSTICS``）:

* **exact**: 言語の LSP（``types`` 役）が ``ready``。LSP が ``publishDiagnostics`` で通知した診断（``types`` と ``lint`` の
  両役のハブ）を、``DocumentSync.sync`` で最新の内容を見せたうえで ``DiagnosticsHub.wait_for`` の版待ちで取る
  （固定の ``sleep`` はしない）。索引の構文エラー（``source = tree-sitter``）も足し、同一位置・同一メッセージは LSP を採る。
* **structural**: LSP が ``indexing`` / ``standby`` / ``unavailable``、または LSP の定義が無い言語・Tier 3。起動は待たず
  （``ensure_started`` で遅延起動の契機にはする）、索引の構文エラーだけで応える。``accuracy_warning`` に
  「型・未定義参照・Lint の診断は含まれていない」と書く。
* **lexical_fallback**: 文法の無い Tier 4。診断なし（0 件）。

``ctx.services`` に求めるもの（duck typing。``tools`` は ``mcp`` を import できない）:

* ``db_path``（``Path``）・``workspace(workspace_id)``・``lines``（compact の行の組み立て関数のモジュール）: 必須。
  T-06 / T-07 / T-19 と同じ約束。
* ``lsp_pool``（``LspPool``）: 任意。無ければ全言語を ``unavailable`` として構文エラーだけで答える。
* ``code_action_store``（``lsp.action_store.CodeActionStore``）: 任意。``include_code_actions`` の ID 発行先。無ければ
  QuickFix は付けず ``warnings`` に書く（ID を引けない ID は発行しない）。T-13 は同じ台帳から引く。
* ``trees``（``TreeCache``）: 任意。索引が古いファイル（ディスクの ``sha256`` が索引と違う）の構文エラーを構文木から取り直す。
  無ければ古いファイルの構文エラーは返さず ``warnings`` に書く。
* ``diagnostics_wait_ms``（``int``）: 任意。LSP の診断待ちの上限（既定 ``DIAGNOSTICS_WAIT_DEFAULT_MS``）。

実装で決めたこと（仕様書との差は報告に書いた）:

* ``revision`` が ``WORKING_TREE`` 以外は ``E_UNSUPPORTED``（T-02・T-06・T-07 と同じ。過去断面の構文木診断は M1 の範囲外）。
* ``Record.confidence``（``_resolution_meta`` の集計の元）は **ファイルの経路の水準**（LSP が答えたファイルの構文エラーも
  ``exact``）。診断ごとの ``confidence_level``（json）は仕様どおり、LSP 由来 ``exact`` / 構文木由来 ``structural``。
* 索引の ``diagnostics`` 表のうち使うのは ``tree-sitter`` と ``lucianke-*``（アダプタ）だけ。LSP の名前の行は、
  ハブの生のキャッシュとの二重計上と古い行を避けるため読まない。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import DIAGNOSTICS_WAIT_DEFAULT_MS, LSP_OPEN_DOCS_MAX, SNIPPET_MAX_CHARS
from lucianke.core.model import (
    Capability,
    Confidence,
    EngineChoice,
    FileInfo,
    LspStatus,
    Position,
    Range,
    RelPath,
    Tier,
)
from lucianke.core.records import Record, ToolOutput
from lucianke.core.textpos import LineIndex, Unit
from lucianke.engine.selector import select
from lucianke.index import queries
from lucianke.lsp.pool import LspHandle, LspState, PoolStatus
from lucianke.lsp.servers import servers_for
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._nav import db_path_of, reader, require_working_tree, workspace_of
from lucianke.workspace.paths import confine, to_abs
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.tools.t08")

Severity = Literal["error", "warning", "information", "hint"]
SEVERITIES: tuple[str, ...] = ("error", "warning", "information", "hint")
_LETTER = {"error": "E", "warning": "W", "information": "I", "hint": "H"}
_LSP_SEVERITY = {1: "error", 2: "warning", 3: "information", 4: "hint"}

TREE_SITTER = "tree-sitter"
SYNTAX_CODE = "syntax"
PATHS_MAX = 50
STALE_CHECK_MAX_FILES = 200
CODE_ACTION_BUDGET_MS = 3000  # 仕様書 T-08 の E_TIMEOUT: include_code_actions の問い合わせの合計
CODE_ACTION_CONCURRENCY = 8
CODE_ACTION_MAX_DIAGS = 100  # 1 回の呼び出しで QuickFix を問い合わせる診断の数（並びの先頭から）
MAX_LSP_FILES = (
    LSP_OPEN_DOCS_MAX // 2
)  # 1 回の呼び出しで LSP に開かせるファイルの数（ワークスペース全体のとき）
_COMPACT_MESSAGE_MAX = 160
_SOURCE_ALIASES = {"pyright": ("pyright", "basedpyright"), "basedpyright": ("basedpyright", "pyright")}

_NOT_COVERED = "型・未定義参照・Lint の診断は含まれていません。"


class GetDiagnosticsArgs(CommonArgs):
    paths: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=PATHS_MAX,
        description=(
            "Files or directories (workspace-relative with '/', or absolute inside the workspace), up to 50. "
            "Default: every indexed file of the workspace."
        ),
    )
    min_severity: Severity = Field(
        default="warning",
        description='"error" | "warning" | "information" | "hint": this severity and above.',
    )
    include_code_actions: bool = Field(
        default=True,
        description="Ask the LSP for QuickFix candidates of errors/warnings and add their code_action_id.",
    )
    sources: list[str] | None = Field(
        default=None,
        description='Only these diagnostic sources (e.g. ["pyright"], ["tree-sitter"]). Default: all.',
    )


# --- 診断 1 件 ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _Diag:
    path: str
    line: int  # 1 始まり
    character: int  # 1 始まり（コードポイント）
    end_line: int
    end_character: int
    severity: str
    source: str
    code: str | None
    message: str
    snippet: str
    confidence: Confidence  # この診断の由来の水準（LSP = exact / 構文木 = structural）
    route: Confidence  # ファイルの経路の水準（Record.confidence になる）
    enclosing_urn: str | None = None
    from_lsp: bool = False
    raw: Mapping[str, Any] | None = None
    handle: LspHandle | None = None
    sha256: str = ""
    internal_range: Range | None = None
    code_actions: list[dict[str, str]] = field(default_factory=list)

    @property
    def dedupe_key(self) -> tuple[str, int, int, str]:
        return (self.path, self.line, self.character, " ".join(self.message.split()))


@dataclass(slots=True)
class _FileRoute:
    file: FileInfo
    choice: EngineChoice
    lsp_handles: list[LspHandle] = field(default_factory=list)


@dataclass(slots=True)
class _Scope:
    files: list[FileInfo]
    rows: dict[str, list[queries.DiagnosticRow]]
    warnings: list[str]


# --- 範囲の解決と索引の読み取り -----------------------------------------------------------------


def _norm(text: str) -> str:
    return text.lower()


def _source_wanted(source: str, wanted: Sequence[str] | None) -> bool:
    if not wanted:
        return True
    s = _norm(source)
    for w in wanted:
        names = _SOURCE_ALIASES.get(_norm(w), (_norm(w),))
        if s in names:
            return True
    return False


def _index_source_ok(source: str) -> bool:
    """索引の ``diagnostics`` 表から読む ``source``: 構文木とアダプタ（``lucianke-*``）だけ。LSP の名前の行は読まない。"""
    return source == TREE_SITTER or source.startswith("lucianke-")


def _load_scope(db_path: Path, ws: Workspace, rels: list[RelPath] | None) -> _Scope:
    """対象ファイル（索引済み）と、その索引の構文診断。``rels`` が ``None`` ならワークスペース全体。"""
    warnings: list[str] = []
    with reader(db_path) as conn:
        everything = queries.files_in_scope(conn, ws.workspace_id)
        if rels is None:
            files = everything
        else:
            by_path = {f.path: f for f in everything}
            chosen: dict[str, FileInfo] = {}
            for rel in rels:
                text = str(rel)
                if text == ".":
                    chosen.update({f.path: f for f in everything})
                    continue
                found = [f for f in everything if f.path == text or f.path.startswith(text + "/")]
                for f in found:
                    chosen[f.path] = f
                if not found:
                    abs_path = to_abs(ws, rel)
                    if not abs_path.exists():
                        raise LuciankeError(
                            ErrorCode.E_TARGET_NOT_FOUND,
                            f"path not found: {text}",
                            details={"path": text, "param": "paths"},
                        )
                    warnings.append(
                        f"{text}: not in the index (ignored, excluded or not yet indexed); skipped"
                    )
            del by_path
            files = sorted(chosen.values(), key=lambda f: f.path)
        rows: dict[str, list[queries.DiagnosticRow]] = {}
        if files:
            explicit = None if rels is None else [f.path for f in files]
            for row in queries.diagnostics_for(conn, ws.workspace_id, explicit, "hint"):
                if _index_source_ok(row.source):
                    rows.setdefault(str(row.path), []).append(row)
    return _Scope(files=files, rows=rows, warnings=warnings)


def _enclosing(db_path: Path, ws_id: str, items: list[tuple[str, int, int]]) -> list[str | None]:
    """``(path, line0, col0)`` ごとの最内シンボルの URN（シンボル外・索引に無ければ ``None``）。"""
    out: list[str | None] = []
    with reader(db_path) as conn:
        for path, line0, col0 in items:
            sym = queries.symbol_at(conn, ws_id, path, line0, col0)
            out.append(None if sym is None else str(sym.urn))
    return out


# --- ファイルの内容と行 --------------------------------------------------------------------------


def _read(ws: Workspace, rel: str) -> bytes | None:
    try:
        return to_abs(ws, RelPath(rel)).read_bytes()
    except OSError:
        return None


def _text_of(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    return text[1:] if text.startswith("﻿") else text


def _snippet(index: LineIndex, line0: int) -> str:
    if not 0 <= line0 < index.line_count:
        return ""
    text = index.line_text(line0)
    return text if len(text) <= SNIPPET_MAX_CHARS else text[: SNIPPET_MAX_CHARS - 1] + "…"


def _clamp_line(index: LineIndex, pos: Position) -> Position:
    """末尾の区切りの後ろの空行（EOF）を指す位置を、最後の内容の行の行末へ寄せる（行数の上限を守る）。"""
    last = index.content_line_count - 1
    if last >= 0 and pos.line > last:
        return Position(last, index.line_length(last, Unit.CODEPOINT))
    return pos


# --- LSP からの収集 ------------------------------------------------------------------------------


def _lsp_severity(raw: Mapping[str, Any]) -> str:
    value = raw.get("severity")
    return (
        _LSP_SEVERITY.get(value, "error")
        if isinstance(value, int) and not isinstance(value, bool)
        else "error"
    )


def _code_of(raw: Mapping[str, Any]) -> str | None:
    code = raw.get("code")
    if isinstance(code, Mapping):  # {"value": ..., "target": ...}（一部のサーバー）
        code = code.get("value")
    if code is None or isinstance(code, bool):
        return None
    text = str(code).strip()
    return text or None


@dataclass(slots=True)
class _LspResult:
    diags: list[_Diag] = field(default_factory=list)
    pending: bool = False
    error: str | None = None


async def _collect_one(
    handle: LspHandle, ws: Workspace, file: FileInfo, route: Confidence, wait_ms: float
) -> _LspResult:
    """1 ファイル × 1 ハンドル: 最新の内容を LSP に見せ、版待ちをして、ハブの診断を ``_Diag`` にする。"""
    rel = RelPath(file.path)
    data = await asyncio.to_thread(_read, ws, file.path)
    if data is None:
        return _LspResult(error=f"{file.path}: cannot read the file")
    language_id = handle.defn.document_language_id if handle.defn is not None else handle.language_id
    try:
        version = handle.documents.sync(rel, _text_of(data), language_id)
    except ConnectionError as e:
        return _LspResult(error=f"{file.path}: {handle.server_name or handle.language_id} disconnected ({e})")
    handle.touch()
    sent_at = handle.documents.sent_at_of(rel)
    hub = handle.diagnostics
    ok = await hub.wait_for(
        rel, min_version=version, sent_at=sent_at if sent_at is not None else 0.0, timeout_ms=wait_ms
    )
    entry = hub.get(rel)
    result = _LspResult(pending=not ok)
    if entry is None:
        return result
    client = handle.client
    index = LineIndex(data)
    sha = hashlib.sha256(data).hexdigest()
    server = handle.server_name or handle.language_id
    for raw in entry.diagnostics:
        rng_raw = raw.get("range")
        if not isinstance(rng_raw, Mapping):
            continue
        try:
            rng = (
                client.from_lsp_range(index, rng_raw)
                if client is not None
                else Range(
                    Position(rng_raw["start"]["line"], rng_raw["start"]["character"]),
                    Position(rng_raw["end"]["line"], rng_raw["end"]["character"]),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
        start = _clamp_line(index, rng.start)
        end = _clamp_line(index, rng.end)
        if end < start:
            end = start
        source = raw.get("source")
        result.diags.append(
            _Diag(
                path=file.path,
                line=start.line + 1,
                character=start.col + 1,
                end_line=end.line + 1,
                end_character=end.col + 1,
                severity=_lsp_severity(raw),
                source=_norm(source) if isinstance(source, str) and source else _norm(server),
                code=_code_of(raw),
                message=str(raw.get("message", "")),
                snippet=_snippet(index, start.line),
                confidence=Confidence.EXACT,
                route=route,
                from_lsp=True,
                raw=raw,
                handle=handle,
                sha256=sha,
                internal_range=Range(start, end),
            )
        )
    return result


# --- 構文木からの収集（索引の行 + 古いファイルの取り直し） --------------------------------------


async def _tree_sitter_diags(
    ctx: ToolContext, ws: Workspace, route: _FileRoute, rows: list[queries.DiagnosticRow], warnings: list[str]
) -> list[_Diag]:
    file = route.file
    data = await asyncio.to_thread(_read, ws, file.path)
    if data is None:
        return []  # 消えたファイル（索引が追いついていない）: 返さない
    sha = hashlib.sha256(data).hexdigest()
    index = LineIndex(data)
    confidence = route.choice.confidence
    facts: list[tuple[Range, str, str, str | None, str | None, Confidence]] = []
    if sha == file.sha256:
        for r in rows:
            facts.append((r.range, r.severity, r.source, r.code, r.message, r.confidence))  # type: ignore[arg-type]
    else:
        trees = getattr(ctx.services, "trees", None)
        fresh: list[tuple[Range, str, str, str | None, str | None, Confidence]] | None = None
        if trees is not None:
            try:
                parsed = await trees.aget_or_parse(
                    ws.workspace_id, RelPath(file.path), sha, file.language, lambda: data
                )
                fresh = [
                    (
                        Range(Position(e.start.line, e.start.col), Position(e.end.line, e.end.col)),
                        "error",
                        TREE_SITTER,
                        None,
                        "syntax error",
                        Confidence.STRUCTURAL,
                    )
                    for e in parsed.error_ranges
                ]
            except Exception:  # noqa: BLE001  文法の無い言語（アダプタ）・解析の失敗は「取り直せない」
                _log.debug("re-parse of %s failed", file.path, exc_info=True)
        if fresh is None:
            warnings.append(
                f"{file.path}: changed since it was indexed; its syntax diagnostics are skipped until it is re-indexed"
            )
            return []
        facts = fresh
    out: list[_Diag] = []
    for rng, severity, source, code, message, conf in facts:
        start = _clamp_line(index, rng.start)
        end = _clamp_line(index, rng.end)
        if end < start:
            end = start
        out.append(
            _Diag(
                path=file.path,
                line=start.line + 1,
                character=start.col + 1,
                end_line=end.line + 1,
                end_character=end.col + 1,
                severity=severity,
                source=source,
                code=code if code else (SYNTAX_CODE if source == TREE_SITTER else None),
                message=message or "syntax error",
                snippet=_snippet(index, start.line),
                confidence=conf,
                route=confidence,
                sha256=sha,
            )
        )
    return out


# --- 経路の決定 ----------------------------------------------------------------------------------


def _has_lsp_def(language: str) -> bool:
    return bool(servers_for(language))


def _status_of(pool: Any, language: str, ws: Workspace) -> PoolStatus:
    """``types`` 役（無ければ ``lint`` 役）の状態。プールが無い・定義が無い言語はそれぞれ ``unavailable`` / ``n/a``。"""
    if not _has_lsp_def(language):
        return PoolStatus(LspStatus.NOT_APPLICABLE, None, None, None, None)
    if pool is None:
        return PoolStatus(LspStatus.UNAVAILABLE, None, None, None, "no_pool")
    primary = "types" if servers_for(language, "types") else "lint"
    return pool.status(language, ws, primary)  # type: ignore[no-any-return]


def _ready_handles(pool: Any, language: str, ws: Workspace) -> list[LspHandle]:
    if pool is None:
        return []
    return [h for h in pool.handles(ws, language) if h.state is LspState.READY and h.client is not None]


def _degraded_warning(choice: EngineChoice) -> str | None:
    if choice.confidence is Confidence.EXACT:
        return None
    if choice.lsp_status == LspStatus.INDEXING.value:
        pct = "" if choice.lsp_progress is None else f"（{choice.lsp_progress}%）"
        return (
            f"LSP が起動・索引中{pct}のため、構文エラー（Tree-sitter）だけを返しています。"
            f"{_NOT_COVERED}完了後に呼び直すと `exact` になります。"
        )
    if choice.lsp_status in (LspStatus.UNAVAILABLE.value, LspStatus.STANDBY.value):
        return (
            "LSP が利用できないため、構文エラー（Tree-sitter）だけを返しています。"
            f"{_NOT_COVERED}`configure_workspace status` で LSP の状態を確認してください。"
        )
    if choice.confidence is Confidence.STRUCTURAL:
        return f"この言語・形式には LSP が無く、構文エラー（Tree-sitter）だけを返しています。{_NOT_COVERED}"
    return None


# --- QuickFix -----------------------------------------------------------------------------------


async def _attach_code_actions(diags: list[_Diag], store: Any, warnings: list[str]) -> tuple[bool, int]:
    """``error`` / ``warning`` の LSP 診断に QuickFix を問い合わせて ID を付ける。``(時間切れか, 失敗した件数)``。"""
    from lucianke.lsp import features  # 遅延 import（LSP 機能呼び出しは代筆中のモジュール）

    targets = [
        d for d in diags if d.from_lsp and d.severity in ("error", "warning") and d.handle is not None
    ][:CODE_ACTION_MAX_DIAGS]
    if not targets:
        return False, 0
    sem = asyncio.Semaphore(CODE_ACTION_CONCURRENCY)
    failed = 0

    async def one(d: _Diag) -> None:
        assert d.handle is not None and d.internal_range is not None and d.raw is not None
        async with sem:
            actions = await features.code_actions(
                d.handle, RelPath(d.path), d.internal_range, only=["quickfix"], diagnostics=[d.raw]
            )
        for action in actions:
            if not isinstance(action, Mapping) or not action.get("title"):
                continue
            stored = store.put(
                path=RelPath(d.path),
                sha256=d.sha256,
                language_id=d.handle.language_id,
                server=d.handle.server_name or d.handle.language_id,
                action=action,
                range=d.raw.get("range", {}),
                diagnostic=d.raw,
            )
            d.code_actions.append({"id": stored.id, "title": stored.title, "kind": stored.kind})

    tasks = [asyncio.ensure_future(one(d)) for d in targets]
    done, pending = await asyncio.wait(tasks, timeout=CODE_ACTION_BUDGET_MS / 1000)
    for p in pending:
        p.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for t in done:
        exc = t.exception()
        if exc is not None:
            failed += 1
            if not isinstance(exc, (LuciankeError, TimeoutError, ConnectionError)):
                _log.warning("code action request failed: %r", exc)
    if failed:
        warnings.append(
            f"QuickFix lookup failed for {failed} diagnostic(s) (the LSP did not answer); they have no fix="
        )
    return bool(pending), failed


# --- 出力 ----------------------------------------------------------------------------------------


def _counts(diags: Sequence[_Diag]) -> tuple[dict[str, int], dict[str, int], int]:
    by_sev = dict.fromkeys(SEVERITIES, 0)
    by_source: dict[str, int] = {}
    files: set[str] = set()
    for d in diags:
        by_sev[d.severity] += 1
        by_source[d.source] = by_source.get(d.source, 0) + 1
        files.add(d.path)
    return by_sev, by_source, len(files)


def _head_line(by_sev: Mapping[str, int], files: int, min_severity: str) -> str:
    total = sum(by_sev.values())
    parts = [f"{by_sev[s]} {_LETTER[s]}" for s in SEVERITIES if by_sev[s]]
    counts = ", ".join(parts)
    inner = f"{counts}; min_severity={min_severity}" if parts else f"min_severity={min_severity}"
    file_word = "file" if files == 1 else "files"
    return f"{total} in {files} {file_word} ({inner})"


def _summarize(rest: Sequence[Record]) -> str:
    by_file: dict[str, int] = {}
    by_sev = dict.fromkeys(SEVERITIES, 0)
    for r in rest:
        by_file[str(r.data["path"])] = by_file.get(str(r.data["path"]), 0) + 1
        by_sev[str(r.data["severity"])] += 1
    sev = ", ".join(f"{n} {_LETTER[s]}" for s, n in by_sev.items() if n)
    shown = list(by_file.items())[:5]
    files = ", ".join(f"{p}: {n}" for p, n in shown)
    more = len(by_file) - len(shown)
    if more > 0:
        files += f", +{more} files"
    return f"{len(rest)} more diagnostics ({sev}) in {len(by_file)} files [{files}]"


def _record(L: Any, d: _Diag, priority: int) -> Record:
    message = (
        d.message
        if len(" ".join(d.message.split())) <= _COMPACT_MESSAGE_MAX
        else (" ".join(d.message.split())[: _COMPACT_MESSAGE_MAX - 1].rstrip() + "…")
    )
    fix = d.code_actions[0]["id"] if d.code_actions else None
    line = L.diag_line(
        _LETTER[d.severity],
        d.path,
        d.line,
        d.character,
        source=d.source,
        code=d.code if d.code else "-",
        message=message or "(no message)",
        fix=fix,
    )
    data = {
        "path": d.path,
        "line": d.line,
        "character": d.character,
        "end_line": d.end_line,
        "end_character": d.end_character,
        "severity": d.severity,
        "source": d.source,
        "code": d.code,
        "message": d.message,
        "enclosing_urn": d.enclosing_urn,
        "snippet": d.snippet,
        "code_actions": list(d.code_actions),
        "confidence_level": d.confidence.value,
    }
    return Record(
        kind="diag",
        key=f"{d.path}:{d.line}:{d.character}:{d.source}:{d.code}:{hashlib.sha1(d.message.encode()).hexdigest()[:6]}",
        priority=priority,
        lines=(line,),
        data=data,
        confidence=d.route,
        group=d.path,
    )


def _overall_engine(routes: list[_FileRoute]) -> EngineChoice:
    """応答全体の ``EngineChoice``（診断が 0 件のときの水準と ``lsp_status`` の元）。

    LSP の定義がある言語のファイルがあれば、その中の最も低い水準を採る（定義の無い JSON などが、LSP が ready の
    ワークスペース全体を降格に見せないため）。無ければ全ファイルの最も低い水準。
    """
    if not routes:
        return EngineChoice(
            engine=TREE_SITTER, confidence=Confidence.EXACT, lsp_status=LspStatus.NOT_APPLICABLE.value
        )
    capable = [r for r in routes if _has_lsp_def(r.file.language) and r.file.tier in (Tier.T1, Tier.T2)]
    pool_of = capable or routes
    worst = min(pool_of, key=lambda r: (r.choice.confidence, r.choice.lsp_status == LspStatus.READY.value))
    choice = worst.choice
    statuses = {r.choice.lsp_status for r in capable}
    if capable:
        for s in (
            LspStatus.UNAVAILABLE.value,
            LspStatus.INDEXING.value,
            LspStatus.STANDBY.value,
            LspStatus.READY.value,
        ):
            if s in statuses:
                status = s
                break
        else:
            status = choice.lsp_status
    else:
        status = LspStatus.NOT_APPLICABLE.value
    progress = choice.lsp_progress if status == LspStatus.INDEXING.value else None
    return EngineChoice(
        engine=choice.engine,
        confidence=choice.confidence,
        lsp_status=status,
        lsp_progress=progress,
        warning=_degraded_warning(choice) if choice.confidence is not Confidence.EXACT else None,
    )


def _lsp_sources(pool: Any, ws: Workspace, languages: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for lang in languages:
        for role in ("types", "lint"):
            if not servers_for(lang, role):  # type: ignore[arg-type]
                continue
            if pool is None:
                st = PoolStatus(LspStatus.UNAVAILABLE, None, servers_for(lang, role)[0].name, None, "no_pool")  # type: ignore[arg-type]
            else:
                st = pool.status(lang, ws, role)
            out.append(
                {
                    "language": lang,
                    "role": role,
                    "server": st.server_name,
                    "status": st.status.value,
                    "progress": st.progress if st.status is LspStatus.INDEXING else None,
                }
            )
    return out


# --- ハンドラ ------------------------------------------------------------------------------------


async def handler(ctx: ToolContext, args: GetDiagnosticsArgs) -> ToolOutput:
    require_working_tree(args.revision)
    db_path = db_path_of(ctx)
    ws = workspace_of(ctx, args.workspace_id)
    L = ctx.services.lines  # type: ignore[attr-defined]
    pool = getattr(ctx.services, "lsp_pool", None)
    store = getattr(ctx.services, "code_action_store", None)
    wait_ms = getattr(ctx.services, "diagnostics_wait_ms", None) or DIAGNOSTICS_WAIT_DEFAULT_MS
    warnings: list[str] = []

    rels = None if args.paths is None else [confine(ws, p, param="paths") for p in args.paths]
    scope = await asyncio.to_thread(_load_scope, db_path, ws, rels)
    warnings.extend(scope.warnings)

    # 1. ファイルごとの経路（selector の判定表）と、LSP の遅延起動
    languages = sorted(
        {f.language for f in scope.files if f.tier in (Tier.T1, Tier.T2) and _has_lsp_def(f.language)}
    )
    if pool is not None:
        for lang in languages:
            await pool.ensure_started(lang, ws)  # 待たない（standby なら起動を投げる）
    statuses = {lang: _status_of(pool, lang, ws) for lang in languages}
    routes: list[_FileRoute] = []
    for f in scope.files:
        st = statuses.get(f.language) or PoolStatus(LspStatus.NOT_APPLICABLE, None, None, None, None)
        choice = select(
            f,
            Capability.DIAGNOSTICS,
            lsp_status=st.status,
            lsp_progress=st.progress,
            has_grammar=f.tier is not Tier.T4,
            has_ctags=False,
            lsp_name=st.server_name,
        )
        handles = _ready_handles(pool, f.language, ws) if choice.confidence is Confidence.EXACT else []
        if choice.confidence is Confidence.EXACT and not handles:  # ready の合図の直後に落ちた
            choice = select(
                f, Capability.DIAGNOSTICS, lsp_status=LspStatus.UNAVAILABLE, lsp_progress=None,
                has_grammar=f.tier is not Tier.T4, has_ctags=False,
            )  # fmt: skip
        routes.append(_FileRoute(f, choice, handles))

    # 2. LSP の診断（exact のファイルだけ。並行して版待ち）
    lsp_routes = [r for r in routes if r.lsp_handles]
    if len(lsp_routes) > MAX_LSP_FILES and args.paths is None:
        warnings.append(
            f"LSP diagnostics were collected for the first {MAX_LSP_FILES} of {len(lsp_routes)} files; "
            "pass paths to narrow the scope"
        )
        for r in lsp_routes[MAX_LSP_FILES:]:
            r.choice = select(
                r.file, Capability.DIAGNOSTICS, lsp_status=LspStatus.UNAVAILABLE, lsp_progress=None,
                has_grammar=True, has_ctags=False,
            )  # fmt: skip
            r.lsp_handles = []
        lsp_routes = lsp_routes[:MAX_LSP_FILES]
    jobs = [(r, h) for r in lsp_routes for h in r.lsp_handles]
    results = await asyncio.gather(
        *(_collect_one(h, ws, r.file, r.choice.confidence, wait_ms) for r, h in jobs), return_exceptions=True
    )
    all_diags: list[_Diag] = []
    pending_files: set[str] = set()
    failed_lsp: dict[str, str] = {}
    for (r, _h), res in zip(jobs, results, strict=True):
        if isinstance(res, BaseException):
            if isinstance(res, asyncio.CancelledError):
                raise res
            failed_lsp[r.file.path] = f"{r.file.path}: LSP request failed ({type(res).__name__})"
            continue
        if res.error:
            failed_lsp[r.file.path] = res.error
            continue
        if res.pending:
            pending_files.add(r.file.path)
        all_diags.extend(res.diags)
    warnings.extend(failed_lsp.values())
    if pending_files:
        shown = ", ".join(sorted(pending_files)[:3]) + (" ..." if len(pending_files) > 3 else "")
        warnings.append(
            f"No fresh diagnostics arrived within {wait_ms} ms for {len(pending_files)} file(s) ({shown}); "
            "the LSP's last known diagnostics are shown (call again to refresh)"
        )

    # 3. 構文木の診断（索引の行。ディスクが索引より新しいファイルは取り直す）。範囲を絞った呼び出しでは、行が無いファイルも
    #    ディスクの sha256 を見て古さを確かめる（広い範囲では索引を信じる。監視が索引を新しく保つ）
    check_stale = args.paths is not None and len(scope.files) <= STALE_CHECK_MAX_FILES
    for r in routes:
        if r.choice.confidence is Confidence.LEXICAL_FALLBACK:
            continue  # 診断源が無い（Tier 4）
        rows = scope.rows.get(r.file.path, [])
        if rows or check_stale:
            all_diags.extend(await _tree_sitter_diags(ctx, ws, r, rows, warnings))

    # 4. 絞り込み・重複排除（LSP を採る）・並べ替え
    allowed = SEVERITIES[: SEVERITIES.index(args.min_severity) + 1]
    keep = [d for d in all_diags if d.severity in allowed and _source_wanted(d.source, args.sources)]
    seen: dict[tuple[str, int, int, str], _Diag] = {}
    for d in sorted(keep, key=lambda x: (not x.from_lsp,)):  # LSP が先
        seen.setdefault(d.dedupe_key, d)
    diags = sorted(
        seen.values(),
        key=lambda d: (SEVERITIES.index(d.severity), d.path, d.line, d.character, d.source, d.message),
    )
    if args.sources:
        known = {TREE_SITTER} | {d.source for d in all_diags}
        for lang in languages:
            known |= {_norm(defn.name) for defn in servers_for(lang)}
        unknown = [s for s in args.sources if not any(_source_wanted(k, [s]) for k in known)]
        if unknown:
            warnings.append(f"unknown source(s) ignored: {', '.join(unknown)}")

    # 5. enclosing_urn（LSP 由来は索引の範囲包含で引く。構文木由来は索引の行の値）
    need = [d for d in diags if d.enclosing_urn is None]
    if need:
        urns = await asyncio.to_thread(
            _enclosing, db_path, ws.workspace_id, [(d.path, d.line - 1, d.character - 1) for d in need]
        )
        for d, urn in zip(need, urns, strict=True):
            d.enclosing_urn = urn

    # 6. QuickFix
    timed_out = False
    window = diags[ctx.cursor_offset :]
    if args.include_code_actions and any(d.from_lsp for d in window):
        if store is None:
            warnings.append(
                "include_code_actions: no code action store is configured; no code_action_id was issued"
            )
        else:
            timed_out, _ = await _attach_code_actions(window, store, warnings)

    # 7. 出力
    by_sev, by_source, n_files = _counts(diags)
    no_source_files = [r for r in routes if r.choice.confidence is Confidence.LEXICAL_FALLBACK]
    if no_source_files:
        by_source["n/a"] = 0
        warnings.append(
            f"{len(no_source_files)} file(s) have no diagnostic source (no grammar, Tier 4); reported as 0 diagnostics"
        )
    records = [_record(L, d, i) for i, d in enumerate(diags)]
    engine = _overall_engine(routes)
    level = min((r.confidence for r in records), default=engine.confidence)
    notes = [
        w
        for r in routes
        if r.choice.confidence is not Confidence.LEXICAL_FALLBACK and (w := _degraded_warning(r.choice))
    ]
    if level < Confidence.EXACT:  # accuracy_warning（engine.warning）に出る分は重ねない
        notes = [w for w in notes if w != engine.warning]
    warnings.extend(dict.fromkeys(notes))
    if len({r.choice.confidence for r in routes}) > 1:
        warnings.append(
            "Files are at mixed accuracy levels; see lsp_sources and each diagnostic's confidence_level"
        )
    languages_in_scope = sorted({f.language for f in scope.files if _has_lsp_def(f.language)})
    detected = ",".join(sorted({f.language for f in scope.files if f.tier in (Tier.T1, Tier.T2)})) or "n/a"
    tier = max((f.tier for f in scope.files), default=Tier.T1)
    out = ToolOutput(
        records=records,
        head_kv=[("diagnostics", _head_line(by_sev, n_files, args.min_severity))],
        data_static={
            "summary": {"files": n_files, "by_severity": by_sev, "by_source": by_source},
            "lsp_sources": _lsp_sources(pool, ws, languages_in_scope),
        },
        max_confidence=Confidence.EXACT,
        engine=engine,
        page_unit="diagnostic",
        summarize=_summarize,
        tier=tier,
        detected_language=detected,
        total_estimate=len(records),
        warnings=tuple(dict.fromkeys(warnings)),
    )
    if timed_out:
        raise LuciankeError(
            ErrorCode.E_TIMEOUT,
            f"QuickFix lookup exceeded {CODE_ACTION_BUDGET_MS} ms; diagnostics are returned without the missing fixes",
            details={"partial": True, "timeout_ms": CODE_ACTION_BUDGET_MS},
            partial=out,
        )
    return out


SPEC = ToolSpec(
    name="get_diagnostics",
    tid="T-08",
    args_model=GetDiagnosticsArgs,
    handler=handler,
    default_max_tokens=2000,
    description=(
        "Return the latest syntax, type and lint diagnostics (errors, warnings) of files or the whole workspace without "
        "running a build or linter. Each line is 'E|W|I|H path:line:col [source:code] message'; a diagnostic with a "
        "QuickFix carries fix=qf_... (pass it to apply_code_action). min_severity filters (default warning); sources filters "
        "by server (pyright, ruff, tree-sitter). When the language server is starting or unavailable only syntax errors "
        "are returned and the response says so."
    ),
    max_confidence=Confidence.EXACT,
    milestone="M1",
    page_unit="diagnostic",
    records_key="diagnostics",
)
TOOL_SPEC = SPEC
