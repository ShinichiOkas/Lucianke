"""LSP の機能呼び出し（設計書 §7.5.6・§7.8.3、ADR-6・ADR-15、スパイク S-04・S-05）。

``definition`` / ``references`` / ``hover`` / ``prepare_call_hierarchy`` / ``incoming_calls`` / ``outgoing_calls`` /
``rename`` / ``code_actions`` / ``execute_command`` は、いずれも ``LspHandle``（``LspPool.get`` / ``wait_ready`` が返す）と
作業ツリーの ``RelPath`` を受ける **async 関数**。やることは次の 4 つだけで、判断（降格・書き込み）は呼び手に任せる。

1. ``ready`` でなければ ``LuciankeError(E_LSP_UNAVAILABLE, details.reason=...)``（``indexing`` / ``standby`` / ``unavailable`` /
   ``disconnected``）。読み取り系（definition・references・hover・callHierarchy）は呼び手が握って ``structural`` に降格し、
   rename / codeAction は ADR-6 の表どおりそのまま上へ伝える。
2. 対象ファイルを ``DocumentSync.sync`` で開いた状態にする（未オープンなら ``didOpen``、ディスクの内容が変わっていれば
   ``didChange``）。LSP が見るのは作業ツリーだけ（``revision != WORKING_TREE`` を開かないのは呼び手の責任）。
3. 位置は **すべて** 交渉した単位（ADR-14。``client.negotiated_encoding``）で変換する。送る位置は文字境界の列だけ（丸めが要る
   位置は送らずに ``E_LSP_UNAVAILABLE`` / ``position_clamped``。S-11: ruff が文字の途中の列でパニックする）。応答の位置を
   内部の ``Position`` に直すには、その位置を含むファイルの内容が要るので、ディスクから読む（呼び出し 1 回の間だけ覚える）。
   ワークスペース外の URI は ``RelPath`` にできないので ``external_uri`` として返す（``from_file_uri`` が ``None`` を返すもの。
   ドライブ文字が小文字・``:`` が ``%3A`` の URI も ``from_file_uri`` が正規化する）。
4. 要求は ``LSP_TIMEOUT_*_MS`` で打ち切る。超過は ``LspRequestTimeout``（``TimeoutError`` の派生。どの要求が何 ms の期限で切れた
   かを持つ。降格の警告に使う: BUG-0091）で上げる（``$/cancelRequest`` は client が送る。
   ``LspServerDef.no_cancel_methods`` の method には送らない。BUG-0090）。
   連続タイムアウトの計数（3 回で ``unavailable``）はここで ``LspHandle.record_timeout`` / ``record_ok`` に伝える。
   サーバーのエラー応答は ``lsp_error``。そのうち ``LspServerDef.restart_on_errors`` の印を含むもの（プロセスがその method に以後
   答えられない）は ``LspHandle.record_broken`` で起動し直させて ``restarting``。降格（``engine.selector.on_lsp_failure``）は呼び手。

WorkspaceEdit（rename・codeAction）は ``edit`` を import できないので **dict のまま** 返す。``Plan`` への変換は edit 層
（``edit.plan.from_workspace_edit``）の仕事。サーバーが ``workspace/applyEdit`` を **要求として** 送ってくる経路
（tsls の ``_typescript.organizeImports``。S-5）は ``execute_command`` が受けて捕まえる（``applied: true`` と答えるが、
実際の書き込みは編集トランザクションの責任）。

PaaD への書き戻しは ``to_exact_relations`` が ``RelationFact``（``confidence=exact``）を作るところまで。
位置 → URN（``symbols`` の範囲包含）は index を持つ呼び手が ``locate`` として渡す。
"""

from __future__ import annotations

import asyncio
import logging
import re
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import (
    LSP_TIMEOUT_CALL_HIERARCHY_MS,
    LSP_TIMEOUT_CODE_ACTION_MS,
    LSP_TIMEOUT_DEFINITION_MS,
    LSP_TIMEOUT_HOVER_MS,
    LSP_TIMEOUT_REFERENCES_MS,
    LSP_TIMEOUT_RENAME_MS,
)
from lucianke.core.model import Confidence, Position, Range, RelPath
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import Urn
from lucianke.index.ingest import RelationFact
from lucianke.lsp.client import LspClient, LspError
from lucianke.lsp.pool import LspHandle, LspState
from lucianke.workspace.paths import from_file_uri, to_abs, to_file_uri
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.lsp.features")

__all__ = [
    "CallEdge",
    "CallItem",
    "CommandOutcome",
    "Location",
    "LspRequestTimeout",
    "RefLocation",
    "code_actions",
    "definition",
    "execute_command",
    "hover",
    "incoming_calls",
    "open_documents",
    "outgoing_calls",
    "prepare_call_hierarchy",
    "references",
    "rename",
    "sync_document",
    "to_exact_relations",
]

COMMAND_ONLY_KEY = "lucianke_command_only"
"""``code_actions`` が返す dict に付ける印: ``edit`` が無く ``command`` だけの CodeAction（実行は ``execute_command``）。"""

