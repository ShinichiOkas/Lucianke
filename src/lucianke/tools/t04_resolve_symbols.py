"""T-04 ``resolve_symbols``（仕様書 §11.3）。最大 20 件の TargetRef の定義位置・シグネチャ・Doc 全文・型の定義 URN を一括で返す。

``structural``（処理規約 3）: PaaD の宣言（``index.queries``）と、import 文のモジュールパス → ワークスペース内ファイルの対応
（``syntax.imports``）で定義を引く。型の展開はせず、``signature_expanded`` は ``signature`` と同じ。

``exact``（処理規約 2、``tools/_t04_exact.py``）: 解決したシンボルを、``_hover_cache`` に行があればそこから（LSP を呼ばない。
設計書 R-11）、無ければ LSP が ``ready`` のときだけ ``definition`` と ``hover`` で上げる（``signature_expanded`` は hover の型文字列。
結果は ``_hover_cache`` に書き戻す）。LSP が ``ready`` でなければ **待たずに** ``structural``（``standby`` は起動を促すだけ）。
タイムアウト・``position_clamped`` など ``E_LSP_UNAVAILABLE`` は ``engine.selector.on_lsp_failure`` で ``structural`` に降格する。
``scip_symbol`` は後で別の役が足す。

* TargetRef（§5.3）は索引で解く。``urn`` > ``line`` > ``symbol_name``。``symbol_name`` がそのファイルに無く、import された名前なら、
  import 表から定義元ファイルのシンボルへ（``follow_aliases`` が真なら再エクスポートの連鎖も、上限 ``MAX_HOPS`` 段まで）。
  ``import x as y`` の別名・``pkg.name`` のようなドット付きも扱う。モジュールそのものを指す名前は ``Module`` として返す。
  ワークスペース外のモジュールの名前は ``E_TARGET_NOT_FOUND`` ではなく ``definition: null`` と ``warnings``（§11 T-04「精度と降格」）。
* ``definition.range`` は定義ファイルを構文木で解析し直し、``get_symbol_bodies`` と同じ ``body_lines`` で求める（事後条件 1 を構成で
  保証する。索引の行範囲は直前の Doc コメントを含まないことがある）。解析できないファイル（言語が未対応など）は索引の行範囲。
* ``doc_full`` / ``decorators`` は索引にも ``SymbolRecord`` にも無いので、構文木から取る（Python は本体先頭の docstring、
  TypeScript / JavaScript は直前の ``/** */``）。
* ``type_definitions`` はシグネチャ（と戻り値の型）の識別子のうち、同じファイルの型宣言か、import をたどった先の型宣言に解決できる
  ものを、出現順に 10 件まで。索引から引くので、その URN は ``resolve_symbols`` に渡して必ず解決できる（事後条件 2）。
* 項目単位の失敗（``E_TARGET_NOT_FOUND`` / ``E_TARGET_AMBIGUOUS``。曖昧なら候補 10 件）。全件失敗のときだけ全体のエラー。

サービスの要求は T-03 と同じ（``reader_pool`` または ``db_path``）。LSP 経路は ``lsp_pool`` と書き込み手（``runtime(ws).writer`` か
``writer``）を duck typing で求める（無ければ ``structural`` のまま。詳細は ``_t04_exact``）。
"""

from __future__ import annotations

import ast
import inspect
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import BATCH_MAX_ITEMS, CANDIDATES_MAX
from lucianke.core.model import Confidence, EngineChoice, FileInfo, Kind, RelPath, SymbolRecord, Tier
from lucianke.core.records import Record, ToolOutput
from lucianke.core.urn import CodeSel, Urn, parse
from lucianke.engine import selector
from lucianke.index import queries
from lucianke.syntax.imports import ImportFact, extract_imports, imported_name_map, make_resolver
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._rev import reject_unsupported_revision
from lucianke.tools._services import FileAnalysis, ToolServices, analyze_file, services_of
from lucianke.tools._t04_exact import ExactLifter
from lucianke.tools.t02_get_file_skeleton import failure_record
from lucianke.tools.t03_search_workspace_symbols import index_reader
from lucianke.tools.t05_get_symbol_bodies import TargetRefArg, body_lines
from lucianke.workspace.paths import confine
from lucianke.workspace.types import Workspace

MAX_HOPS = 5
"""import / 再エクスポートをたどる段数の上限（循環の保険）。"""
TYPE_DEFS_MAX = 10
"""``type_definitions`` の 1 対象あたりの上限（仕様書 T-04 入力表）。"""

