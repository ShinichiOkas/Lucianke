"""ファイル骨格のレコード（設計書 §7.4.4、仕様書 §11 T-02 の処理規約 2・8・9、§6.1.1）。

``build_file_record`` は 1 ファイル = 1 レコード（``kind="file-block"``、``group=rel``）を作る。

* ``lines``: ``=== path (N lines, skeleton: M lines, sha=…) ===`` → ``imports: […]`` → 入れ子の ``symbol-block``
  （``doc:`` ・``urn:`` ・``decorators:`` ・``raises:`` の属性行つき）。本体は ``{ ...N lines omitted }`` に畳む。
* ``data``: T-02 の json の ``items[]`` の 1 項目（``symbols[]`` は SymbolRecord の json に ``children`` ・``fold_lines`` ・
  ``decorators`` ・``raises`` を足したもの）。
* ``shrink``: 予算に入らないときの縮退版（処理規約 8: ``doc:`` → ``urn:`` → 非公開メンバー → 入れ子メンバー〈最上位は残す〉
  の順に落とし、``- truncated: N members omitted`` を足す）。入らなければ ``None``。

行の組み立て関数（``file_block_header`` ・``import_line`` ・``symbol_line`` ・``attr_line`` ・``note``）は
``mcp/render/compact.py`` の同名の関数と **同じ文法・同じ署名** で、ここに複製してある。``syntax`` は ``mcp`` を
import できない（``tests/unit/test_import_graph.py``。設計書 §4.2）ので、呼び出せないため。複製が離れないよう、
``tests/unit/test_syntax_skeleton.py`` が両者の出力を突き合わせる。共通化するなら行の組み立てを ``core`` へ移す。

位置: SymbolRecord は内部表現（0 始まり）。外へ出す行は ``Range.line_span_external()``（1 始まり・両端を含む）。
本体の畳み方: 畳む行数 = 範囲の行数 − デコレータ行 − シグネチャ行（定義の見出し〜本体の直前）− 出した子シンボルの範囲の行数。
``{ ...N lines omitted }`` を付けるのは関数・メソッド・コンストラクタだけ（クラスなどの入れ物は付けない。§6.1.1 の例に倣う）。
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from lucianke.core.model import Confidence, Kind, SymbolRecord
from lucianke.core.records import Record
from lucianke.syntax.support import SUPPORT
from lucianke.syntax.types import ParsedFile, SkeletonOptions

__all__ = [
    "attr_line",
    "build_file_record",
    "default_estimate",
    "extract_imports",
    "file_block_header",
    "import_line",
    "note",
    "symbol_line",
]

LINE_LIMIT = 200  # 仕様書 §6.1 規約 6（doc）
MODIFIERS: tuple[str, ...] = ("pub", "priv", "prot", "static", "async", "abstract", "export")
_KINDS: frozenset[str] = frozenset(k.value for k in Kind)
_CALLABLE_KINDS = frozenset({Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR})
_SHA_RE = re.compile(r"[0-9a-fA-F]{12,}")


# --- compact.py と同じ行の組み立て関数（複製。モジュール docstring を参照） ------------------------------


def _one_line(text: object) -> str:
    parts = [p.strip() for p in str(text).splitlines()]
    return " ".join(p for p in parts if p)


def _ind(level: int) -> str:
    if level < 0:
        raise ValueError(f"indent must be >= 0: {level}")
    return "  " * level


def _fit(prefix: str, text: str, suffix: str = "") -> str:
    if len(prefix) + len(text) + len(suffix) <= LINE_LIMIT:
        return text
    keep = max(LINE_LIMIT - len(prefix) - len(suffix) - 1, 0)
    return text[:keep].rstrip() + "…"


def file_block_header(
    path: str,
    *,
    lines: int,
    cells: int | None = None,
    skeleton_lines: int | None = None,
    sha12: str | None = None,
) -> str:
    """``=== <path> (<N> lines[, <M> cells][, skeleton: <K> lines][, sha=<12hex>]) ===``。"""
    inner = [f"{lines} lines"]
    if cells is not None:
        inner.append(f"{cells} cells")
    if skeleton_lines is not None:
        inner.append(f"skeleton: {skeleton_lines} lines")
    if sha12 is not None:
        if not _SHA_RE.fullmatch(sha12):
            raise ValueError(f"sha12 は 16 進 12 桁以上: {sha12!r}")
        inner.append(f"sha={sha12[:12].lower()}")
    return f"=== {_one_line(path)} ({', '.join(inner)}) ==="


def import_line(items: Iterable[str]) -> str:
    """``imports: [a, b, c]``。"""
    return f"imports: [{', '.join(_one_line(i) for i in items)}]"


def symbol_line(
    kind: Kind | str,
    name: str,
    *,
    indent: int = 0,
    modifiers: Iterable[str] = (),
    sig: str | None = None,
    ret: str | None = None,
    start_line: int,
    end_line: int | None = None,
    fold_lines: int | None = None,
) -> str:
    """``[Kind] [mods ]name[(sig)[ -> ret]] @L<a>[-<b>][ { ...N lines omitted }]``（行は 1 始まり）。"""
    kind_text = str(kind.value if isinstance(kind, Kind) else kind)
    if kind_text not in _KINDS:
        raise ValueError(f"kind は {sorted(_KINDS)} のいずれか: {kind_text!r}")
    mods = list(modifiers)
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(f"modifier は {MODIFIERS} のいずれか: {m!r}")
    if start_line < 1:
        raise ValueError(f"start_line は 1 始まり（>= 1）: {start_line}")
    if end_line is not None and end_line < start_line:
        raise ValueError(f"end_line < start_line: {end_line} < {start_line}")
    head = f"{_ind(indent)}[{kind_text}] " + "".join(f"{m} " for m in mods) + _one_line(name)
    if sig is not None:
        inline = _one_line(sig)
        if not inline.startswith("("):
            inline = f"({inline})"
        if ret and " -> " not in inline:
            inline += f" -> {_one_line(ret)}"
        head += inline
    elif ret:
        raise ValueError("ret には sig が必要（sig-inline = '(' text ')' [' -> ' text]）")
    rng = f"{start_line}" if end_line is None or end_line == start_line else f"{start_line}-{end_line}"
    line = f"{head} @L{rng}"
    if fold_lines is not None:
        line += f" {{ ...{fold_lines} lines omitted }}"
    return line


def attr_line(key: str, value: object, *, indent: int) -> str:
    """``<key>: <value>``。``indent`` は属性を持つ ``symbol_line`` の段数（属性行はその 1 段内側）。"""
    prefix = f"{_ind(indent + 1)}{key}: "
    text = _one_line(value)
    if key in ("doc", "when"):
        text = _fit(prefix, text)
    return prefix + text


def note(text: object) -> str:
    """``- <text>``。"""
    return f"- {_one_line(text)}"


# --- トークンの見積もり（mcp.budget.TokenEstimator と同じ係数。syntax から mcp は import できないので近似を持つ） ----


def default_estimate(text: str) -> int:
    """仕様書 §5.7 の係数（ASCII 英数字・空白 1/4、ASCII 記号 1/2、全角 1/1.5、その他 1/2）の合計を切り上げる。

    ``shrink`` の判定用の既定。パッカーの見積もり（``mcp.budget.TokenEstimator``）と 1〜2% ずれうるので、
    ツール側は ``build_file_record(..., estimate=TokenEstimator().count)`` で差し替えるのが確実。
    """
    units = 0  # 12 分の 1 トークン単位（1/4 = 3、1/2 = 6、1/1.5 = 8）
    for ch in text:
        if ch.isascii():
            units += 3 if (ch.isalnum() or ch.isspace()) else 6
        elif unicodedata.east_asian_width(ch) in "WF":
            units += 8
        else:
            units += 6
    return -(-units // 12)


# --- 構文木から取るもの（import ・デコレータ・raise ・シグネチャ行数） ------------------------------------


def _text(parsed: ParsedFile, node: Any) -> str:
    return parsed.source[node.start_byte : node.end_byte].decode("utf-8", "replace")


_PY_CONTAINERS = frozenset(
    {
        "if_statement",
        "elif_clause",
        "else_clause",
        "try_statement",
        "except_clause",
        "except_group_clause",
        "finally_clause",
        "with_statement",
        "block",
        "for_statement",
        "while_statement",
        "match_statement",
        "case_clause",
    }
)


def _py_imports(parsed: ParsedFile) -> list[str]:
    items: list[str] = []

    def name_of(node: Any) -> str:
        if node.type == "aliased_import":
            base = node.child_by_field_name("name")
            alias = node.child_by_field_name("alias")
            if base is not None and alias is not None:
                return f"{_text(parsed, base)} as {_text(parsed, alias)}"
        return _text(parsed, node)

    def visit(parent: Any) -> None:
        for c in parent.named_children:
            if c.type == "import_statement":
                items.extend(name_of(n) for n in c.children_by_field_name("name"))
            elif c.type == "import_from_statement":
                module = c.child_by_field_name("module_name")
                where = _text(parsed, module) if module is not None else "?"
                names = [name_of(n) for n in c.children_by_field_name("name")]
                if any(ch.type == "wildcard_import" for ch in c.children):
                    names.append("*")
                items.extend(f"{n} from {where}" for n in names)
            elif c.type in _PY_CONTAINERS:
                visit(c)

    visit(parsed.tree.root_node)
    return items


def _js_imports(parsed: ParsedFile) -> list[str]:
    items: list[str] = []
    for c in parsed.tree.root_node.named_children:
        if c.type != "import_statement":
            continue
        source = c.child_by_field_name("source")
        if source is None:
            continue
        where = _text(parsed, source).strip("\"'`")
        clause = next((ch for ch in c.named_children if ch.type == "import_clause"), None)
        if clause is None:
            items.append(where)  # import "side-effect";
            continue
        for part in clause.named_children:
            if part.type == "identifier":  # 既定 import
                items.append(f"{_text(parsed, part)} from {where}")
            elif part.type == "namespace_import":
                ident = next((n for n in part.named_children if n.type == "identifier"), None)
                local = _text(parsed, ident) if ident is not None else "?"
                items.append(f"* as {local} from {where}")
            elif part.type == "named_imports":
                for spec in part.named_children:
                    if spec.type != "import_specifier":
                        continue
                    base = spec.child_by_field_name("name")
                    alias = spec.child_by_field_name("alias")
                    if base is None:
                        continue
                    text = _text(parsed, base)
                    if alias is not None:
                        text += f" as {_text(parsed, alias)}"
                    items.append(f"{text} from {where}")
    return items


def extract_imports(parsed: ParsedFile) -> list[str]:
    """``import`` の要約: ``import x`` → ``x``、``from m import a, b`` → ``a from m``, ``b from m``、
    ``import X, { A as B } from "m"`` → ``X from m``, ``A as B from m``。重複は除く。対象外の言語は空。"""
    if parsed.language_id == "python":
        raw = _py_imports(parsed)
    elif parsed.language_id in ("typescript", "tsx", "javascript", "jsx"):
        raw = _js_imports(parsed)
    else:
        return []
    return list(dict.fromkeys(raw))


@dataclass(slots=True)
class _Info:
    """シンボルの定義ノードから読んだもの。"""

    decorators: list[str] = field(default_factory=list)
    decorator_lines: int = 0  # 範囲の中にあるデコレータの行数
    raises: list[str] = field(default_factory=list)
    header_lines: int = 1  # シグネチャ行（定義の見出し行〜本体の直前）


_NESTED_DEFS = frozenset(
    {
        "function_definition",
        "class_definition",
        "lambda",
        "function_declaration",
        "function_expression",
        "function",
        "generator_function",
        "generator_function_declaration",
        "arrow_function",
        "method_definition",
        "class_declaration",
        "class",
    }
)
_RAISE_NODES = frozenset({"raise_statement", "throw_statement"})
_JS_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})


def _definition_node(parsed: ParsedFile, sym: SymbolRecord) -> tuple[Any, Any] | None:
    """``(定義ノード, 名前ノード)``。名前の位置から外へ辿り、``name`` フィールドがその位置を含む最初のノード。"""
    pos = sym.selection.start if sym.selection is not None else sym.range.start
    off = parsed.line_index.position_to_offset(pos).value
    root = parsed.tree.root_node
    node = root.descendant_for_byte_range(off, min(off + 1, len(parsed.source)))
    while node is not None:
        name = node.child_by_field_name("name")
        if name is not None and name.start_byte <= off < name.end_byte:
            return node, name
        node = node.parent
    return None


def _thrown_name(parsed: ParsedFile, stmt: Any) -> str | None:
    expr = next(iter(stmt.named_children), None)
    if expr is None:
        return None
    if expr.type in ("call", "call_expression"):
        expr = expr.child_by_field_name("function") or expr
    elif expr.type == "new_expression":
        expr = expr.child_by_field_name("constructor") or expr
    if expr.type not in ("identifier", "attribute", "member_expression", "type_identifier"):
        return None
    name = _text(parsed, expr)
    last = re.split(r"[.]", name)[-1]
    return name if last[:1].isupper() else None  # 変数の再 raise（``raise exc``）は拾わない


def _collect_raises(parsed: ParsedFile, body: Any) -> list[str]:
    found: list[str] = []
    stack = list(reversed(body.named_children))
    while stack:
        node = stack.pop()
        if node.type in _NESTED_DEFS:
            continue  # 入れ子の定義の raise は、その定義のもの
        if node.type in _RAISE_NODES:
            name = _thrown_name(parsed, node)
            if name:
                found.append(name)
            continue
        stack.extend(reversed(node.named_children))
    return list(dict.fromkeys(found))


def _read_info(parsed: ParsedFile, sym: SymbolRecord, span: tuple[int, int]) -> _Info:
    info = _Info()
    found = _definition_node(parsed, sym)
    if found is None:
        return info
    node, name = found
    decorator_nodes: list[Any] = []
    parent = node.parent
    if parent is not None and parent.type == "decorated_definition":
        decorator_nodes = [c for c in parent.children if c.type == "decorator"]
    else:
        decorator_nodes = [c for c in node.children if c.type == "decorator"]
        if parent is not None and parent.type == "export_statement":
            decorator_nodes = [c for c in parent.children if c.type == "decorator"] + decorator_nodes
    first, last = span[0], span[1]
    for d in decorator_nodes:
        info.decorators.append(_one_line(_text(parsed, d)))
        a, b = d.start_point.row + 1, d.end_point.row + 1
        info.decorator_lines += max(0, min(b, last) - max(a, first) + 1)
    target = node
    if node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        if value is not None and value.type in _JS_FUNCTION_VALUES:
            target = value
    body = target.child_by_field_name("body")
    name_row = name.start_point.row
    if body is not None:
        if parsed.language_id == "python":
            info.header_lines = max(1, body.start_point.row - name_row)  # block は colon の次の行から
        else:
            info.header_lines = body.start_point.row - name_row + 1  # ``{`` が見出しの最後の行にある
        if sym.kind in _CALLABLE_KINDS:
            info.raises = _collect_raises(parsed, body)
    return info


# --- 入れ子の木 -------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Entry:
    sym: SymbolRecord
    span: tuple[int, int]  # 1 始まり・両端を含む
    private: bool
    info: _Info
    children: list[_Entry] = field(default_factory=list)
    hidden: bool = False  # include_private=False で最初から含めない（子も含めない）

    @property
    def lines(self) -> int:
        return self.span[1] - self.span[0] + 1


def _is_private(sym: SymbolRecord, *, top_level: bool, language_id: str) -> bool:
    mods = sym.modifiers
    if "priv" in mods:
        return True
    if "pub" in mods or "prot" in mods or "export" in mods:
        return False
    if language_id == "python":
        return sym.name.startswith("_") and not (sym.name.startswith("__") and sym.name.endswith("__"))
    return sym.name.startswith("#") or top_level  # TS / JS: 非 export の最上位は非公開


def _build_tree(
    parsed: ParsedFile, symbols: Sequence[SymbolRecord], *, include_private: bool
) -> list[_Entry]:
    spans = [s.range.line_span_external() for s in symbols]
    order = sorted(range(len(symbols)), key=lambda i: (spans[i][0], -spans[i][1], i))
    roots: list[_Entry] = []
    stack: list[_Entry] = []
    for i in order:
        sym, span = symbols[i], spans[i]
        # 親 = 範囲をちょうど含む最も内側のシンボル。同じ範囲は兄弟（親子にしない）
        while stack and not (
            stack[-1].span[0] <= span[0] and span[1] <= stack[-1].span[1] and stack[-1].span != span
        ):
            stack.pop()
        parent = stack[-1] if stack else None
        private = _is_private(sym, top_level=parent is None, language_id=parsed.language_id)
        hidden = (parent is not None and parent.hidden) or (not include_private and private)
        entry = _Entry(
            sym=sym,
            span=span,
            private=private,
            info=_Info() if hidden else _read_info(parsed, sym, span),
            hidden=hidden,
        )
        if not hidden:
            (parent.children if parent is not None else roots).append(entry)
        stack.append(entry)
    return roots


def _count(entries: Sequence[_Entry]) -> int:
    return sum(1 + _count(e.children) for e in entries)


# --- 描画（詳細度ごと） -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Detail:
    doc: bool
    urn: bool
    private: bool
    nested: bool


def _urn_wanted(opts: SkeletonOptions, entry: _Entry) -> bool:
    if opts.include_urns == "all":
        return True
    return opts.include_urns == "public" and not entry.private


def _emit(
    entries: Sequence[_Entry],
    depth: int,
    opts: SkeletonOptions,
    d: _Detail,
    out: list[str],
) -> tuple[list[dict[str, Any]], list[_Entry], int]:
    """``(json の symbols, 出した直下の Entry, 出した総数)``。"""
    jsons: list[dict[str, Any]] = []
    emitted: list[_Entry] = []
    total = 0
    for e in entries:
        if not d.private and e.private:
            continue
        sym = e.sym
        slot = len(out)
        out.append("")  # 見出し行は子を出した後（畳む行数が決まってから）で埋める
        doc = sym.doc_summary if d.doc and opts.include_doc_summary else None
        if doc:
            out.append(attr_line("doc", doc, indent=depth))
        if d.urn and _urn_wanted(opts, e):
            out.append(attr_line("urn", str(sym.urn), indent=depth))
        if e.info.decorators:
            out.append(attr_line("decorators", ", ".join(e.info.decorators), indent=depth))
        if e.info.raises:
            out.append(attr_line("raises", ", ".join(e.info.raises), indent=depth))
        child_json: list[dict[str, Any]] = []
        child_entries: list[_Entry] = []
        child_total = 0
        if d.nested:
            child_json, child_entries, child_total = _emit(e.children, depth + 1, opts, d, out)
        fold = 0
        if sym.kind in _CALLABLE_KINDS:
            fold = (
                e.lines - e.info.decorator_lines - e.info.header_lines - sum(c.lines for c in child_entries)
            )
            fold = max(fold, 0)
        out[slot] = symbol_line(
            sym.kind,
            sym.qualified_name if depth == 0 else sym.name,
            indent=depth,
            modifiers=[m for m in sym.modifiers if m in MODIFIERS],
            sig=sym.signature,
            start_line=e.span[0],
            end_line=e.span[1],
            fold_lines=fold or None,
        )
        jsons.append(_symbol_json(e, doc, fold, child_json))
        emitted.append(e)
        total += 1 + child_total
    return jsons, emitted, total


def _symbol_json(e: _Entry, doc: str | None, fold: int, children: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        base = e.sym.to_json()
    except ValueError:  # 複数行にまたがる selection は json に出せない
        base = dataclasses.replace(e.sym, selection=None).to_json()
    base["doc_summary"] = doc
    base["children"] = children
    base["fold_lines"] = fold
    base["decorators"] = list(e.info.decorators)
    base["raises"] = list(e.info.raises)
    return base


def build_file_record(
    parsed: ParsedFile,
    symbols: list[SymbolRecord],
    *,
    rel: str,
    opts: SkeletonOptions,
    priority: int = 0,
    estimate: Callable[[str], int] | None = None,
) -> Record:
    """1 ファイルぶんの骨格レコードを作る（設計書 §7.4.4）。

    ``symbols`` は ``symbols.extract`` の結果（順不同でよい）。入れ子は範囲の包含で決める。``opts.include_private`` が
    偽なら非公開シンボル（とその子）は最初から含めない（予算による省略ではないので ``truncated`` には数えない）。
    ``estimate`` は ``shrink`` が使うトークン見積もり（既定は ``default_estimate``。パッカーのものを渡すと確実）。
    """
    est = estimate or default_estimate
    entries = _build_tree(parsed, symbols, include_private=opts.include_private)
    imports = extract_imports(parsed) if opts.include_imports else []
    spec = SUPPORT.get(parsed.language_id)
    tier = int(getattr(spec, "tier", 1))
    line_count = parsed.line_index.content_line_count
    full = _Detail(doc=opts.include_doc_summary, urn=opts.include_urns != "none", private=True, nested=True)
    total_members = _count(entries)

    def render(d: _Detail, shrinkable: bool) -> tuple[Record, tuple[str, ...]]:
        """``(レコード, truncated の注を除いた本文)``。後者は縮退段の重複判定に使う。"""
        body: list[str] = []
        if imports:
            body.append(import_line(imports))
        jsons, _, emitted = _emit(entries, 0, opts, d, body)
        core = tuple(body)
        omitted = total_members - emitted
        if d != full:
            dropped = [
                n for n, was, now in (("doc", full.doc, d.doc), ("urn", full.urn, d.urn)) if was and not now
            ]
            suffix = f" ({', '.join(dropped)} omitted)" if dropped else ""
            body.append(note(f"truncated: {omitted} members omitted{suffix}"))
        header = file_block_header(rel, lines=line_count, skeleton_lines=len(body), sha12=parsed.sha256[:12])
        data = {
            "ok": True,
            "path": rel,
            "language": parsed.language_id,
            "tier": tier,
            "line_count": line_count,
            "skeleton_line_count": len(body),
            "imports": list(imports),
            "symbols": jsons,
            "file_sha256": parsed.sha256,
            "truncated_members": omitted if d != full else 0,
            "confidence_level": Confidence.STRUCTURAL.value,
        }
        rec = Record(
            kind="file-block",
            key=rel,
            priority=priority,
            lines=(header, *body),
            data=data,
            confidence=Confidence.STRUCTURAL,
            group=rel,
            shrink=shrink if shrinkable else None,
        )
        return rec, core

    levels = [
        _Detail(doc=False, urn=full.urn, private=True, nested=True),
        _Detail(doc=False, urn=False, private=True, nested=True),
        _Detail(doc=False, urn=False, private=False, nested=True),
        _Detail(doc=False, urn=False, private=False, nested=False),
    ]

    def shrink(remaining_tokens: int) -> Record | None:
        _, previous = render(full, False)
        for d in levels:
            rec, core = render(d, False)
            if core == previous:
                continue  # 前の段と同じ本文（すでに doc が無い、など）は飛ばす
            previous = core
            if est("\n".join(rec.lines)) <= remaining_tokens:
                return rec
        return None

    return render(full, True)[0]