M_DEFINITION = "textDocument/definition"
M_REFERENCES = "textDocument/references"
M_HOVER = "textDocument/hover"
M_PREPARE_RENAME = "textDocument/prepareRename"
M_RENAME = "textDocument/rename"
M_PREPARE_CALL = "textDocument/prepareCallHierarchy"
M_INCOMING = "callHierarchy/incomingCalls"
M_OUTGOING = "callHierarchy/outgoingCalls"
M_CODE_ACTION = "textDocument/codeAction"
M_CODE_ACTION_RESOLVE = "codeAction/resolve"
M_EXECUTE_COMMAND = "workspace/executeCommand"
M_APPLY_EDIT = "workspace/applyEdit"

# --- 戻り値の型 -------------------------------------------------------------------------


class Location(NamedTuple):
    """``definition`` の 1 件。ワークスペース内なら ``rel`` が入り ``external_uri`` は ``None``、外なら逆。"""

    rel: RelPath | None
    range: Range
    external_uri: str | None


class RefLocation(NamedTuple):
    """``references`` の 1 件（ワークスペース内だけ）。"""

    rel: RelPath
    range: Range


class CallItem(NamedTuple):
    """``CallHierarchyItem`` の写し。先頭の 4 つが ``(name, rel, range, selection_range)``。

    ``raw`` は LSP の原本（``data`` を含む）で、``incoming_calls`` / ``outgoing_calls`` へそのまま返す。比較・表示には使わない。
    """

    name: str
    rel: RelPath | None
    range: Range
    selection_range: Range
    kind: int | None = None
    detail: str | None = None
    external_uri: str | None = None
    raw: Mapping[str, Any] | None = None


class CallEdge(NamedTuple):
    """``incoming_calls`` / ``outgoing_calls`` の 1 件。

    ``item`` は相手（incoming なら呼び元、outgoing なら呼び先）。``from_ranges`` は呼び出しの位置で、LSP の
    ``fromRanges`` の写し。**呼び元の側のファイル** ``from_rel`` の中にある（incoming は ``item.rel``、outgoing は
    問い合わせた項目のファイル）。ワークスペース外のときは ``from_rel`` が ``None``。
    """

    item: CallItem
    from_rel: RelPath | None
    from_ranges: tuple[Range, ...]


class CommandOutcome(NamedTuple):
    """``execute_command`` の結果: コマンドの戻り値と、実行中にサーバーが ``workspace/applyEdit`` で要求した WorkspaceEdit。"""

    result: Any
    edits: list[dict[str, Any]]


# --- 例外の作り方 -----------------------------------------------------------------------


def _server_details(handle: LspHandle) -> dict[str, Any]:
    return {"server": handle.server_name, "language": handle.language_id}


def _unavailable(handle: LspHandle, reason: str, message: str | None = None, **extra: Any) -> LuciankeError:
    details: dict[str, Any] = {"reason": reason, **_server_details(handle)}
    details.update(extra)
    return LuciankeError(
        ErrorCode.E_LSP_UNAVAILABLE,
        message or f"LSP {handle.server_name or handle.language_id} is not available ({reason})",
        details=details,
    )


def _require_ready(handle: LspHandle) -> LspClient:
    client = handle.client
    if handle.state is LspState.READY and client is not None and client.is_connected:
        _capture_for(client)  # workspace/applyEdit への既定の応答を用意しておく
        return client
    if handle.state in (LspState.STARTING, LspState.INDEXING):
        reason = "indexing"
    elif handle.state is LspState.READY:
        reason = "disconnected"
    else:
        reason = handle.state.value
    raise _unavailable(handle, reason)


# --- workspace/applyEdit の受け皿 --------------------------------------------------------


class _EditCapture:
    """``workspace/applyEdit`` の要求で来た WorkspaceEdit を書き換えずに溜める（``execute_command`` の間だけ有効）。"""

    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []
        self.active = 0
        self.lock = asyncio.Lock()

    def on_apply_edit(self, params: Any) -> dict[str, Any]:
        edit = params.get("edit") if isinstance(params, dict) else None
        if self.active > 0 and isinstance(edit, dict):
            self.edits.append(edit)
            return {"applied": True}
        # 捕まえる最中でない要求には適用しない（書き込みは編集トランザクションの責任）。
        return {"applied": False, "failureReason": "Lucianke does not apply edits requested by the server"}


_CAPTURES: weakref.WeakKeyDictionary[LspClient, _EditCapture] = weakref.WeakKeyDictionary()


def _capture_for(client: LspClient) -> _EditCapture:
    cap = _CAPTURES.get(client)
    if cap is None:
        cap = _CAPTURES[client] = _EditCapture()
        client.on_request(M_APPLY_EDIT, cap.on_apply_edit)
    return cap


# --- ファイルと URI ---------------------------------------------------------------------


def _read(ws: Workspace, rel: RelPath) -> bytes:
    try:
        return to_abs(ws, rel).read_bytes()
    except OSError as e:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND, f"cannot read {rel}: {e.strerror or e}", details={"path": str(rel)}
        ) from e