_SELECTOR_PREFIXES = ("xpath:", "jsonpath:", "jq:", "toml:", "yaml:", "cell:")
_TYPE_KINDS = frozenset({Kind.CLASS, Kind.INTERFACE, Kind.STRUCT, Kind.ENUM, Kind.TRAIT, Kind.TYPE})
_DOC_KINDS = frozenset(
    {Kind.CLASS, Kind.INTERFACE, Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR, Kind.PROPERTY}
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PY_DEFS = frozenset({"function_definition", "class_definition"})
_JS_CONTAINERS = frozenset(
    {
        "program",
        "class_body",
        "interface_body",
        "statement_block",
        "object_type",
        "enum_body",
        "internal_module",
    }
)
_JS_CLASS_NODES = frozenset({"class_declaration", "abstract_class_declaration", "class"})


class ResolveSymbolsArgs(CommonArgs):
    targets: list[TargetRefArg] = Field(
        min_length=1, max_length=BATCH_MAX_ITEMS, description="1-20 TargetRefs."
    )
    resolve_type_definitions: bool = Field(
        default=True,
        description="Resolve user-defined types in the signature to URNs (one level, up to 10 per target).",
    )
    include_doc_full: bool = Field(
        default=True,
        description="Put the full doc comment in json. compact shows only the first line and doc_lines=N.",
    )
    follow_aliases: bool = Field(
        default=True, description="Follow imports, import aliases and re-exports to the original definition."
    )


# --- 道具 -----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Imports:
    names: dict[str, tuple[RelPath, str]]  # 束ねた名前 -> (定義元ファイル, 元の名前)
    facts: list[ImportFact]


@dataclass(slots=True)
class _Env:
    svc: ToolServices
    ws: Workspace
    conn: sqlite3.Connection
    args: ResolveSymbolsArgs
    analyses: dict[str, FileAnalysis | None] = field(default_factory=dict)
    imports: dict[str, _Imports | None] = field(default_factory=dict)
    files: dict[str, FileInfo | None] = field(default_factory=dict)
    lifter: ExactLifter | None = None
    choices: list[EngineChoice] = field(default_factory=list)  # 項目ごとのエンジン選択（応答の代表を決める）

    @property
    def ws_id(self) -> str:
        return self.ws.workspace_id

    def file_info(self, path: str) -> FileInfo | None:
        if path not in self.files:
            self.files[path] = queries.file_by_path(self.conn, self.ws_id, path)
        return self.files[path]

    def exists(self, rel: RelPath) -> bool:
        return self.file_info(str(rel)) is not None

    async def analysis(self, path: str) -> FileAnalysis | None:
        """構文木で解析したファイル。読めない・未対応の言語は ``None``（索引の情報だけで答える）。"""
        if path not in self.analyses:
            try:
                self.analyses[path] = await analyze_file(self.svc, self.ws, path)
            except LuciankeError:
                self.analyses[path] = None
        return self.analyses[path]

    async def imports_of(self, path: str) -> _Imports | None:
        if path not in self.imports:
            an = await self.analysis(path)
            if an is None:
                self.imports[path] = None
            else:
                facts = extract_imports(an.parsed, language_id=an.language_id)
                resolve = make_resolver(from_rel=an.rel, language_id=an.language_id, exists=self.exists)
                self.imports[path] = _Imports(
                    imported_name_map(facts, resolve, include_reexports=True), facts
                )
        return self.imports[path]

    def suffix(self, path: str, name: str) -> list[SymbolRecord]:
        return queries.symbols_by_qualified_suffix(
            self.conn, self.ws_id, path, name, limit=CANDIDATES_MAX + 1
        )


@dataclass(frozen=True, slots=True)
class _Target:
    """TargetRef の解決結果。コード上のシンボル、モジュール（ファイル）そのもの、ワークスペース外のモジュールのどれか。"""

    sym: SymbolRecord | None = None
    module_path: str | None = None
    external: str | None = None
    name: str = ""
    via: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _candidate(s: SymbolRecord) -> dict[str, Any]:
    return {"urn": str(s.urn), "signature": s.signature, "path": str(s.path), "line": s.range.start.line + 1}


def _ambiguous(name: str, found: list[SymbolRecord]) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_TARGET_AMBIGUOUS,
        f"{name!r} matches {len(found)} symbols; use a longer symbol_name, a urn, or a line",
        candidates=[_candidate(s) for s in found[:CANDIDATES_MAX]],
    )


