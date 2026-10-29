"""T-02 の骨格 1 ファイル分を作る最小の実装（Python・TS・JS。構文木だけ）。

``syntax/skeleton.py``（``build_file_record``）ができたら、``t02_get_file_skeleton`` の呼び先をそちらに差し替える。
差し替えの境目は ``build_file_record(svc, analysis, opts, priority) -> Record`` だけ。

* 入れ子は ``SymbolRecord.range`` の包含で作る（文書順に並んだ列を、スタックで親子にする）。
* 予算に収まらないときの縮退（仕様書 §11 T-02 手順 8）: ``doc:`` → ``urn:`` → 非公開メンバー → 入れ子メンバー（最上位は残す）。
  ``Record.shrink`` が残り予算に収まる最初の段を返す。落としたメンバー数は ``- truncated: N members omitted`` に書く。
* ``decorators:`` / ``raises:`` は未実装。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from lucianke.core.model import Confidence, Kind, SymbolRecord
from lucianke.core.records import Record
from lucianke.tools._services import FileAnalysis, ToolServices, estimate_tokens

IMPORTS_MAX = 40
_FOLDABLE = frozenset({Kind.FUNCTION, Kind.METHOD, Kind.CONSTRUCTOR, Kind.PROPERTY})


@dataclass(frozen=True, slots=True)
class SkeletonOpts:
    include_private: bool = True
    include_doc_summary: bool = True
    include_urns: Literal["public", "all", "none"] = "public"
    include_imports: bool = True


@dataclass(frozen=True, slots=True)
class _Level:
    doc: bool = True
    urn: bool = True
    private: bool = True
    max_depth: int | None = None  # 入れ子の段数の上限（1 = 最上位のみ）


_LEVELS = (
    _Level(),
    _Level(doc=False),
    _Level(doc=False, urn=False),
    _Level(doc=False, urn=False, private=False),
    _Level(doc=False, urn=False, private=False, max_depth=1),
)


@dataclass(slots=True)
class _Node:
    sym: SymbolRecord
    children: list[_Node] = field(default_factory=list)

    def count(self) -> int:
        return 1 + sum(c.count() for c in self.children)


def _nest(symbols: tuple[SymbolRecord, ...]) -> list[_Node]:
    roots: list[_Node] = []
    stack: list[_Node] = []
    for sym in symbols:
        while stack and not stack[-1].sym.range.contains_range(sym.range):
            stack.pop()
        node = _Node(sym)
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
    return roots


def _prune_private(nodes: list[_Node]) -> list[_Node]:
    return [_Node(n.sym, _prune_private(n.children)) for n in nodes if "priv" not in n.sym.modifiers]


# --- import ---------------------------------------------------------------------------------


def _text(an: FileAnalysis, node: Any) -> str:
    return an.data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _py_imports(an: FileAnalysis) -> list[str]:
    out: list[str] = []
    for node in an.parsed.tree.root_node.children:
        if node.type == "import_statement":
            for n in node.children_by_field_name("name"):
                target = n.child_by_field_name("name") if n.type == "aliased_import" else n
                if target is not None:
                    out.append(_text(an, target))
        elif node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            mod = _text(an, module) if module is not None else "?"
            if any(c.type == "wildcard_import" for c in node.children):
                out.append(f"* from {mod}")
                continue
            for n in node.children_by_field_name("name"):
                target = n.child_by_field_name("name") if n.type == "aliased_import" else n
                if target is not None:
                    out.append(f"{_text(an, target)} from {mod}")
    return out


def _js_imports(an: FileAnalysis) -> list[str]:
    out: list[str] = []
    for node in an.parsed.tree.root_node.children:
        if node.type != "import_statement":
            continue
        source = node.child_by_field_name("source")
        src = _text(an, source).strip("'\"`") if source is not None else "?"
        clause = next((c for c in node.children if c.type == "import_clause"), None)
        if clause is None:
            out.append(src)
            continue
        for c in clause.children:
            if c.type == "identifier":
                out.append(f"{_text(an, c)} from {src}")
            elif c.type == "namespace_import":
                ident = next((x for x in c.children if x.type == "identifier"), None)
                out.append(f"* as {_text(an, ident)} from {src}" if ident is not None else f"* from {src}")
            elif c.type == "named_imports":
                for spec in c.children:
                    if spec.type == "import_specifier":
                        name = spec.child_by_field_name("name")
                        if name is not None:
                            out.append(f"{_text(an, name)} from {src}")
    return out


def _imports(an: FileAnalysis) -> list[str]:
    items = _py_imports(an) if an.language_id == "python" else _js_imports(an)
    return list(dict.fromkeys(items))


# --- 描画 -----------------------------------------------------------------------------------


def _emit(
    svc: ToolServices,
    nodes: list[_Node],
    indent: int,
    level: _Level,
    opts: SkeletonOpts,
    lines: list[str],
    dropped: list[int],
) -> list[dict[str, Any]]:
    L = svc.lines
    data: list[dict[str, Any]] = []
    for node in nodes:
        s = node.sym
        too_deep = level.max_depth is not None and indent >= level.max_depth
        if too_deep or ("priv" in s.modifiers and not level.private):
            dropped[0] += node.count()
            continue
        start, end = s.range.line_span_external()
        fold = end - start if s.kind in _FOLDABLE and end > start else None
        lines.append(
            L.symbol_line(
                s.kind, s.name, indent=indent, modifiers=s.modifiers, sig=s.signature,
                start_line=start, end_line=end, fold_lines=fold,  # signature は戻り値の型を含む
            )  # fmt: skip
        )
        item = s.to_json()
        if fold is not None:
            item["fold_lines"] = fold
        if level.doc and opts.include_doc_summary and s.doc_summary:
            lines.append(L.attr_line("doc", s.doc_summary, indent=indent))
        else:
            item.pop("doc_summary", None)
        public = "priv" not in s.modifiers
        want_urn = level.urn and (opts.include_urns == "all" or (opts.include_urns == "public" and public))
        if want_urn:
            lines.append(L.attr_line("urn", str(s.urn), indent=indent))
        else:
            item.pop("urn", None)
        item["children"] = _emit(svc, node.children, indent + 1, level, opts, lines, dropped)
        data.append(item)
    return data


def _build(svc: ToolServices, an: FileAnalysis, opts: SkeletonOpts, roots: list[_Node], imports: list[str],
           level: _Level, priority: int) -> Record:  # fmt: skip
    L = svc.lines
    body: list[str] = []
    shown_imports = imports[:IMPORTS_MAX] + ([f"…+{len(imports) - IMPORTS_MAX}"] if len(imports) > IMPORTS_MAX else [])
    if opts.include_imports and shown_imports:
        body.append(L.import_line(shown_imports))
    dropped = [0]
    symbols = _emit(svc, roots, 0, level, opts, body, dropped)
    if dropped[0]:
        body.append(L.note(f"truncated: {dropped[0]} members omitted"))
    rel = str(an.rel)
    header = L.file_block_header(rel, lines=an.line_count, skeleton_lines=len(body), sha12=an.sha256[:12])
    data = {
        "ok": True,
        "path": rel,
        "language": an.language_id,
        "tier": int(an.tier),
        "line_count": an.line_count,
        "skeleton_line_count": len(body),
        "file_sha256": an.sha256,
        "imports": imports if opts.include_imports else [],
        "symbols": symbols,
        "truncated_members": dropped[0],
        "confidence_level": Confidence.STRUCTURAL.value,
    }
    shrink = None
    if level is _LEVELS[0]:

        def shrink(budget: int) -> Record | None:
            for lv in _LEVELS[1:]:
                rec = _build(svc, an, opts, roots, imports, lv, priority)
                if estimate_tokens("\n".join(rec.lines)) <= budget:
                    return rec
            return None

    return Record(
        kind="file-block",
        key=rel,
        priority=priority,
        lines=(header, *body),
        data=data,
        confidence=Confidence.STRUCTURAL,
        group=rel,
        shrink=shrink,
    )


def build_file_record(svc: ToolServices, an: FileAnalysis, opts: SkeletonOpts, priority: int) -> Record:
    """1 ファイルの骨格を 1 つの ``file-block`` レコードにする（縮退つき）。"""
    roots = _nest(an.symbols)
    if not opts.include_private:
        roots = _prune_private(roots)
    return _build(svc, an, opts, roots, _imports(an), _LEVELS[0], priority)