def _text_of(data: bytes) -> str:
    """LSP に見せる本文。BOM は付けない（``LineIndex`` の列も BOM の後ろから数える）。"""
    text = data.decode("utf-8", "replace")
    return text[1:] if text.startswith("\ufeff") else text


def _external_path(uri: str) -> Path | None:
    """``file:`` の URI をローカルのパスにする（``%3A`` や小文字のドライブも読む）。``file`` 以外は ``None``。"""
    try:
        parts = urlsplit(uri)
        if parts.scheme.lower() != "file":
            return None
        path = unquote(parts.path)
        if parts.netloc and parts.netloc.lower() != "localhost":
            path = f"//{parts.netloc}{path}"
        return Path(url2pathname(path))
    except (ValueError, OSError):
        return None


def _canonical_external(uri: str) -> str:
    """ワークスペース外の ``file:`` URI を、``%3A`` を戻した形（``file:///c:/...``）にそろえる。それ以外はそのまま。"""
    path = _external_path(uri)
    if path is None:
        return uri
    try:
        return path.as_uri()
    except ValueError:
        return uri


def _raw_position(lsp_pos: Mapping[str, Any]) -> Position:
    return Position(max(int(lsp_pos["line"]), 0), max(int(lsp_pos["character"]), 0))


def _raw_range(lsp_range: Mapping[str, Any]) -> Range:
    """ファイルを読めないときの代替: 数値をそのまま内部の位置とみなす（UTF-16 でも ASCII なら一致する）。"""
    start, end = _raw_position(lsp_range["start"]), _raw_position(lsp_range["end"])
    return Range(start, max(start, end))


class _Locator:
    """1 回の呼び出しの間だけ、応答の位置を内部の ``Position`` に直すためのファイル内容を覚える。"""

    def __init__(
        self, ws: Workspace, client: LspClient, known: Mapping[RelPath, LineIndex] | None = None
    ) -> None:
        self.ws = ws
        self.client = client
        self._rel_index: dict[RelPath, LineIndex | None] = dict(known or {})
        self._ext_index: dict[str, LineIndex | None] = {}

    def _index_of_rel(self, rel: RelPath) -> LineIndex | None:
        if rel not in self._rel_index:
            try:
                self._rel_index[rel] = LineIndex(to_abs(self.ws, rel).read_bytes())
            except (OSError, ValueError):
                self._rel_index[rel] = None
        return self._rel_index[rel]

    def _index_of_external(self, uri: str) -> LineIndex | None:
        if uri not in self._ext_index:
            path = _external_path(uri)
            try:
                self._ext_index[uri] = LineIndex(path.read_bytes()) if path is not None else None
            except OSError:
                self._ext_index[uri] = None
        return self._ext_index[uri]

    def index_of(self, loc: Location) -> LineIndex | None:
        """``loc`` のファイルの内容（読めなければ ``None``）。"""
        if loc.rel is not None:
            return self._index_of_rel(loc.rel)
        return self._index_of_external(loc.external_uri) if loc.external_uri else None

    def _range(self, index: LineIndex | None, lsp_range: Mapping[str, Any]) -> Range:
        if index is not None:
            try:
                return self.client.from_lsp_range(index, lsp_range)
            except (ValueError, KeyError):
                _log.debug("could not convert %s; using raw numbers", dict(lsp_range))
        return _raw_range(lsp_range)

    def locate(self, uri: str, lsp_range: Mapping[str, Any]) -> Location:
        rel = from_file_uri(self.ws, uri)
        if rel is not None:
            return Location(rel, self._range(self._index_of_rel(rel), lsp_range), None)
        canonical = _canonical_external(uri)
        return Location(None, self._range(self._index_of_external(canonical), lsp_range), canonical)

    def locations(self, result: Any) -> list[Location]:
        """``Location | Location[] | LocationLink[] | null`` → ``Location`` のリスト（重複は落とす）。"""
        if result is None:
            return []
        items = result if isinstance(result, list) else [result]
        out: list[Location] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if "targetUri" in item:  # LocationLink
                lsp_range = item.get("targetSelectionRange") or item.get("targetRange")
                uri = item["targetUri"]
            else:
                lsp_range, uri = item.get("range"), item.get("uri")
            if not isinstance(uri, str) or not isinstance(lsp_range, dict):
                continue
            loc = self.locate(uri, lsp_range)
            if loc not in out:
                out.append(loc)
        return out


# --- 共通の呼び出し ---------------------------------------------------------------------


async def _open(handle: LspHandle, rel: RelPath) -> tuple[LspClient, LineIndex, str]:
    """``ready`` を確かめ、``rel`` を LSP に見せて ``(client, 送った内容の LineIndex, 文書の URI)`` を返す。"""
    client = _require_ready(handle)
    data = _read(handle.ws, rel)
    language_id = handle.defn.document_language_id if handle.defn is not None else handle.language_id
    try:
        handle.documents.sync(rel, _text_of(data), language_id)
    except ConnectionError as e:
        raise _unavailable(handle, "disconnected") from e
    handle.touch()
    return client, LineIndex(data), to_file_uri(handle.ws, rel)