def _not_found(message: str, **details: Any) -> LuciankeError:
    return LuciankeError(ErrorCode.E_TARGET_NOT_FOUND, message, details=details)


def _pick(name: str, found: list[SymbolRecord]) -> SymbolRecord | None:
    """``qualified_name`` の末尾一致の候補から 1 件を選ぶ。完全一致が 1 件ならそれを優先。0 件は ``None``、複数は曖昧。"""
    exact = [s for s in found if s.qualified_name == name]
    if len(exact) == 1:
        return exact[0]
    found = exact or found
    if not found:
        return None
    if len(found) > 1:
        raise _ambiguous(name, found)
    return found[0]


def _ref_label(ref: TargetRefArg) -> str:
    if ref.urn:
        return ref.urn
    if ref.symbol_name:
        return f"{ref.path}#{ref.symbol_name}"
    return f"{ref.path}:{ref.line}" if ref.line is not None else str(ref.path)


# --- TargetRef の解決（§5.3） ----------------------------------------------------------------


def _external_module(facts: list[ImportFact], name: str) -> str | None:
    """``name`` を束ねている import のモジュール名（解決できなかった import の名前を、ワークスペース外として示す）。"""
    head = name.partition(".")[0]
    for f in facts:
        if f.kind == "import" and not f.names and f.module.split(".")[0] == head:
            return f.module
        for n, alias in f.names:
            if (alias or n) == head and not (n == "*" and alias is None):
                return "." * f.level + f.module
    return None


async def _follow(env: _Env, rel: str, name: str) -> _Target:
    """``rel`` で使える ``name`` が import された名前なら、定義元のシンボルまで import / 再エクスポートをたどる。"""
    cur_path, cur_name = rel, name
    hops: list[str] = []
    seen: set[tuple[str, str]] = set()
    for _ in range(MAX_HOPS):
        if (cur_path, cur_name) in seen:
            break
        seen.add((cur_path, cur_name))
        imp = await env.imports_of(cur_path)
        if imp is None:
            break
        head, _, rest = cur_name.partition(".")
        entry = imp.names.get(head)
        if entry is None:
            module = _external_module(imp.facts, cur_name)
            if module is not None:
                msg = (
                    f"{cur_name!r} is imported from {module!r}, which is outside the workspace: "
                    "definition is null (no structural information)"
                )
                return _Target(external=module, name=cur_name, via=tuple(hops), warnings=(msg,))
            break
        target_file, orig = entry
        hops.append(f"{cur_path}: {head} -> {target_file}")
        if orig == "*":  # モジュールそのもの（import a.b as c / import * as ns / from . import x）
            if not rest:
                return _Target(module_path=str(target_file), via=tuple(hops))
            look = rest
        else:
            look = (head if orig == "default" else orig) + (f".{rest}" if rest else "")
        sym = _pick(look, env.suffix(str(target_file), look))
        if sym is not None:
            return _Target(sym=sym, via=tuple(hops))
        cur_path, cur_name = str(target_file), look  # 再エクスポート（__init__.py / export ... from）をたどる
    raise _not_found(
        f"symbol {name!r} not found in {rel}, and no import of it resolves to a definition in the workspace",
        path=rel,
        symbol_name=name,
    )


async def _by_name(env: _Env, rel: str, name: str) -> _Target:
    sym = _pick(name, env.suffix(rel, name))
    if sym is not None:
        return _Target(sym=sym)
    if not env.args.follow_aliases:
        raise _not_found(
            f"symbol {name!r} not found in {rel} (follow_aliases is off, so imports are not followed)",
            path=rel,
            symbol_name=name,
        )
    return await _follow(env, rel, name)


def _word_at(text: str, col0: int) -> str | None:
    for m in _IDENT.finditer(text):
        if m.start() <= col0 < m.end():
            return m.group(0)
    return None


