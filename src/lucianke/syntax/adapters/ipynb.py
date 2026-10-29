"""Jupyter Notebook（``.ipynb``）のアダプタ（仕様書 §8.6 B2・§11 T-02 処理規約 3・T-05 処理規約 4、設計書 §7.4.8）。

``.ipynb`` は JSON である。標準の ``json`` で読み、``nbformat`` は使わない（付録 A）。``outputs`` と ``metadata`` は
**読まない**（Base64 画像や巨大な実行結果が応答に混ざらないようにする。MUST）。見るのは各セルの
``cell_type`` / ``source`` / ``execution_count`` だけ。

仮想 Python ファイル（``to_virtual_python``）:

* **コードセルの ``source`` だけ** を順に連結する。セルの間は空行 1 つ。各セルは改行で終わらせる（``source`` が改行で終わらなければ 1 つ足す）。
* Markdown / raw セルは仮想ファイルに含めない（行を占めない）。骨格には ``[Cell] n markdown`` として出し、
  行番号の欄（``@L``）には「そのセルが挟まっている位置」= 次のコードセルの開始行（後ろにコードセルが無ければ最終行）を書く。
* 行番号は **仮想ファイル上の行**（T-02・T-05・編集ツールで共通）。``CellMap`` の行は 0 始まりで、終了行を含む。

セル番号は ``cells`` 配列の 1 始まりの位置（Markdown も数える）。``cell:3`` は ``cells[2]``。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lucianke.core.model import Confidence, Range, Tier
from lucianke.core.records import Record
from lucianke.syntax.types import Match, SkeletonOptions

__all__ = ["ADAPTER", "CellInfo", "CellMap", "IpynbAdapter", "select", "skeleton", "to_virtual_python"]

_LINE_LIMIT = 200  # compact 規約 6（1 行の上限。doc: はこれを超えたら末尾を … にする）
_MODIFIERS = frozenset({"pub", "priv", "prot", "static", "async", "abstract", "export"})
_FOLDABLE = frozenset({"Function", "Method", "Constructor"})
_SELECTOR_RE = re.compile(r"cell:(\d+)")
_HEADING_RE = re.compile(r" {0,3}#{1,6}(?:\s|$)")
_FENCE_RE = re.compile(r" {0,3}(```|~~~)")


# --- セル表 -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CellInfo:
    """セル 1 つ。行は仮想ファイル上の 0 始まりで、``end_line`` を含む。

    Attributes:
        number: ``cells`` 配列の 1 始まりの位置（全セルを数える）。
        kind: ``"code"`` | ``"markdown"`` | ``"raw"``（それ以外の文字列はそのまま）。
        exec_count: ``execution_count``（未実行・非コードは ``None``）。
        start_line / end_line: コードセルの仮想ファイル上の行。コードセル以外は ``None``（行を占めない）。
        anchor_line: コードセル以外が「挟まっている」位置。次のコードセルの開始行、無ければ最終行。コードセルでは ``start_line``。
        json_index: 元の JSON の ``cells`` 配列の添字（0 始まり）。
    """

    number: int
    kind: str
    exec_count: int | None
    start_line: int | None
    end_line: int | None
    anchor_line: int
    json_index: int

    @property
    def is_code(self) -> bool:
        return self.start_line is not None

    @property
    def span_external(self) -> tuple[int, int]:
        """``@L<a>-<b>`` に書く 1 始まり・両端を含む行範囲（コードセル以外は ``anchor_line`` だけ）。"""
        if self.start_line is None or self.end_line is None:
            return (self.anchor_line + 1, self.anchor_line + 1)
        return (self.start_line + 1, self.end_line + 1)


@dataclass(frozen=True, slots=True)
class CellMap:
    """セル境界の表。``cells`` は ``cells`` 配列の順、``line_count`` は仮想ファイルの行数（``wc -l`` 流）。"""

    cells: tuple[CellInfo, ...]
    line_count: int

    def get(self, number: int) -> CellInfo:
        """1 始まりのセル番号から引く。範囲外は ``ValueError``。"""
        if not 1 <= number <= len(self.cells):
            raise ValueError(f"cell:{number} is out of range (the notebook has {len(self.cells)} cells)")
        return self.cells[number - 1]

    def cell_at_line(self, line: int) -> CellInfo | None:
        """仮想ファイルの 0 始まりの行を含むコードセル。セルの間の空行は ``None``。"""
        for cell in self.cells:
            if (
                cell.start_line is not None
                and cell.end_line is not None
                and cell.start_line <= line <= cell.end_line
            ):
                return cell
        return None


@dataclass(frozen=True, slots=True)
class _RawCell:
    number: int
    kind: str
    exec_count: int | None
    source: str
    json_index: int


def _load(source: bytes) -> list[_RawCell]:
    """JSON を読み、各セルの ``cell_type`` / ``source`` / ``execution_count`` だけを取り出す。壊れていれば ``ValueError``。"""
    try:
        doc = json.loads(source.decode("utf-8-sig"))
    except ValueError as exc:  # JSONDecodeError と UnicodeDecodeError はどちらも ValueError
        raise ValueError(f"not a valid notebook (JSON): {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("cells"), list):
        raise ValueError(
            "not a valid notebook: the top level must be an object with a 'cells' array (nbformat >= 4)"
        )
    cells: list[_RawCell] = []
    for i, raw in enumerate(doc["cells"]):
        if not isinstance(raw, dict):
            raise ValueError(f"cells[{i}] is not an object")
        text = raw.get("source", "")
        if isinstance(text, list):
            if not all(isinstance(part, str) for part in text):
                raise ValueError(f"cells[{i}].source must be a string or an array of strings")
            text = "".join(text)
        elif not isinstance(text, str):
            raise ValueError(f"cells[{i}].source must be a string or an array of strings")
        kind = raw.get("cell_type")
        if not isinstance(kind, str):
            raise ValueError(f"cells[{i}].cell_type is missing")
        count = raw.get("execution_count")
        exec_count = count if isinstance(count, int) and not isinstance(count, bool) else None
        cells.append(_RawCell(i + 1, kind, exec_count if kind == "code" else None, text, i))
    return cells


def _block(text: str) -> bytes:
    """コードセル 1 つぶんの仮想ファイル上のバイト列（改行で終わる。空のセルは空行 1 つ）。"""
    data = text.encode("utf-8", "replace")
    return data if data.endswith(b"\n") else data + b"\n"


def _build(cells: Sequence[_RawCell]) -> tuple[bytes, CellMap]:
    blocks: list[bytes] = []
    spans: dict[int, tuple[int, int]] = {}  # セル番号 → (開始行, 終了行)
    line = 0
    for cell in cells:
        if cell.kind != "code":
            continue
        block = _block(cell.source)
        n = block.count(b"\n")
        spans[cell.number] = (line, line + n - 1)
        blocks.append(block)
        line += n + 1  # 空行 1 つ
    virtual = b"\n".join(blocks)
    line_count = (line - 1) if blocks else 0

    infos: list[CellInfo | None] = [None] * len(cells)
    pending: list[_RawCell] = []  # 次のコードセルが決まるのを待つ非コードセル

    def settle(anchor: int) -> None:
        for p in pending:
            infos[p.number - 1] = CellInfo(p.number, p.kind, None, None, None, anchor, p.json_index)
        pending.clear()

    for cell in cells:
        if cell.kind == "code":
            start, end = spans[cell.number]
            settle(start)
            infos[cell.number - 1] = CellInfo(
                cell.number, "code", cell.exec_count, start, end, start, cell.json_index
            )
        else:
            pending.append(cell)
    settle(max(line_count - 1, 0))
    return virtual, CellMap(tuple(i for i in infos if i is not None), line_count)


def to_virtual_python(source: bytes) -> tuple[bytes, CellMap]:
    """``.ipynb`` の全コードセルの ``source`` を連結した仮想 Python ファイルと、セル境界の表を返す。

    ``outputs`` と ``metadata`` は読まない。壊れた JSON・``cells`` の無い JSON は ``ValueError``。
    """
    return _build(_load(source))


# --- 骨格 -------------------------------------------------------------------------------


def _first_heading(text: str) -> str | None:
    """Markdown の最初の見出し行（前後の空白を除く。コードフェンスの中は見ない）。無ければ ``None``。"""
    fence: str | None = None
    for line in text.splitlines():
        m = _FENCE_RE.match(line)
        if m is not None:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence is None and _HEADING_RE.match(line):
            return line.strip()
    return None


def _one_line(text: str) -> str:
    return " ".join(p.strip() for p in text.splitlines() if p.strip())


def _fit(prefix: str, text: str) -> str:
    if len(prefix) + len(text) <= _LINE_LIMIT:
        return text
    return text[: max(_LINE_LIMIT - len(prefix) - 1, 0)].rstrip() + "…"


def _span_text(start: int, end: int) -> str:
    return f"{start}" if end == start else f"{start}-{end}"


def _is_private(sym: Any) -> bool:
    name: str = sym.name
    if "priv" in tuple(getattr(sym, "modifiers", ())):
        return True
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


@dataclass(slots=True)
class _Node:
    sym: Any
    start: int  # 1 始まり
    end: int
    children: list[_Node]


def _nest(symbols: Sequence[Any], cmap: CellMap) -> dict[int, list[_Node]]:
    """シンボル列を、含まれるコードセルごとに範囲の包含で入れ子にする（セルの外に出るものは捨てる）。"""
    per_cell: dict[int, list[_Node]] = {}
    ordered = sorted(
        ((s, *s.range.line_span_external()) for s in symbols),
        key=lambda t: (t[1], -t[2]),
    )
    stacks: dict[int, list[_Node]] = {}
    for sym, start, end in ordered:
        cell = cmap.cell_at_line(start - 1)
        if cell is None or cell.end_line is None or end > cell.end_line + 1:
            continue
        node = _Node(sym, start, end, [])
        stack = stacks.setdefault(cell.number, [])
        while stack and not (stack[-1].start <= start and end <= stack[-1].end):
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            per_cell.setdefault(cell.number, []).append(node)
        stack.append(node)
    return per_cell


def _symbol_lines(node: _Node, indent: int, opts: SkeletonOptions, out: list[str]) -> dict[str, Any] | None:
    sym = node.sym
    if not opts.include_private and _is_private(sym):
        return None
    pad = "  " * indent
    kind = str(getattr(sym.kind, "value", sym.kind))
    mods = "".join(f"{m} " for m in getattr(sym, "modifiers", ()) if m in _MODIFIERS)
    signature = getattr(sym, "signature", None)
    head = f"{pad}[{kind}] {mods}{sym.name}{_one_line(signature) if signature else ''} @L{_span_text(node.start, node.end)}"
    slot = len(out)
    out.append(head)
    doc = getattr(sym, "doc_summary", None)
    if opts.include_doc_summary and doc:
        prefix = f"{pad}  doc: "
        out.append(prefix + _fit(prefix, _one_line(doc)))
    children: list[dict[str, Any]] = []
    for child in node.children:
        item = _symbol_lines(child, indent + 1, opts, out)
        if item is not None:
            children.append(item)
    if not node.children and kind in _FOLDABLE and node.end > node.start:
        out[slot] = f"{head} {{ ...{node.end - node.start} lines omitted }}"
    return {
        "name": sym.name,
        "kind": kind,
        "signature": signature,
        "range": {"start_line": node.start, "end_line": node.end},
        "children": children,
    }


def skeleton(
    source: bytes,
    opts: SkeletonOptions,
    *,
    rel: str,
    sha256: str,
    python_symbols: Callable[[bytes], Sequence[Any]] | None = None,
) -> Record:
    """Notebook の骨格 1 ファイルブロック（仕様書 §11 T-02 処理規約 3）。

    ``python_symbols`` は仮想 Python ファイルのバイト列を受け取り、その中の定義（``SymbolRecord`` 相当。``name`` / ``kind`` /
    ``signature`` / ``modifiers`` / ``doc_summary`` / ``range``。行は仮想ファイル上）を返す。与えられなければセル行だけを出す。
    ``urn:`` 行は出さない（ワークスペース名を持たず、仮想ファイル上の URN は実在しないため）。
    """
    virtual, cmap = to_virtual_python(source)
    nested = _nest(python_symbols(virtual), cmap) if python_symbols is not None else {}
    raw = {c.number: c for c in _load(source)}  # Markdown の見出し用（outputs は触らない）

    body: list[str] = []
    cell_items: list[dict[str, Any]] = []
    for cell in cmap.cells:
        start, end = cell.span_external
        item: dict[str, Any] = {
            "number": cell.number,
            "kind": cell.kind,
            "exec_count": cell.exec_count,
            "start_line": start if cell.is_code else None,
            "end_line": end if cell.is_code else None,
            "symbols": [],
        }
        count = f" (exec_count: {cell.exec_count})" if cell.exec_count is not None else ""
        body.append(f"[Cell] {cell.number} {cell.kind}{count} @L{_span_text(start, end)}")
        if cell.kind == "markdown" and opts.include_doc_summary:
            heading = _first_heading(raw[cell.number].source)
            if heading:
                prefix = "  doc: "
                body.append(prefix + _fit(prefix, _one_line(heading)))
        for node in nested.get(cell.number, []):
            sym_item = _symbol_lines(node, 1, opts, body)
            if sym_item is not None:
                item["symbols"].append(sym_item)
        cell_items.append(item)

    line_count = cmap.line_count
    header = (
        f"=== {_one_line(rel)} ({line_count} lines, {len(cmap.cells)} cells, "
        f"skeleton: {len(body)} lines, sha={sha256[:12].lower()}) ==="
    )
    data: Mapping[str, Any] = {
        "path": rel,
        "language": "ipynb",
        "tier": int(Tier.T2),
        "line_count": line_count,
        "cell_count": len(cmap.cells),
        "skeleton_line_count": len(body),
        "file_sha256": sha256,
        "cells": cell_items,
        "confidence_level": Confidence.STRUCTURAL.value,
    }
    return Record(
        kind="file-block",
        key=rel,
        priority=0,
        lines=(header, *body),
        data=data,
        confidence=Confidence.STRUCTURAL,
        group=rel,
    )


# --- 部分取得 ---------------------------------------------------------------------------


def select(source: bytes, selector: str) -> list[Match]:
    """``cell:<n>``（1 始まり）で、そのセルの ``source`` の連結だけを返す（``outputs`` は含めない。MUST）。

    ``Match.range`` は仮想ファイル上の行（コードセル）。Markdown / raw セルは行を占めないので、``anchor_line`` の 1 行を指し、
    ``data["in_virtual_file"]`` が ``False``。構文の誤り・範囲外のセル番号は ``ValueError``。
    """
    m = _SELECTOR_RE.fullmatch(selector.strip())
    if m is None:
        raise ValueError(f"unsupported notebook selector {selector!r}: expected 'cell:<n>' (n >= 1)")
    number = int(m.group(1))
    cells = _load(source)
    _, cmap = _build(cells)
    cell = cmap.get(number)  # 範囲外は ValueError（cell:0 も含む）
    start, end = cell.span_external
    return [
        Match(
            range=Range.from_line_span_external(start, end),
            text=cells[number - 1].source,
            kind="Cell",
            name=f"cell:{number}",
            data={
                "number": number,
                "cell_type": cell.kind,
                "exec_count": cell.exec_count,
                "json_index": cell.json_index,
                "in_virtual_file": cell.is_code,
            },
        )
    ]


# --- 登録用 -----------------------------------------------------------------------------


class IpynbAdapter:
    """``FormatAdapter``（設計書 §7.4.8）。``ADAPTERS`` への登録は ``syntax/adapters/__init__.py`` が行う。"""

    language_id = "ipynb"
    tier = Tier.T2
    extensions = (".ipynb",)

    def skeleton(self, source: bytes, opts: SkeletonOptions) -> list[Record]:
        """``rel`` と ``sha256`` の受け口が Protocol に無いため、``rel`` は空・``sha256`` は ``source`` から計算する。

        呼び出し側（T-02）は ``skeleton()`` 関数に ``rel`` / ``sha256`` / ``python_symbols`` を渡して直接呼ぶこと。
        """
        return [skeleton(source, opts, rel="", sha256=hashlib.sha256(source).hexdigest())]

    def select(self, source: bytes, selector: str) -> list[Match]:
        return select(source, selector)

    def replace(self, source: bytes, selector: str, new: bytes) -> bytes:
        raise NotImplementedError("editing .ipynb cells is M2")


ADAPTER = IpynbAdapter()