async def open_documents(handle: LspHandle, files: Iterable[tuple[RelPath, str]]) -> int:
    """``files``（``(作業ツリーの RelPath, LSP の languageId)``）を LSP に見せる（未オープンなら ``didOpen``）。新しく開いた件数を返す。

    参照検索の **探す側** を LSP に教えるためのもの。tsserver（typescript-language-server / vtsls）は、``tsconfig.json`` の
    ``include`` に入っていないファイル（``include: ["source"]`` の ``test/`` など）を、開かれるまでどのプロジェクトにも入れず、
    ``references`` / ``callHierarchy`` の結果に出さない（BUG-0047）。呼び手が参照元になりうるファイルをここで開いてから聞く。
    読めないファイルは飛ばす。``ready`` でなければ ``E_LSP_UNAVAILABLE``、切断なら ``E_LSP_UNAVAILABLE`` / ``disconnected``。
    """
    _require_ready(handle)
    opened = 0
    for rel, language_id in files:
        if handle.documents.is_open(rel):
            continue
        try:
            text = _text_of(_read(handle.ws, rel))
        except LuciankeError:
            continue
        try:
            handle.documents.did_open(rel, text, language_id)
        except ConnectionError as e:
            raise _unavailable(handle, "disconnected") from e
        opened += 1
    if opened:
        handle.touch()
    return opened


async def sync_document(handle: LspHandle, rel: RelPath) -> float | None:
    """``rel`` を LSP に見せ（未オープンなら ``didOpen``、内容が変わっていれば ``didChange``、同じなら何も送らない）、
    その文書を最後に送った時刻（``DocumentSync.sent_at_of``）を返す。診断を待つ呼び手（``DiagnosticsHub.wait_for(sent_at=...)``）の準備。

    要求は送らない。``ready`` でなければ ``E_LSP_UNAVAILABLE``、切断なら ``E_LSP_UNAVAILABLE`` / ``disconnected``。
    """
    await _open(handle, rel)
    return handle.documents.sent_at_of(rel)


def _lsp_position(handle: LspHandle, client: LspClient, index: LineIndex, pos: Position) -> dict[str, int]:
    """内部の位置 → LSP の位置。**文字境界の列だけ** を送る（S-11: ruff は文字の途中の列でパニックして終了し、
    UTF-16 の 3 サーバーは別のシンボルに当たる）。丸めが要る位置（文字の途中・行末を越える列）は送らずに
    ``E_LSP_UNAVAILABLE``（``details.reason="position_clamped"``）にする。呼び手はこれを降格として扱う。
    変換と丸めの判定は ``LspClient.to_lsp_position`` の 1 か所（ここは範囲外の行を ``E_INVALID_ARGUMENT`` にするだけ）。
    """
    try:
        return client.to_lsp_position(index, pos, details=_server_details(handle))
    except ValueError as e:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT, f"position is outside the file: {e}", details={"param": "position"}
        ) from e


def _lsp_range(handle: LspHandle, client: LspClient, index: LineIndex, rng: Range) -> dict[str, Any]:
    return {
        "start": _lsp_position(handle, client, index, rng.start),
        "end": _lsp_position(handle, client, index, rng.end),
    }


def _breaks_the_server(handle: LspHandle, message: str) -> bool:
    """エラー応答の ``message`` が、そのサーバーの「以後この method に答えられない」印（``LspServerDef.restart_on_errors``）を含む。"""
    markers = handle.defn.restart_on_errors if handle.defn is not None else ()
    return any(marker in message for marker in markers)


class LspRequestTimeout(TimeoutError):
    """要求 1 つが期限（``LSP_TIMEOUT_*_MS``）内に答えなかった。``TimeoutError`` の派生なので、握る側はそのままでよい。

    降格の警告に「どの要求が、実際に何 ms の期限で切れたか」を書くために持つ（定数ではなく、その要求に渡した期限。BUG-0091）。
    """

    def __init__(self, method: str, timeout_ms: float, server: str | None = None) -> None:
        super().__init__(f"LSP {server or '?'} did not answer {method} within {timeout_ms:.0f} ms")
        self.method = method
        self.timeout_ms = timeout_ms
        self.server = server