async def _by_line(env: _Env, rel: str, line: int, character: int | None) -> _Target:
    sym = queries.symbol_at(env.conn, env.ws_id, rel, line - 1, None if character is None else character - 1)
    if sym is not None:
        return _Target(sym=sym)
    imp = await env.imports_of(rel)
    an = await env.analysis(rel)
    if imp is not None and an is not None:  # import 文の上なら、そこで束ねた名前を引く
        for fact in imp.facts:
            first, last = fact.range.line_span_external()
            if not first <= line <= last:
                continue
            bound = [alias or n for n, alias in fact.names if not (n == "*" and alias is None)]
            if fact.kind == "import" and not fact.names:
                bound = [fact.module.split(".")[0]]
            under = _word_at(an.parsed.line_index.line_text(line - 1), character - 1) if character else None
            chosen = under if under in bound else (bound[0] if len(bound) == 1 else None)
            if chosen is None:
                raise _not_found(
                    f"line {line} of {rel} is an import of several names; give character or symbol_name",
                    path=rel,
                    line=line,
                )
            return await _follow(env, rel, chosen)
    raise _not_found(f"no symbol contains line {line} of {rel}", path=rel, line=line)


async def _resolve(env: _Env, ref: TargetRefArg) -> tuple[_Target, list[str]]:
    """``urn`` > ``line`` > ``symbol_name``（§5.3 規則 4）。無視した形は warnings に記録する。"""
    ignored: list[str] = []
    if ref.urn is not None:
        ignored = [
            f
            for f, v in (("path", ref.path), ("line", ref.line), ("symbol_name", ref.symbol_name))
            if v is not None
        ]
        try:
            urn = parse(ref.urn)
        except ValueError as exc:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT, f"targets.urn: {exc}", details={"param": "targets.urn"}
            ) from None
        warn = [f"{', '.join(ignored)} ignored: urn takes precedence"] if ignored else []
        if not isinstance(urn.selector, CodeSel):
            raise _not_found(f"urn does not name a code symbol: {ref.urn}", urn=ref.urn)
        sym = queries.symbol_by_urn(env.conn, urn)
        if urn.is_external:
            if sym is None:
                raise _not_found(f"no symbol with urn {ref.urn}", urn=ref.urn)
            pkg = urn.external_parts()[0]
            msg = f"{ref.urn} belongs to the external package {pkg!r}: definition is null (outside the workspace)"
            return _Target(external=pkg, name=sym.qualified_name, warnings=(msg,)), warn
        if urn.workspace_id != env.ws_id:
            raise _not_found(f"urn does not belong to workspace {env.ws_id!r}: {ref.urn}", urn=ref.urn)
        if sym is not None:
            return _Target(sym=sym), warn
        qualified = urn.selector.qualified  # arity / sighash の違いは緩めて探す
        found = [s for s in env.suffix(urn.path, qualified) if s.qualified_name == qualified]
        if len(found) == 1:
            return _Target(sym=found[0]), warn
        if len(found) > 1:
            raise _ambiguous(ref.urn, found)
        raise _not_found(f"no symbol with urn {ref.urn}", urn=ref.urn)
    if ref.path is None:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            "targets: give urn, or path with symbol_name or line",
            details={"param": "targets"},
        )
    rel = str(confine(env.ws, ref.path, param="targets"))
    if env.file_info(rel) is None:
        raise _not_found(f"file is not in the index: {rel}", path=rel, param="targets")
    if ref.line is not None:
        warn = ["symbol_name ignored: line takes precedence"] if ref.symbol_name is not None else []
        return await _by_line(env, rel, ref.line, ref.character), warn
    if ref.symbol_name is not None:
        if ref.symbol_name.startswith(_SELECTOR_PREFIXES):
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"selector {ref.symbol_name.split(':', 1)[0]}: is not supported in this build (code symbols only)",
                details={"reason": "selector_not_supported"},
            )
        return await _by_name(env, rel, ref.symbol_name), []
    raise LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT,
        "targets: path needs symbol_name or line (or use urn)",
        details={"param": "targets"},
    )


# --- Doc 全文・デコレータ（構文木） ----------------------------------------------------------


def _text(an: FileAnalysis, node: Any) -> str:
    return an.data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _py_docstring(an: FileAnalysis, def_node: Any) -> str | None:
    body = def_node.child_by_field_name("body")
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type != "expression_statement" or not first.named_children:
        return None
    lit = first.named_children[0]
    if lit.type not in ("string", "concatenated_string"):
        return None
    raw = _text(an, lit)
    try:
        value = ast.literal_eval(raw)
        if isinstance(value, str):
            return inspect.cleandoc(value) or None
    except (ValueError, SyntaxError):
        pass
    return raw.strip("\"' \r\n") or None


