"""T-05 ``get_symbol_bodies``（仕様書 §11.3）。指定したシンボルの実装本体だけを AST ノード境界で切り出して返す。

縦切りの範囲: Python・TypeScript・JavaScript を構文木だけで（``structural``）。TargetRef は ``urn`` と ``path`` +
``symbol_name`` / ``line``。``include_referenced_symbols`` は索引が無いので ``refs:`` を出さない。

コード以外の対象（仕様書 §11 T-05 処理規約 3〜5、BUG-0049）:

* ``.ipynb`` の ``cell:<n>``（1 始まり。Markdown も数える）: ``cells[n].source`` の連結だけを ``code`` に返す（``outputs`` は読まない。
  MUST）。行は T-02 と同じ仮想 Python ファイル上の行。コードセルは ``Record.shrink`` / ``resume`` でコードと同じく続きを取れる。
  Markdown / raw セルは行を占めない（次のコードセルの先頭行に挟まる）ので、全文を 1 レコードで返し、行番号は付けない。
* 文法を結線していない言語・未知の拡張子（Tier 4）: 名前の字句一致（``syntax.heuristic.lexical_find``。定義らしい一致が先）の
  前後 15 行（最大 31 行）の窓を ``windows[]`` に最大 3 つ。他の窓に含まれる一致は飛ばす。水準は ``lexical_fallback``
  （``! DEGRADED`` と ``is_degraded`` はパイプラインがレコードの水準から付ける）。``path`` + ``line`` と、T-03 / T-06 が返す
  ``lex:NAME@Ln`` / ``ctags:NAME@Ln`` の URN は、その行を中心とする 1 窓。
* 上の以外のセレクタ（``xpath:`` など）と、データファイル・Markdown へのシンボル名は項目単位で ``E_UNSUPPORTED``。
  ``cell:`` を ``.ipynb`` 以外に使うのも ``E_UNSUPPORTED``。

``revision`` は ``WORKING_TREE``・``STAGED``・過去断面（``tools/_rev.py``。ファイルは Git オブジェクトから読み、作業ツリーに触れない）。

返す範囲は ``syntax.boundaries.definition_range``（デコレータ・直前の Doc コメントを含む）の前に ``context_lines`` 行を足したもの。
1 対象が残り予算に収まらないときは ``Record.shrink`` が先頭から詰めた版を返し、``truncated_at_line`` を記録する。
続きは ``next_cursor``（同じ対象を ``truncated_at_line`` から。``Record.resume`` とパッカー）か、``line_range`` で取る
（応答の ``continue_with`` と注記に ``start`` / ``end`` を書く）。前置き（``context_lines``）の途中では切らない。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import BATCH_MAX_ITEMS, CANDIDATES_MAX
from lucianke.core.model import Confidence, EngineChoice, Kind, Range, RelPath, SymbolRecord, Tier
from lucianke.core.records import Record, ToolOutput, split_offset
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import CellSel, CodeSel, DataSel, LexSel, TagSel, Urn, parse, relax
from lucianke.syntax.adapters import ipynb
from lucianke.syntax.adapters._common import SelectorError, UnsupportedSelectorError
from lucianke.syntax.boundaries import definition_range
from lucianke.syntax.heuristic import BLOCK_WINDOW_RADIUS, block_window, lexical_find
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._data_files import DATA_FILE_MAX_BYTES, adapter_for
from lucianke.tools._rev import LoadedFile, RevisionView, analyze_at, open_revision, read_checked
from lucianke.tools._services import (
    SYMBOL_LANGUAGES,
    FileAnalysis,
    ToolServices,
    batch_warnings,
    estimate_tokens,
    services_of,
)
from lucianke.tools.t02_get_file_skeleton import failure_record
from lucianke.workspace.languages import detect
from lucianke.workspace.paths import confine

_SELECTOR_PREFIXES = ("xpath:", "jsonpath:", "jq:", "toml:", "yaml:", "cell:")
_OTHER_SELECTOR_PREFIXES = tuple(p for p in _SELECTOR_PREFIXES if p != "cell:")  # cell: は .ipynb の部分取得として受ける
_CELL_RE = re.compile(r"cell:(\d+)")
_MARKUP = frozenset({"markdown", "rst"})
_ENGINE_TREE = "tree-sitter"
_ENGINE_CELL = "parser:ipynb"
_ENGINE_LEXICAL = "internal_ripgrep_word_boundary"  # 仕様書 §8.4 段 4 の engine_used（engine.selector.ENGINE_LEXICAL_GREP と同じ）
_ENGINE_ORDER = (_ENGINE_TREE, _ENGINE_CELL, _ENGINE_LEXICAL)
_WINDOWS_MAX = 3  # 仕様書 §11 T-05 処理規約 5: lexical_fallback の窓は最大 3 つ


class TargetRefArg(BaseModel):
    """仕様書 §5.3 の TargetRef。外部の ``line`` / ``character`` は 1 始まり。"""

    model_config = ConfigDict(extra="forbid")

    urn: str | None = Field(default=None, description="From another tool's output.")
    path: str | None = Field(default=None)
    symbol_name: str | None = Field(default=None, description="\"name\" or \"Outer.name\".")
    line: int | None = Field(default=None, ge=1)
    character: int | None = Field(default=None, ge=1)


class LineRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> LineRange:
        if self.start > self.end:
            raise ValueError("line_range: start must be <= end")
        return self


class GetSymbolBodiesArgs(CommonArgs):
    targets: list[TargetRefArg] = Field(min_length=1, max_length=BATCH_MAX_ITEMS)
    context_lines: int = Field(default=2, ge=0, le=20, description="Extra lines before the body.")
    with_line_numbers: bool = Field(default=True)
    include_referenced_symbols: bool = Field(
        default=True
    )
    line_range: LineRange | None = Field(default=None, description="1-based range within the body.")


# --- 解決（仕様書 §5.3） ---------------------------------------------------------------------


def _candidate(s: SymbolRecord) -> dict[str, Any]:
    return {"urn": str(s.urn), "signature": s.signature, "path": str(s.path), "line": s.range.start.line + 1}


def _ambiguous(name: str, found: list[SymbolRecord]) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_TARGET_AMBIGUOUS,
        f"{name!r} matches {len(found)} symbols; use a longer symbol_name, a urn, or a line",
        candidates=[_candidate(s) for s in found[:CANDIDATES_MAX]],
    )


def _by_name(an: FileAnalysis, name: str) -> SymbolRecord:
    """``qualified_name`` の末尾一致。完全一致が 1 件ならそれを優先する。0 件は NOT_FOUND、2 件以上は AMBIGUOUS。"""
    exact = [s for s in an.symbols if s.qualified_name == name]
    if len(exact) == 1:
        return exact[0]
    found = exact or [s for s in an.symbols if s.qualified_name.endswith("." + name)]
    if not found:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"symbol {name!r} not found in {an.rel}",
            details={"path": str(an.rel), "symbol_name": name},
        )
    if len(found) > 1:
        raise _ambiguous(name, found)
    return found[0]


def _by_line(an: FileAnalysis, line: int) -> SymbolRecord:
    inside = [s for s in an.symbols if s.range.line_span_external()[0] <= line <= s.range.line_span_external()[1]]
    if not inside:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"no symbol contains line {line} of {an.rel}",
            details={"path": str(an.rel), "line": line},
        )
    return min(inside, key=lambda s: (s.range.line_span_external()[1] - s.range.line_span_external()[0], -s.range.start.line))


def _by_urn(an: FileAnalysis, urn: Urn) -> SymbolRecord:
    wanted = str(urn)
    for s in an.symbols:
        if str(s.urn) == wanted:
            return s
    loose = str(relax(urn))
    found = [s for s in an.symbols if str(relax(s.urn)) == loose]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise _ambiguous(wanted, found)
    raise LuciankeError(ErrorCode.E_TARGET_NOT_FOUND, f"no symbol with urn {wanted}", details={"urn": wanted})


async def _resolve(svc: ToolServices, ws: Any, view: RevisionView, ref: TargetRefArg, cache: dict[str, FileAnalysis]) -> tuple[FileAnalysis, SymbolRecord]:
    async def load(path: str) -> FileAnalysis:
        if path not in cache:
            cache[path] = await analyze_at(svc, ws, view, path, param="targets")
        return cache[path]

    if ref.urn is not None:  # urn > line > symbol_name（§5.3 規則 4）
        try:
            urn = parse(ref.urn)
        except ValueError as exc:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT, f"targets.urn: {exc}", details={"param": "targets.urn"}
            ) from None
        if urn.workspace_id != ws.workspace_id or not isinstance(urn.selector, CodeSel):
            raise LuciankeError(
                ErrorCode.E_TARGET_NOT_FOUND,
                f"urn does not name a code symbol of workspace {ws.workspace_id!r}: {ref.urn}",
                details={"urn": ref.urn},
            )
        an = await load(urn.path)
        return an, _by_urn(an, urn)
    if ref.path is None:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            "targets: give urn, or path with symbol_name or line",
            details={"param": "targets"},
        )
    if ref.line is not None:
        an = await load(ref.path)
        return an, _by_line(an, ref.line)
    if ref.symbol_name is not None:
        if ref.symbol_name.startswith(_SELECTOR_PREFIXES):
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"selector {ref.symbol_name.split(':', 1)[0]}: is not supported in this build (code symbols only)",
                details={"reason": "selector_not_supported"},
            )
        an = await load(ref.path)
        return an, _by_name(an, ref.symbol_name)
    raise LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT,
        "targets: path needs symbol_name or line (or use urn)",
        details={"param": "targets"},
    )


# --- 本体の切り出し -------------------------------------------------------------------------


def body_lines(an: FileAnalysis, sym: SymbolRecord) -> tuple[int, int]:
    """本体の行範囲（1 始まり・両端を含む）。デコレータ・直前の Doc コメントを含む（``context_lines`` は含まない）。"""
    index = an.parsed.line_index
    start = index.position_to_offset(sym.range.start).value
    end = index.position_to_offset(sym.range.end).value
    node = an.parsed.tree.root_node.descendant_for_byte_range(start, end)
    return definition_range(node, an.parsed, language_id=an.language_id).line_span_external()


@dataclass(frozen=True, slots=True)
class _Body:
    """``_record`` が行を取る元。コードは構文木の行表、``.ipynb`` のコードセルは仮想 Python ファイルの行表。

    ``verbatim`` は ``(first, last, text)``: ``first``〜``last`` をそのまま返すとき ``code`` に使う原文（``cells[n].source``
    の連結。行の連結では末尾の改行を失うため）。``extra`` は項目（json）に足す欄。
    """

    rel: str
    sha256: str
    language_id: str
    line_text: Any  # Callable[[int], str]: 0 始まりの行番号 -> 行の本文
    confidence: Confidence = Confidence.STRUCTURAL
    verbatim: tuple[int, int, str] | None = None
    extra: Any = None  # Mapping[str, Any] | None


def _body_of(an: FileAnalysis) -> _Body:
    return _Body(str(an.rel), an.sha256, an.language_id, an.parsed.line_index.line_text)


def _record(
    svc: ToolServices,
    an: _Body,
    sym: SymbolRecord,
    args: GetSymbolBodiesArgs,
    request: dict[str, Any],
    priority: int,
    *,
    first: int,
    last: int,
    context_before: int,
    cut_at: int | None = None,
) -> Record:
    """``first``〜``last``（1 始まり・両端を含む）を返す 1 レコード。``cut_at`` は予算で切った最後の行の次の行。"""
    L = svc.lines
    stop = last if cut_at is None else cut_at - 1
    code_rows = [an.line_text(i - 1) for i in range(first, stop + 1)]
    rel = an.rel
    head = L.symbol_line(sym.kind, f"{rel}#{sym.qualified_name}", start_line=first, end_line=stop)
    lines = [head, L.attr_line("sha", an.sha256[:12], indent=0)]
    lines += L.code_block(code_rows, lang=an.language_id, start_line=first, with_line_numbers=args.with_line_numbers)
    if cut_at is not None:
        lines.append(L.note(f"truncated at line {cut_at} of {last}: continue with the cursor, or line_range {cut_at}-{last}"))
    item = {
        "ok": True,
        "request": request,
        "symbol": sym.to_json(),
        "range_returned": {"start_line": first, "end_line": stop},
        "file_sha256": an.sha256,
        "context_before_lines": context_before,
        "code": "\n".join(code_rows),
        "truncated": cut_at is not None,
        "referenced_symbols": [],
        "confidence_level": an.confidence.value,
    }
    if an.verbatim is not None and (first, stop) == an.verbatim[:2]:
        item["code"] = an.verbatim[2]
    if an.extra:
        item.update(an.extra)
    if cut_at is not None:
        item["truncated_at_line"] = cut_at
        item["continue_with"] = {"line_range": {"start": cut_at, "end": last}}
    shrink = None
    body_first = first + context_before  # 前置き（context_lines）の行は、本体の先頭が入らないなら切らない（続きに隙間ができる）
    if cut_at is None and stop > body_first:

        def shrink(budget: int) -> Record | None:
            """先頭から詰める（本体を最低 1 行）。入らなければ ``None``（次のページへ）。"""
            lo, hi, best = body_first, stop - 1, None
            while lo <= hi:  # 入る最大の行数を二分探索
                mid = (lo + hi) // 2
                rec = _record(svc, an, sym, args, request, priority, first=first, last=last,
                              context_before=context_before, cut_at=mid + 1)  # fmt: skip
                if estimate_tokens("\n".join(rec.lines)) <= budget:
                    best, lo = rec, mid + 1
                else:
                    hi = mid - 1
            return best

    return Record(
        kind="code",
        key=f"{rel}#{sym.qualified_name}",
        priority=priority,
        lines=tuple(lines),
        data=item,
        confidence=an.confidence,
        group=rel,
        shrink=shrink,
        resume=cut_at,
    )


def build_record(svc: ToolServices, an: FileAnalysis, sym: SymbolRecord, args: GetSymbolBodiesArgs,
                 request: dict[str, Any], priority: int, resume: int | None = None) -> Record:  # fmt: skip
    """``resume`` はカーソルが持つ再開行（前のページが切った行の次。``Record.resume``）。その行から本体の終わりまで。"""
    first, last = body_lines(an, sym)
    context_before = 0
    if args.line_range is not None:
        first, last = max(first, args.line_range.start), min(last, args.line_range.end)
        if first > last:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT,
                f"line_range {args.line_range.start}-{args.line_range.end} is outside the body "
                f"({first}-{last} after clamping)",
                details={"param": "line_range"},
            )
    else:
        context_before = min(args.context_lines, first - 1)
        while context_before and not an.parsed.line_index.line_text(first - context_before - 1).strip():
            context_before -= 1  # 前置きの先頭の空行は価値が低いので付けない
        first -= context_before
    if resume is not None:  # 続き: 前のページが返した行の次から。前置きは付けない
        if not first < resume <= last:
            raise LuciankeError(
                ErrorCode.E_CURSOR_EXPIRED,
                f"the body of {sym.qualified_name} changed since the cursor was issued; call again from the first page",
            )
        first, context_before = resume, 0
    return _record(
        svc, _body_of(an), sym, args, request, priority, first=first, last=last, context_before=context_before
    )


# --- コード以外の対象: .ipynb のセル・Tier 4 の字句窓（仕様書 §11 T-05 処理規約 3〜5、BUG-0049） -----------------


@dataclass(frozen=True, slots=True)
class _Done:
    """コード以外の対象 1 件の結果。"""

    record: Record
    language_id: str
    tier: Tier
    engine: str


def _plan(ws: Any, ref: TargetRefArg) -> tuple[str, str | None, int | None, bool] | None:
    """この節が受ける対象なら ``(path, symbol_name, line, 字句の URN か)``。コードの経路（``_resolve``）に任せるなら ``None``。

    ``urn`` > ``line`` > ``symbol_name``（§5.3 規則 4）。``lex:`` / ``ctags:`` の URN（T-03・T-06 が Tier 4 に付ける）は、
    その行を中心とする窓として受ける。``xpath:`` など ``cell:`` 以外のセレクタは ``_resolve`` が項目単位で断る。
    """
    if ref.urn is not None:
        try:
            urn = parse(ref.urn)
        except ValueError:
            return None  # 構文の誤りは _resolve が E_INVALID_ARGUMENT にする
        sel = urn.selector
        if urn.workspace_id == ws.workspace_id and isinstance(sel, LexSel | TagSel):
            return urn.path, sel.name, sel.line, True
        return None
    if ref.path is None:
        return None
    if ref.line is not None:
        return ref.path, ref.symbol_name, ref.line, False
    name = ref.symbol_name
    if name is None:
        return None
    # xpath: / jsonpath: / jq: / toml: / yaml: selectors: pass through to _non_code for adapter dispatch
    return ref.path, name, None, False


def _not_applicable(selector: str, rel: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_UNSUPPORTED,
        f"selector {selector.split(':', 1)[0]}: applies to .ipynb files only ({rel})",
        details={"reason": "selector_not_applicable", "path": rel},
    )


# --- セレクタ（xpath:/jsonpath:/jq:/toml:/yaml:）とファイル種別の対応 -----------

# selector prefix -> acceptable adapter language_ids
_SELECTOR_ADAPTER_MAP: dict[str, frozenset[str]] = {
    "xpath": frozenset({"xml"}),
    "jsonpath": frozenset({"json", "jsonl"}),
    "jq": frozenset({"json", "jsonl"}),
    "toml": frozenset({"toml"}),
    "yaml": frozenset({"yaml"}),
}

_SELECTOR_PREFIXES_DATA = tuple(_SELECTOR_ADAPTER_MAP.keys())


def _data_selector_prefix(name: str) -> str | None:
    """``name`` が ``xpath:`` などのデータセレクタなら接頭辞（``"xpath"`` など）を返す。違えば ``None``。"""
    col = name.find(":")
    if col < 0:
        return None
    pfx = name[:col].lower()
    return pfx if pfx in _SELECTOR_ADAPTER_MAP else None


async def _selector(
    svc: ToolServices, ws: Any, loaded: Any, selector: str, prefix: str, adapter: Any,
    args: GetSymbolBodiesArgs, request: dict[str, Any], priority: int,
) -> _Done:  # fmt: skip
    """xpath: / jsonpath: / toml: / yaml: をアダプタの select() に委譲し、一致を 1 件にまとめて返す。

    * 一致が 0 件: ``E_TARGET_NOT_FOUND``
    * 一致が複数: 最初の 20 件を 1 項目内に並べ、``match_count`` に総数を書く（仕様書 §11 T-05 処理規約 3）
    * ファイル種別に合わないセレクタ: ``E_UNSUPPORTED``
    * 構文誤り: ``E_INVALID_ARGUMENT``
    """
    rel = str(loaded.rel)
    accepted = _SELECTOR_ADAPTER_MAP[prefix]
    if adapter.language_id not in accepted:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"selector {prefix}: does not apply to {adapter.language_id} files ({rel})",
            details={"reason": "selector_not_applicable", "path": rel},
        )
    try:
        matches = await asyncio.to_thread(adapter.select, loaded.data, selector)
    except UnsupportedSelectorError as exc:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            str(exc),
            details={"reason": "selector_not_supported", "path": rel},
        ) from exc
    except SelectorError as exc:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            f"targets.symbol_name: {exc}",
            details={"param": "targets.symbol_name"},
        ) from exc
    if not matches:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"selector {selector!r} matched nothing in {rel}",
            details={"path": rel, "symbol_name": selector},
        )
    match_count = len(matches)
    displayed = matches[:20]
    # Build a SymbolRecord from the first match for the key / URN
    first = displayed[0]
    first_start, first_end = first.range.line_span_external()
    first_kind = Kind.parse(first.kind)
    sym = SymbolRecord(
        urn=Urn(ws.workspace_id, rel, DataSel(prefix, selector.split(":", 1)[1])),
        path=RelPath(rel),
        name=selector,
        qualified_name=selector,
        kind=first_kind,
        range=Range.from_line_span_external(first_start, first_end),
        confidence=Confidence.STRUCTURAL,
    )
    L = svc.lines
    source_lines_list = loaded.data.decode("utf-8", errors="replace").splitlines()
    lang = re.sub(r"[\s`]", "", adapter.language_id) or None
    lines_out: list[str] = []
    items_data: list[dict[str, Any]] = []
    for k, m in enumerate(displayed):
        s, e = m.range.line_span_external()
        rows = source_lines_list[s - 1 : e]
        match_kind = Kind.parse(m.kind)
        match_name = m.name or selector
        lines_out.append(L.symbol_line(match_kind, f"{rel}#{match_name}", start_line=s, end_line=e))
        if k == 0:
            lines_out.append(L.attr_line("sha", loaded.sha256[:12], indent=0))
        lines_out += L.code_block(rows, lang=lang, start_line=s, with_line_numbers=args.with_line_numbers)
        items_data.append({"name": match_name, "range": {"start_line": s, "end_line": e}, "code": "\n".join(rows), **m.data})
    item = {
        "ok": True,
        "request": request,
        "symbol": sym.to_json(),
        "range_returned": {"start_line": first_start, "end_line": first_end},
        "file_sha256": loaded.sha256,
        "context_before_lines": 0,
        "code": items_data[0]["code"],
        "truncated": False,
        "referenced_symbols": [],
        "confidence_level": Confidence.STRUCTURAL.value,
        "match_count": match_count,
        "matches": items_data,
    }
    rec = Record(
        kind="code",
        key=f"{rel}#{selector}",
        priority=priority,
        lines=tuple(lines_out),
        data=item,
        confidence=Confidence.STRUCTURAL,
        group=rel,
    )
    return _Done(rec, adapter.language_id, Tier.T3, f"adapter:{adapter.language_id}")


async def _non_code(
    svc: ToolServices, ws: Any, view: RevisionView, ref: TargetRefArg, args: GetSymbolBodiesArgs,
    request: dict[str, Any], priority: int, resume: int | None,
) -> _Done | None:  # fmt: skip
    """``cell:<n>``、データセレクタ（xpath:/jsonpath:/toml:/yaml:）、Tier 4 の窓。コードの経路に任せる対象は ``None``。"""
    plan = _plan(ws, ref)
    if plan is None:
        return None
    path, name, line, from_urn = plan
    rel = str(confine(ws, path, param="targets"))
    is_cell = line is None and name is not None and name.startswith("cell:")
    if not is_cell and not from_urn and detect(RelPath(rel), b"").language_id in SYMBOL_LANGUAGES:
        return None  # 名前でコードと決まる: 読み込みは analyze_at に任せる（二重に読まない）
    adapter = adapter_for(rel)
    if is_cell:
        assert name is not None
        if adapter is None or adapter.language_id != "ipynb":
            raise _not_applicable(name, rel)
        loaded = await read_checked(view, ws, path, param="targets", max_bytes=DATA_FILE_MAX_BYTES)
        return await _cell(svc, ws, loaded, name, args, request, priority, resume)
    # --- データセレクタ（xpath: / jsonpath: / jq: / toml: / yaml:） ---
    if name is not None:
        sel_prefix = _data_selector_prefix(name)
        if sel_prefix is not None:
            if adapter is None:
                raise LuciankeError(
                    ErrorCode.E_UNSUPPORTED,
                    f"selector {sel_prefix}: does not apply to this file type ({rel})",
                    details={"reason": "selector_not_applicable", "path": rel},
                )
            loaded = await read_checked(view, ws, path, param="targets", max_bytes=DATA_FILE_MAX_BYTES)
            return await _selector(svc, ws, loaded, name, sel_prefix, adapter, args, request, priority)
    loaded = await read_checked(view, ws, path, param="targets")
    language_id = loaded.detected.language_id
    if not from_urn and (language_id in SYMBOL_LANGUAGES or adapter is not None or language_id in _MARKUP):
        return None  # シバンで決まるコード・データファイル・Markdown: _resolve が従来の断り（または構文木の解決）を返す
    return _lexical(svc, ws, loaded, name, line, args, request, priority)


async def _cell(
    svc: ToolServices, ws: Any, loaded: LoadedFile, selector: str, args: GetSymbolBodiesArgs,
    request: dict[str, Any], priority: int, resume: int | None,
) -> _Done:  # fmt: skip
    """``cell:<n>``: ``cells[n].source`` の連結だけを返す（``outputs`` は読まない）。行は仮想 Python ファイル上の行。"""
    rel = str(loaded.rel)
    m = _CELL_RE.fullmatch(selector.strip())
    if m is None or int(m[1]) < 1:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            f"targets.symbol_name: selector {selector!r} must be 'cell:<n>' with n >= 1 (1-based; markdown cells count)",
            details={"param": "targets.symbol_name"},
        )
    number = int(m[1])
    try:
        virtual, cmap = await asyncio.to_thread(ipynb.to_virtual_python, loaded.data)
    except (ValueError, RecursionError) as exc:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"{rel}: cannot be read as ipynb: {exc}",
            details={"reason": "invalid_data_file", "language_id": "ipynb"},
        ) from exc
    if number > len(cmap.cells):
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"cell:{number} is out of range: {rel} has {len(cmap.cells)} cells",
            details={"path": rel, "symbol_name": selector},
        )
    cell = cmap.cells[number - 1]
    match = ipynb.select(loaded.data, f"cell:{number}")[0]  # 原文（source の連結）。outputs / metadata は読まない
    start, end = match.range.line_span_external()
    sym = SymbolRecord(
        urn=Urn(ws.workspace_id, rel, CellSel(number)),
        path=RelPath(rel),
        name=f"cell:{number}",
        qualified_name=f"cell:{number}",
        kind=Kind.CELL,
        range=match.range,
        confidence=Confidence.STRUCTURAL,
    )
    extra = {
        "match_count": 1,
        "cell": {
            "number": number,
            "cell_type": cell.kind,
            "exec_count": cell.exec_count,
            "in_virtual_file": cell.is_code,
        },
    }
    if cell.is_code:
        first, last = start, end
        if args.line_range is not None:
            first, last = max(first, args.line_range.start), min(last, args.line_range.end)
            if first > last:
                raise LuciankeError(
                    ErrorCode.E_INVALID_ARGUMENT,
                    f"line_range {args.line_range.start}-{args.line_range.end} is outside the cell ({start}-{end})",
                    details={"param": "line_range"},
                )
        if resume is not None:  # 続き: 前のページが切った行の次から（BUG-0016 と同じ規約）
            if not first < resume <= last:
                raise LuciankeError(
                    ErrorCode.E_CURSOR_EXPIRED,
                    f"cell:{number} of {rel} changed since the cursor was issued; call again from the first page",
                )
            first = resume
        body = _Body(
            rel, loaded.sha256, "python", LineIndex(virtual).line_text, verbatim=(start, end, match.text), extra=extra
        )
        rec = _record(svc, body, sym, args, request, priority, first=first, last=last, context_before=0)
        return _Done(rec, "ipynb", Tier.T2, _ENGINE_CELL)
    # Markdown / raw セルは仮想ファイルの行を占めない。次のコードセルの先頭行に挟まるので、全文を 1 レコードで返す
    L = svc.lines
    rows = re.split(r"\r\n|\r|\n", match.text)
    lines = [
        L.symbol_line(Kind.CELL, f"{rel}#cell:{number}", start_line=start, end_line=start),
        L.attr_line("sha", loaded.sha256[:12], indent=0),
        *L.code_block(rows, lang=cell.kind, with_line_numbers=False),
        L.note("not part of the virtual Python file: @L is the first line of the next code cell"),
    ]
    item = {
        "ok": True,
        "request": request,
        "symbol": sym.to_json(),
        "range_returned": {"start_line": start, "end_line": start},
        "file_sha256": loaded.sha256,
        "context_before_lines": 0,
        "code": match.text,
        "truncated": False,
        "referenced_symbols": [],
        "confidence_level": Confidence.STRUCTURAL.value,
        **extra,
    }
    rec = Record(
        kind="code",
        key=f"{rel}#cell:{number}",
        priority=priority,
        lines=tuple(lines),
        data=item,
        confidence=Confidence.STRUCTURAL,
        group=rel,
    )
    return _Done(rec, "ipynb", Tier.T2, _ENGINE_CELL)


def _lexical(
    svc: ToolServices, ws: Any, loaded: LoadedFile, name: str | None, line: int | None, args: GetSymbolBodiesArgs,
    request: dict[str, Any], priority: int,
) -> _Done:  # fmt: skip
    """Tier 4: 一致行を中心に前後 15 行（最大 31 行）の窓を最大 3 つ。水準は ``lexical_fallback``（``! DEGRADED`` は自動）。"""
    L = svc.lines
    rel = str(loaded.rel)
    data = loaded.data
    index = LineIndex(data)
    centers: list[tuple[int, bool | None]]  # (0 始まりの行, 定義らしいか)
    if line is not None:
        total = index.content_line_count
        if line > total:
            raise LuciankeError(
                ErrorCode.E_TARGET_NOT_FOUND,
                f"line {line} is beyond the end of {rel} ({total} lines)",
                details={"path": rel, "line": line},
            )
        centers = [(line - 1, None)]
        match_count = 1
    else:
        assert name is not None
        try:
            hits = lexical_find(data, name, context=0)  # 定義らしい一致が先、同じ組の中は行の順
        except ValueError as exc:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT, f"targets.symbol_name: {exc}", details={"param": "targets.symbol_name"}
            ) from None
        if not hits:
            raise LuciankeError(
                ErrorCode.E_TARGET_NOT_FOUND,
                f"symbol {name!r} not found in {rel} (no word-boundary match)",
                details={"path": rel, "symbol_name": name},
            )
        centers = [(h.line, h.is_definition) for h in hits]
        match_count = len(hits)
    label = name or f"L{line}"
    windows: list[dict[str, Any]] = []
    rows_of: list[list[str]] = []
    for ln, is_def in centers:
        if any(w["start_line"] <= ln + 1 <= w["end_line"] for w in windows):
            continue  # 前の窓に入っている一致は、別の窓にしない
        a, b = block_window(data, ln, radius=BLOCK_WINDOW_RADIUS).line_span_external()
        rows = [index.line_text(i - 1) for i in range(a, b + 1)]
        rows_of.append(rows)
        windows.append(
            {"start_line": a, "end_line": b, "match_line": ln + 1, "is_definition": is_def, "code": "\n".join(rows)}
        )
        if len(windows) == _WINDOWS_MAX:
            break
    lang = re.sub(r"[\s`]", "", loaded.detected.language_id) or None
    lines: list[str] = []
    for k, (w, rows) in enumerate(zip(windows, rows_of, strict=True)):
        lines.append(L.symbol_line(Kind.SECTION, f"{rel}#{label}", start_line=w["start_line"], end_line=w["end_line"]))
        if k == 0:
            lines.append(L.attr_line("sha", loaded.sha256[:12], indent=0))
        lines += L.code_block(rows, lang=lang, start_line=w["start_line"], with_line_numbers=args.with_line_numbers)
        how = "definition-like match" if w["is_definition"] else "match" if w["is_definition"] is False else "line"
        lines.append(L.note(f"lexical {how} at line {w['match_line']}; window {w['start_line']}-{w['end_line']}"))
    head = windows[0]
    sym = SymbolRecord(
        urn=Urn(ws.workspace_id, rel, LexSel(label, head["match_line"])),
        path=RelPath(rel),
        name=label,
        qualified_name=label,
        kind=Kind.SECTION,
        range=Range.from_line_span_external(head["match_line"], head["match_line"]),
        confidence=Confidence.LEXICAL_FALLBACK,
    )
    item = {
        "ok": True,
        "request": request,
        "symbol": sym.to_json(),
        "range_returned": {"start_line": head["start_line"], "end_line": head["end_line"]},
        "file_sha256": loaded.sha256,
        "context_before_lines": 0,
        "code": head["code"],
        "truncated": False,
        "referenced_symbols": [],
        "confidence_level": Confidence.LEXICAL_FALLBACK.value,
        "windows": windows,
        "match_count": match_count,
    }
    rec = Record(
        kind="code",
        key=f"{rel}#{label}",
        priority=priority,
        lines=tuple(lines),
        data=item,
        confidence=Confidence.LEXICAL_FALLBACK,
        group=rel,
    )
    return _Done(rec, loaded.detected.language_id, Tier.T4, _ENGINE_LEXICAL)


def _engine_name(view: RevisionView, engines: set[str]) -> str:
    """``engine_used``: 使った経路を ``_ENGINE_ORDER`` の順に ``+`` でつなぐ（コードだけなら ``tree-sitter``）。"""
    used = [e for e in _ENGINE_ORDER if e in engines] or [_ENGINE_TREE]
    return "+".join(view.engine_name(e) for e in used)


async def handler(ctx: ToolContext, args: GetSymbolBodiesArgs) -> ToolOutput:
    svc = services_of(ctx.services)
    ws = svc.workspace(args.workspace_id)
    view = await open_revision(ws, args.revision)  # 解決できなければ E_REVISION_NOT_FOUND（作業ツリーは Git に触れない）
    cache: dict[str, FileAnalysis] = {}
    records: list[Record] = []
    errors: list[tuple[str, LuciankeError]] = []
    languages: list[str] = []
    engines: set[str] = set()
    tier = Tier.T1
    resume_index, resume_line = split_offset(ctx.cursor_offset)  # カーソルが途中で切った対象と、その続きの行
    for i, ref in enumerate(args.targets):
        request = ref.model_dump(exclude_none=True)
        label = ref.urn or (f"{ref.path}#{ref.symbol_name}" if ref.symbol_name else f"{ref.path}:{ref.line}")
        try:
            resume = resume_line if i == resume_index else None
            done = await _non_code(svc, ws, view, ref, args, request, i, resume)
            if done is not None:  # .ipynb のセル・Tier 4 の窓
                records.append(done.record)
                engines.add(done.engine)
                if done.language_id not in languages:
                    languages.append(done.language_id)
                tier = max(tier, done.tier)
                continue
            an, sym = await _resolve(svc, ws, view, ref, cache)
            engines.add(_ENGINE_TREE)
            records.append(build_record(svc, an, sym, args, request, i, resume_line if i == resume_index else None))
            if an.language_id not in languages:
                languages.append(an.language_id)
            tier = max(tier, an.tier)
        except LuciankeError as err:
            errors.append((label, err))
            rec = failure_record(label, err, i, svc.lines.banner)
            if err.candidates:  # 曖昧のときは候補を 1 行 1 件で添える（仕様書 §5.4.2）
                rec = Record(
                    kind=rec.kind, key=rec.key, priority=rec.priority, group=rec.group, confidence=rec.confidence,
                    lines=(*rec.lines, *(svc.lines.candidate_line(c) for c in err.candidates)), data=rec.data,
                )  # fmt: skip
            records.append(rec)
    if errors and len(errors) == len(args.targets):
        _, first = errors[0]
        raise LuciankeError(
            first.code,
            first.message,
            details={**first.details, "items": [{"ok": False, "request": r.model_dump(exclude_none=True), "error": e.to_json()}
                                                for r, (_, e) in zip(args.targets, errors, strict=True)]},
            candidates=first.candidates,
        )  # fmt: skip
    level = min((r.confidence for r in records if r.data.get("ok")), default=Confidence.STRUCTURAL)
    return ToolOutput(
        records=records,
        max_confidence=Confidence.STRUCTURAL,
        engine=EngineChoice(engine=_engine_name(view, engines), confidence=level),
        page_unit="target",
        tier=tier,
        detected_language=",".join(languages) if languages else "n/a",
        warnings=(*view.warnings, *batch_warnings(len(errors), len(args.targets))),
    )


SPEC = ToolSpec(
    name="get_symbol_bodies",
    tid="T-05",
    args_model=GetSymbolBodiesArgs,
    handler=handler,
    default_max_tokens=3000,
    description=(
        "Implementation bodies of 1-20 symbols cut at AST node boundaries, with decorators, the doc comment above, @L line numbers and the file sha. Python/TS/JS (syntax tree only). Prefer this over reading the whole file."
    ),
    max_confidence=Confidence.STRUCTURAL,
    milestone="M1",
    page_unit="target",
    records_key="items",
)