async def _call(
    handle: LspHandle, client: LspClient, method: str, params: Any, timeout_ms: float, *, strike: bool = True
) -> Any:
    """要求を 1 つ送る。タイムアウトは数えて ``LspRequestTimeout`` で上げ（実際に渡した期限を持つ。BUG-0091）、切断・サーバーの
    エラーは ``E_LSP_UNAVAILABLE`` にする。

    サーバーのエラーは ``details.reason="lsp_error"``（``server`` / ``method`` / ``lsp_code`` / ``lsp_message`` 付き。LSP は動いている）。
    ただしエラーが ``LspServerDef.restart_on_errors`` の印を含むとき（プロセスがその method に以後答えられない。BUG-0090）は
    ``LspHandle.record_broken`` で起動し直させ、``details.reason="restarting"`` にする（呼び手の警告と ``lsp_status`` が「再起動中」になる）。
    起動し直しが上限に達して ``unavailable``（``broken_3x``）になったときは ``details.reason="unavailable"``。
    ``strike=False`` の時間切れは連続時間切れ（3 回で ``unavailable``）に数えない: 1 問い合わせの予算の残りを期限にした要求で、
    その残りが要求 1 つの期限より短かった（予算が尽きて切れただけで、LSP の無応答ではない。BUG-0091）。
    """
    try:
        result = await client.request(method, params, timeout_ms=timeout_ms)
    except TimeoutError as e:
        if strike:
            handle.record_timeout()
        raise LspRequestTimeout(method, timeout_ms, getattr(handle, "server_name", None)) from e
    except ConnectionError as e:
        raise _unavailable(handle, "disconnected", str(e)) from e
    except LspError as e:
        reason, tail = "lsp_error", ""
        if _breaks_the_server(handle, e.message):
            state = handle.record_broken(method, e.message)
            if state is LspState.STARTING:
                reason, tail = "restarting", "; restarting the LSP"
            elif state is LspState.UNAVAILABLE:
                reason, tail = "unavailable", f"; the LSP is now unavailable ({handle.reason})"
        raise _unavailable(
            handle,
            reason,
            f"LSP {handle.server_name} failed {method}: {e.message}{tail}",
            method=method,
            lsp_code=e.code,
            lsp_message=e.message,
        ) from e
    handle.record_ok()
    return result


# --- definition / references / hover ------------------------------------------------------


_IDENT = re.compile(r"\w+")


def _identifier_at(index: LineIndex, pos: Position) -> str | None:
    """``pos`` にある（または直前で終わる）識別子。無ければ ``None``。"""
    try:
        text = index.line_text(pos.line)
    except ValueError:
        return None
    for m in _IDENT.finditer(text):
        if m.start() <= pos.col <= m.end():
            return m.group()
    return None


def _narrow_to_name(locator: _Locator, loc: Location, name: str | None) -> Location:
    """定義の範囲が識別子より広いとき（vtsls は宣言文全体を返す。S-11）、範囲の中の最初の ``name`` の位置へ絞る。

    すでに名前ちょうどの範囲、名前が範囲に見つからない（別名・再エクスポートなど）、ファイルを読めないときはそのまま。
    """
    if not name:
        return loc
    index = locator.index_of(loc)
    if index is None:
        return loc
    start, end = loc.range.start, loc.range.end
    try:
        for line in range(start.line, min(end.line, index.line_count - 1) + 1):
            text = index.line_text(line)
            lo = start.col if line == start.line else 0
            hi = end.col if line == end.line else len(text)
            if line == start.line and line == end.line and text[lo:hi] == name:
                return loc  # すでに名前ちょうど
            for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", text):
                if lo <= m.start() and m.end() <= max(hi, lo):
                    return Location(
                        loc.rel, Range(Position(line, m.start()), Position(line, m.end())), loc.external_uri
                    )
    except ValueError:
        return loc
    return loc


async def _definition_locations(
    handle: LspHandle, client: LspClient, index: LineIndex, rel: RelPath, uri: str, pos: Position
) -> list[Location]:
    params = {"textDocument": {"uri": uri}, "position": _lsp_position(handle, client, index, pos)}
    result = await _call(handle, client, M_DEFINITION, params, LSP_TIMEOUT_DEFINITION_MS)
    locator = _Locator(handle.ws, client, {rel: index})
    name = _identifier_at(index, pos)
    return [_narrow_to_name(locator, loc, name) for loc in locator.locations(result)]


async def definition(handle: LspHandle, rel: RelPath, pos: Position) -> list[Location]:
    """``rel`` の ``pos`` にある名前の定義。サーバーが返した順。ワークスペース外は ``rel=None`` と ``external_uri``。

    範囲は名前（識別子）の範囲にそろえる。サーバーが宣言文全体を返したとき（vtsls）は、``pos`` の識別子と同じ綴りの
    最初の出現へ絞る（絞れなければそのまま）。``didOpen`` 直後の要求は解析待ちで 0.6 秒ほどかかる（S-4）。
    ``LSP_TIMEOUT_DEFINITION_MS`` に収まる。
    """
    client, index, uri = await _open(handle, rel)
    return await _definition_locations(handle, client, index, rel, uri, pos)