def _clean_jsdoc(raw: str) -> str | None:
    body = raw[3:]
    if body.endswith("*/"):
        body = body[:-2]
    lines = [re.sub(r"^\s*\*+\s?", "", ln).rstrip() for ln in body.splitlines()]
    return "\n".join(lines).strip() or None


def _js_decorators_inside(an: FileAnalysis, outer: Any) -> list[str]:
    found = [c for c in outer.children if c.type == "decorator"]
    for child in outer.children:
        if child.type in _JS_CLASS_NODES:
            found += [c for c in child.children if c.type == "decorator"]
    return [_one_line(_text(an, d)) for d in found]


def _doc_and_decorators(an: FileAnalysis, sym: SymbolRecord) -> tuple[str | None, list[str]]:
    """``(Doc 全文, デコレータ)``。取れなければ ``(None, [])``。位置は ``point[0]`` で読む（属性読みはしない。BUG-0003）。"""
    if sym.kind not in _DOC_KINDS:
        return None, []
    pos = sym.selection.start if sym.selection is not None else sym.range.start
    offset = an.parsed.line_index.position_to_offset(pos).value
    name_node = an.parsed.tree.root_node.descendant_for_byte_range(offset, offset + 1)
    if name_node is None:
        return None, []
    if an.language_id == "python":
        node = name_node
        while node is not None and node.type not in _PY_DEFS:
            node = node.parent
        field_name = None if node is None else node.child_by_field_name("name")
        if node is None or field_name is None or field_name.start_byte != name_node.start_byte:
            return None, []
        parent = node.parent
        decorators: list[str] = []
        if parent is not None and parent.type == "decorated_definition":
            decorators = [_one_line(_text(an, c)) for c in parent.children if c.type == "decorator"]
        return _py_docstring(an, node), decorators
    outer = name_node
    while outer.parent is not None and outer.parent.type not in _JS_CONTAINERS:
        outer = outer.parent
    decorators = _js_decorators_inside(an, outer)
    anchor_row = outer.start_point[0]
    prev = outer.prev_sibling
    leading: list[str] = []
    while prev is not None and prev.type == "decorator":
        leading.insert(0, _one_line(_text(an, prev)))
        anchor_row = prev.start_point[0]
        prev = prev.prev_sibling
    decorators = leading + decorators
    doc: str | None = None
    if prev is not None and prev.type == "comment" and anchor_row - prev.end_point[0] <= 1:
        raw = _text(an, prev)
        if raw.startswith("/**"):
            doc = _clean_jsdoc(raw)
    return doc, decorators


# --- 型の定義 -------------------------------------------------------------------------------


def _span(s: SymbolRecord) -> dict[str, int]:
    start, end = s.range.line_span_external()
    return {"start_line": start, "end_line": end}


async def _type_definitions(env: _Env, sym: SymbolRecord) -> list[dict[str, Any]]:
    text = " ".join(t for t in (sym.signature, sym.return_type) if t)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in dict.fromkeys(_IDENT.findall(text)):
        if len(out) >= TYPE_DEFS_MAX:
            break
        if name in ("self", "cls"):
            continue
        local = [s for s in env.suffix(str(sym.path), name) if s.kind in _TYPE_KINDS]
        found: SymbolRecord | None = None
        try:
            found = _pick(name, local)
            if found is None:
                target = await _follow(env, str(sym.path), name)
                if target.sym is not None and target.sym.kind in _TYPE_KINDS:
                    found = target.sym
        except LuciankeError:
            continue  # 曖昧・見つからない・ワークスペース外の型は載せない
        if found is not None and str(found.urn) not in seen:
            seen.add(str(found.urn))
            out.append({"name": name, "urn": str(found.urn), "path": str(found.path), "range": _span(found)})
    return out


# --- レコード -------------------------------------------------------------------------------