async def references(
    handle: LspHandle, rel: RelPath, pos: Position, *, include_declaration: bool = False
) -> list[RefLocation]:
    """``pos`` の名前の参照（ワークスペース内だけ。外は捨てる）。ファイル・位置の昇順、重複なし。

    サーバーには **常に** ``includeDeclaration=true`` で聞き、``include_declaration=False`` のときは ``definition`` の位置を
    こちらで引く。basedpyright は ``false`` のとき、宣言ではなく **カーソルの位置の参照** を落とし、宣言を残す
    （呼び出しの位置から聞くと、その呼び出しが結果から消える）ため。
    """
    client, index, uri = await _open(handle, rel)
    params = {
        "textDocument": {"uri": uri},
        "position": _lsp_position(handle, client, index, pos),
        "context": {"includeDeclaration": True},
    }
    result = await _call(handle, client, M_REFERENCES, params, LSP_TIMEOUT_REFERENCES_MS)
    found = {
        RefLocation(loc.rel, loc.range)
        for loc in _Locator(handle.ws, client, {rel: index}).locations(result)
        if loc.rel is not None
    }
    if not include_declaration and found:
        try:
            declared = await _definition_locations(handle, client, index, rel, uri, pos)
        except LuciankeError as e:
            if e.details.get("reason") != "lsp_error":
                raise
            declared = []
        starts = {(d.rel, d.range.start) for d in declared if d.rel is not None}
        found = {r for r in found if (r.rel, r.range.start) not in starts}
    return sorted(found, key=lambda r: (r.rel, r.range.start, r.range.end.line, r.range.end.col))


_FENCE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.DOTALL)


def _first_markup(contents: Any) -> str | None:
    """``Hover.contents``（``MarkupContent | MarkedString | MarkedString[]``）の先頭の部分を文字列にする。"""
    if isinstance(contents, list):
        contents = contents[0] if contents else None
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        value = contents.get("value")
        if isinstance(value, str):
            if "language" in contents:  # MarkedString {language, value}: 値がそのまま型の文字列
                return f"```{contents['language']}\n{value}\n```"
            return value
    return None


def hover_text(contents: Any) -> str | None:
    """Hover の中身から **型の文字列** を取り出す。

    markdown なら最初のコードフェンスの中身（basedpyright・tsls とも署名はここに入る）。フェンスが無ければ最初の
    ``---`` か空行の手前まで。空なら ``None``。
    """
    text = _first_markup(contents)
    if not text or not text.strip():
        return None
    fence = _FENCE.search(text)
    if fence is not None:
        body = fence.group(1).strip()
        return body or None
    head = re.split(r"\n\s*---+\s*\n|\n\s*\n", text.strip(), maxsplit=1)[0].strip()
    return head or None


async def hover(handle: LspHandle, rel: RelPath, pos: Position) -> str | None:
    """``pos`` の型の文字列（``hover_cache`` 用）。ホバーが無ければ ``None``。"""
    client, index, uri = await _open(handle, rel)
    params = {"textDocument": {"uri": uri}, "position": _lsp_position(handle, client, index, pos)}
    result = await _call(handle, client, M_HOVER, params, LSP_TIMEOUT_HOVER_MS)
    if not isinstance(result, dict):
        return None
    return hover_text(result.get("contents"))


# --- callHierarchy ----------------------------------------------------------------------


def _call_item(locator: _Locator, raw: Mapping[str, Any]) -> CallItem | None:
    uri = raw.get("uri")
    if not isinstance(uri, str) or "range" not in raw:
        return None
    full = locator.locate(uri, raw["range"])
    sel = locator.locate(uri, raw.get("selectionRange") or raw["range"])
    kind = raw.get("kind")
    detail = raw.get("detail")
    return CallItem(
        name=str(raw.get("name", "")),
        rel=full.rel,
        range=full.range,
        selection_range=sel.range,
        kind=kind if isinstance(kind, int) else None,
        detail=detail if isinstance(detail, str) else None,
        external_uri=full.external_uri,
        raw=dict(raw),
    )


async def prepare_call_hierarchy(
    handle: LspHandle, rel: RelPath, pos: Position, *, timeout_ms: float | None = None, strike: bool = True
) -> list[CallItem]:
    """``pos`` の関数・メソッドの ``CallHierarchyItem``（無ければ空）。

    ``timeout_ms`` を省くと ``LSP_TIMEOUT_CALL_HIERARCHY_MS``。T-07 の根の要求は 1 問い合わせの予算の残りを渡す（``strike`` は
    時間切れを連続時間切れに数えるか。``_call``。BUG-0091）。
    """
    client, index, uri = await _open(handle, rel)
    params = {"textDocument": {"uri": uri}, "position": _lsp_position(handle, client, index, pos)}
    limit = LSP_TIMEOUT_CALL_HIERARCHY_MS if timeout_ms is None else timeout_ms
    result = await _call(handle, client, M_PREPARE_CALL, params, limit, strike=strike)
    locator = _Locator(handle.ws, client, {rel: index})
    items = result if isinstance(result, list) else []
    return [it for raw in items if isinstance(raw, dict) and (it := _call_item(locator, raw)) is not None]