async def _symbol_record(
    env: _Env, target: _Target, request: dict[str, Any], priority: int, warnings: list[str]
) -> tuple[Record, FileInfo | None]:
    L = env.svc.lines
    db_sym = target.sym
    assert db_sym is not None
    path = str(db_sym.path)
    an = await env.analysis(path)
    fresh = None
    if an is not None:
        fresh = next((s for s in an.symbols if str(s.urn) == str(db_sym.urn)), None)
    sym = fresh or db_sym
    if fresh is not None and an is not None:
        first, last = body_lines(
            an, fresh
        )  # T-05 と同じ関数。definition.range == range_returned（context_lines=0）
    else:
        first, last = sym.range.line_span_external()
    doc_full: str | None = None
    decorators: list[str] = []
    if fresh is not None and an is not None:
        doc_full, decorators = _doc_and_decorators(an, fresh)
    if doc_full is None:
        doc_full = sym.doc_summary
    type_defs = await _type_definitions(env, sym) if env.args.resolve_type_definitions else []

    info = env.file_info(path)
    sha = an.sha256 if an is not None else (info.sha256 if info is not None else None)
    lift = None
    if env.lifter is not None and info is not None and sha is not None:
        lift = await env.lifter.lift(sym, info, sha)  # exact に上げる（cache → LSP。降格は lift の中）
        env.choices.append(lift.choice)
    exact = lift is not None and lift.exact
    confidence = Confidence.EXACT if exact else sym.confidence
    if lift is not None and lift.warnings:
        warnings = [*warnings, *lift.warnings]

    symbol_json = sym.to_json()
    item: dict[str, Any] = {
        "ok": True,
        "request": request,
        "symbol": symbol_json,
        "definition": {
            "path": path,
            "range": {"start_line": first, "end_line": last},
            "selection_range": symbol_json.get("selection_range"),
            "declared_in": sym.declared_in,
        },
        "signature_expanded": lift.text if lift is not None and lift.text is not None else sym.signature,
        "decorators": decorators,
        "type_definitions": type_defs,
        "confidence_level": confidence.value,
    }
    if env.args.include_doc_full:
        item["doc_full"] = doc_full
    if target.via:
        item["resolved_via"] = list(target.via)
    if warnings:
        item["warnings"] = warnings

    lines = [
        L.symbol_line(sym.kind, f"{path}#{sym.qualified_name}", start_line=first, end_line=last),
        L.attr_line("urn", str(sym.urn), indent=0),
    ]
    if sym.signature:
        lines.append(L.attr_line("sig", sym.signature, indent=0))
    if type_defs:
        lines.append(L.attr_line("type", " ".join(f"{d['name']}={d['urn']}" for d in type_defs), indent=0))
    if doc_full:
        doc_lines = doc_full.splitlines() or [doc_full]
        first_line = next((ln.strip() for ln in doc_lines if ln.strip()), "")
        suffix = f" (doc_lines={len(doc_lines)})" if fresh is not None and an is not None else ""
        lines.append(L.attr_line("doc", f"{first_line}{suffix}", indent=0))
    if decorators:
        lines.append(L.attr_line("decorators", ", ".join(decorators), indent=0))
    if confidence < Confidence.STRUCTURAL:
        lines.append(L.attr_line("conf", confidence.value, indent=0))
    record = Record(
        kind="symbol",
        key=str(sym.urn),
        priority=priority,
        lines=tuple(lines),
        data=item,
        confidence=confidence,
        group=path,
    )
    return record, env.file_info(path)


def _module_record(
    env: _Env, target: _Target, request: dict[str, Any], priority: int
) -> tuple[Record, FileInfo | None]:
    L = env.svc.lines
    path = target.module_path or ""
    info = env.file_info(path)
    last = max(info.line_count if info is not None else 1, 1)
    urn = str(Urn(env.ws_id, path))
    item = {
        "ok": True,
        "request": request,
        "symbol": None,
        "definition": {
            "path": path,
            "range": {"start_line": 1, "end_line": last},
            "selection_range": None,
            "declared_in": "source",
        },
        "signature_expanded": None,
        "decorators": [],
        "type_definitions": [],
        "resolved_via": list(target.via),
        "confidence_level": Confidence.STRUCTURAL.value,
    }
    lines = (L.symbol_line(Kind.MODULE, path, start_line=1, end_line=last), L.attr_line("urn", urn, indent=0))
    record = Record(
        kind="symbol", key=urn, priority=priority, lines=lines, data=item,
        confidence=Confidence.STRUCTURAL, group=path,
    )  # fmt: skip
    return record, info


def _external_record(
    env: _Env, target: _Target, label: str, request: dict[str, Any], priority: int
) -> Record:
    item = {
        "ok": True,
        "request": request,
        "symbol": None,
        "definition": None,
        "signature_expanded": None,
        "decorators": [],
        "type_definitions": [],
        "warnings": list(target.warnings),
        "confidence_level": Confidence.STRUCTURAL.value,
    }
    lines = tuple(env.svc.lines.banner(f"{label}: {target.warnings[0]}"))
    return Record(
        kind="note", key=f"external:{label}", priority=priority, lines=lines, data=item,
        confidence=Confidence.STRUCTURAL, group=label,
    )  # fmt: skip