async def _calls(
    handle: LspHandle,
    method: str,
    item: CallItem,
    *,
    incoming: bool,
    timeout_ms: float | None = None,
    strike: bool = True,
) -> list[CallEdge]:
    if item.raw is None:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT, "CallItem has no raw LSP item", details={"param": "item"}
        )
    client = _require_ready(handle)
    handle.touch()
    limit = LSP_TIMEOUT_CALL_HIERARCHY_MS if timeout_ms is None else timeout_ms
    result = await _call(handle, client, method, {"item": dict(item.raw)}, limit, strike=strike)
    locator = _Locator(handle.ws, client)
    out: list[CallEdge] = []
    for entry in result if isinstance(result, list) else []:
        if not isinstance(entry, dict):
            continue
        other = _call_item(locator, entry.get("from" if incoming else "to") or {})
        if other is None:
            continue
        # fromRanges は呼び元の側のファイルの中の範囲（incoming は相手のファイル、outgoing は問い合わせた項目のファイル）。
        home_uri = entry["from"]["uri"] if incoming else item.raw.get("uri")
        located = [locator.locate(home_uri, r) for r in entry.get("fromRanges") or [] if isinstance(r, dict)]
        ranges = tuple(
            dict.fromkeys(loc.range for loc in located)
        )  # basedpyright は同じ範囲を重ねて返すことがある
        out.append(CallEdge(other, other.rel if incoming else item.rel, ranges))
    return out


async def incoming_calls(
    handle: LspHandle, item: CallItem, *, timeout_ms: float | None = None, strike: bool = True
) -> list[CallEdge]:
    """``item`` を呼ぶもの。``item`` は ``prepare_call_hierarchy`` の戻り値（``raw`` を含む）。"""
    return await _calls(handle, M_INCOMING, item, incoming=True, timeout_ms=timeout_ms, strike=strike)


async def outgoing_calls(
    handle: LspHandle, item: CallItem, *, timeout_ms: float | None = None, strike: bool = True
) -> list[CallEdge]:
    """``item`` が呼ぶもの。"""
    return await _calls(handle, M_OUTGOING, item, incoming=False, timeout_ms=timeout_ms, strike=strike)


# --- rename / codeAction / executeCommand -------------------------------------------------


def _provides_prepare_rename(client: LspClient) -> bool:
    provider = client.server_capabilities.get("renameProvider")
    return isinstance(provider, dict) and bool(provider.get("prepareProvider"))


async def rename(handle: LspHandle, rel: RelPath, pos: Position, new_name: str) -> dict[str, Any]:
    """``prepareRename`` → ``rename`` の ``WorkspaceEdit``（dict。ファイルは書き換えない）。

    ``prepareRename`` が拒否（``null`` またはエラー）したら ``E_LSP_UNAVAILABLE``（``details.reason="prepare_rename_rejected"``）。
    ``rename`` が ``null`` を返した場合は ``details.reason="rename_rejected"``。編集の形は basedpyright が ``documentChanges``、
    tsls が ``changes``（S-4・S-5）なので、呼び手の変換は両方を読むこと。
    """
    client, index, uri = await _open(handle, rel)
    text_document = {"uri": uri}
    position = _lsp_position(handle, client, index, pos)
    if _provides_prepare_rename(client):
        try:
            prepared = await _call(
                handle,
                client,
                M_PREPARE_RENAME,
                {"textDocument": text_document, "position": position},
                LSP_TIMEOUT_RENAME_MS,
            )
        except LuciankeError as e:
            if e.details.get("reason") != "lsp_error":
                raise
            prepared = None
        if prepared is None or (isinstance(prepared, dict) and prepared.get("defaultBehavior") is False):
            raise _unavailable(
                handle,
                "prepare_rename_rejected",
                f"the symbol at {rel}:{pos.line + 1}:{pos.col + 1} cannot be renamed",
            )
    edit = await _call(
        handle,
        client,
        M_RENAME,
        {"textDocument": text_document, "position": position, "newName": new_name},
        LSP_TIMEOUT_RENAME_MS,
    )
    if not isinstance(edit, dict):
        raise _unavailable(
            handle, "rename_rejected", f"the LSP returned no edit for renaming to {new_name!r}"
        )
    return edit


def _can_resolve(client: LspClient, action: Mapping[str, Any]) -> bool:
    provider = client.server_capabilities.get("codeActionProvider")
    if "data" in action:
        return True
    # data も command も無いものは、resolve で edit が付くかもしれない。command だけのものは resolve しない。
    return (
        action.get("command") is None and isinstance(provider, dict) and bool(provider.get("resolveProvider"))
    )