def _failure(env: _Env, label: str, err: LuciankeError, request: dict[str, Any], priority: int) -> Record:
    rec = failure_record(label, err, priority, env.svc.lines.banner)
    data = {"ok": False, "request": request, "error": err.to_json()}
    lines = (
        *rec.lines,
        *(env.svc.lines.candidate_line(c) for c in err.candidates),
    )  # 曖昧のときは候補を 1 行 1 件で添える
    return Record(
        kind=rec.kind, key=rec.key, priority=rec.priority, group=rec.group,
        confidence=rec.confidence, lines=lines, data=data,
    )  # fmt: skip


# --- ハンドラ -------------------------------------------------------------------------------


def _response_engine(choices: list[EngineChoice]) -> EngineChoice:
    """応答の代表のエンジン選択: 最も低い水準の項目（``accuracy_warning`` に本当の理由を載せる）。同じ水準なら LSP の答えを先に。

    シンボルの項目が無い（モジュール・外部パッケージ・失敗だけ）ときは、構文木と import 表で答えた ``structural``。
    """
    if not choices:
        return EngineChoice(engine="tree-sitter+imports", confidence=Confidence.STRUCTURAL)
    return min(choices, key=lambda c: (c.confidence, c.engine == selector.ENGINE_LSP_CACHE))


async def handler(ctx: ToolContext, args: ResolveSymbolsArgs) -> ToolOutput:
    await reject_unsupported_revision(ctx, args.revision)
    svc = services_of(ctx.services)
    ws = svc.workspace(args.workspace_id)
    records: list[Record] = []
    errors: list[LuciankeError] = []
    languages: list[str] = []
    tier = Tier.T1
    with index_reader(svc) as conn:
        env = _Env(svc=svc, ws=ws, conn=conn, args=args, lifter=ExactLifter(svc, ws, conn))
        for i, ref in enumerate(args.targets):
            request = ref.model_dump(exclude_none=True)
            label = _ref_label(ref)
            try:
                target, warnings = await _resolve(env, ref)
                info: FileInfo | None = None
                if target.sym is not None:
                    rec, info = await _symbol_record(env, target, request, i, warnings)
                elif target.module_path is not None:
                    rec, info = _module_record(env, target, request, i)
                else:
                    rec = _external_record(env, target, label, request, i)
                records.append(rec)
                if info is not None:
                    tier = max(tier, info.tier)
                    if info.language not in languages:
                        languages.append(info.language)
            except LuciankeError as err:
                errors.append(err)
                records.append(_failure(env, label, err, request, i))
    if errors and len(errors) == len(args.targets):
        first = errors[0]
        raise LuciankeError(
            first.code,
            first.message,
            details={
                **first.details,
                "items": [
                    {"ok": False, "request": r.model_dump(exclude_none=True), "error": e.to_json()}
                    for r, e in zip(args.targets, errors, strict=True)
                ],
            },
            candidates=first.candidates,
        )
    return ToolOutput(
        records=records,
        max_confidence=Confidence.EXACT,
        engine=_response_engine(env.choices),
        page_unit="target",
        tier=tier,
        detected_language=",".join(languages) if languages else "n/a",
    )


TOOL_SPEC = ToolSpec(
    name="resolve_symbols",
    tid="T-04",
    args_model=ResolveSymbolsArgs,
    handler=handler,
    default_max_tokens=2000,
    description=(
        "Resolve 1-20 TargetRefs (urn, or path + symbol_name, or path + line) to their definition: path and line range, "
        "signature, doc comment, decorators, and URNs of the user-defined types in the signature. Follows imports, "
        "aliases and re-exports to the original definition inside the workspace; a name imported from an external "
        "package returns definition null with a warning. Python/TypeScript/JavaScript. When the language server is "
        "ready (or a previous exact answer is cached) the item is exact and signature_expanded is the hover type; "
        "otherwise it falls back to the syntax tree and index (structural) without waiting for the server. "
        "A failed or ambiguous item is reported on its own (ambiguous items list up to 10 candidates)."
    ),
    max_confidence=Confidence.EXACT,
    milestone="M1",
    page_unit="target",
    records_key="items",
)
SPEC = TOOL_SPEC  # tools/__init__.register_tools の命名（SPEC）に合わせる別名