async def code_actions(
    handle: LspHandle,
    rel: RelPath,
    rng: Range,
    *,
    only: Sequence[str] | None = None,
    diagnostics: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """``rng`` の CodeAction（dict）。``edit`` が無いものは ``codeAction/resolve`` で付ける（ruff の organizeImports・fixAll）。

    * ``diagnostics`` は LSP の ``Diagnostic`` の dict（``DiagnosticsHub`` が受けたもの）。そのまま ``context`` に載せる。
    * ``resolve`` しても ``edit`` が無く ``command`` だけのものには ``COMMAND_ONLY_KEY: True`` を付ける。裸の ``Command`` も
      ``{"title", "command", COMMAND_ONLY_KEY}`` の形にして返す。実行は ``execute_command``。
    * ``resolve`` が失敗（サーバーのエラー）したものは、``edit`` 無しのまま返す。
    """
    client, index, uri = await _open(handle, rel)
    context: dict[str, Any] = {"diagnostics": [dict(d) for d in diagnostics]}
    if only:
        context["only"] = list(only)
    params = {
        "textDocument": {"uri": uri},
        "range": _lsp_range(handle, client, index, rng),
        "context": context,
    }
    result = await _call(handle, client, M_CODE_ACTION, params, LSP_TIMEOUT_CODE_ACTION_MS)
    out: list[dict[str, Any]] = []
    for item in result if isinstance(result, list) else []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("command"), str):  # 裸の Command {title, command, arguments}
            out.append(
                {
                    "title": item.get("title", ""),
                    "command": {k: item[k] for k in ("title", "command", "arguments") if k in item},
                    COMMAND_ONLY_KEY: True,
                }
            )
            continue
        action = dict(item)
        if action.get("edit") is None and _can_resolve(client, action):
            try:
                resolved = await _call(
                    handle, client, M_CODE_ACTION_RESOLVE, action, LSP_TIMEOUT_CODE_ACTION_MS
                )
            except LuciankeError as e:
                if e.details.get("reason") != "lsp_error":
                    raise
                _log.debug("codeAction/resolve failed for %r: %s", action.get("title"), e)
            else:
                if isinstance(resolved, dict):
                    action = resolved
        if action.get("edit") is None and action.get("command") is not None:
            action[COMMAND_ONLY_KEY] = True
        out.append(action)
    return out


async def execute_command(
    handle: LspHandle, command: str, arguments: Sequence[Any] | None = None
) -> CommandOutcome:
    """``workspace/executeCommand``。実行中にサーバーが ``workspace/applyEdit`` を要求したら、その WorkspaceEdit を
    書き換えずに ``edits`` に集める（サーバーには ``applied: true`` と答える。ファイルは書かない）。

    使い道: ``command`` だけの CodeAction（tsls の quickfix 後処理）、tsls の ``_typescript.organizeImports``（S-5）。
    """
    client = _require_ready(handle)
    cap = _capture_for(client)
    params: dict[str, Any] = {"command": command}
    if arguments is not None:
        params["arguments"] = list(arguments)
    handle.touch()
    async with cap.lock:
        start = len(cap.edits)
        cap.active += 1
        try:
            result = await _call(handle, client, M_EXECUTE_COMMAND, params, LSP_TIMEOUT_CODE_ACTION_MS)
        finally:
            cap.active -= 1
        edits = cap.edits[start:]
        del cap.edits[start:]
    return CommandOutcome(result, edits)


# --- PaaD への書き戻し -------------------------------------------------------------------

Locate = Callable[[RelPath, Range], Urn | str | None]
"""位置 → そこを含む最も内側のシンボルの URN（``symbols`` の範囲包含。ワークスペース外・見つからなければ ``None``）。"""


def to_exact_relations(
    source_urn: Urn | str,
    refs: Iterable[tuple[RelPath, Range]],
    *,
    locate: Locate,
    relation_kind: str = "CALLS",
    use_kind: str | None = None,
    call_site: Position | None = None,
    incoming: bool = False,
) -> list[RelationFact]:
    """LSP の結果を ``confidence=exact`` の ``RelationFact`` にする（``index.ingest.upsert_exact_relations`` に渡す）。

    * ``incoming=False``（既定。definition・outgoingCalls）: ``source_urn`` が **関係の元**。``refs`` の各位置を ``locate`` で
      URN にしたものが **先**。``call_site`` は元の側の呼び出し位置（definition を引いた位置など。内部表現）。
    * ``incoming=True``（references・incomingCalls）: ``source_urn`` は **先**（問い合わせたシンボル）。``refs`` の各位置は
      使用箇所で、``locate`` が返す URN が **元**、呼び出し位置は ``refs`` の範囲の開始。

    ``locate`` が ``None`` を返した位置（ワークスペース外・シンボルが無い）は捨てる。同じ組は 1 件にする。
    ``upsert_exact_relations`` は ``(元, 先, 種別)`` ごとに既存行を置き換えるので、1 つの問い合わせの結果は
    **まとめて 1 回** 渡すこと。
    """
    seen: set[tuple[str, str, int | None, int | None]] = set()
    out: list[RelationFact] = []
    for rel, rng in refs:
        other = locate(rel, rng)
        if other is None:
            continue
        if incoming:
            src, tgt, site = other, source_urn, rng.start
        else:
            src, tgt, site = source_urn, other, call_site
        key = (str(src), str(tgt), site.line if site else None, site.col if site else None)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            RelationFact(
                source_urn=src,
                target_urn=tgt,
                relation_kind=relation_kind,
                call_site_line=site.line if site is not None else None,
                call_site_col=site.col if site is not None else None,
                use_kind=use_kind,
                confidence=Confidence.EXACT,
            )
        )
    return out
