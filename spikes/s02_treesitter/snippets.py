"""S-2 の「使うコードの型」: syntax 層を作る役がそのまま写せる最小部品（run_api.py が検証する）。

使い方: 直接実行しない。`uv run python spikes/s02_treesitter/run_api.py` が import して検証する。
内容: 言語の読み込み表 / スレッドローカルの ParserPool / タグ風クエリの実行 / ERROR・MISSING の列挙 / LineIndex（バイト→行・コードポイント・UTF-16）。
注意: 位置はすべて 0 始まり。tree-sitter の列は「その行の先頭からのバイト数」。
"""

from __future__ import annotations

import functools
import importlib
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass

from tree_sitter import Language, Node, Parser, Query, QueryCursor, Tree

# ---- 言語の読み込み表（lang_id -> (import 名, 言語取得関数の実名)） ------------------------------
LANGUAGES: dict[str, tuple[str, str]] = {
    "python": ("tree_sitter_python", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "javascript": ("tree_sitter_javascript", "language"),
    "json": ("tree_sitter_json", "language"),
    "yaml": ("tree_sitter_yaml", "language"),
    "toml": ("tree_sitter_toml", "language"),
    "xml": ("tree_sitter_xml", "language_xml"),
    "dtd": ("tree_sitter_xml", "language_dtd"),
    "markdown": ("tree_sitter_markdown", "language"),
    "markdown_inline": ("tree_sitter_markdown", "inline_language"),
    "rust": ("tree_sitter_rust", "language"),
    "go": ("tree_sitter_go", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "c_sharp": ("tree_sitter_c_sharp", "language"),
    "rst": ("tree_sitter_rst", "language"),
    "sql": ("tree_sitter_sql", "language"),
}


@functools.cache
def get_language(lang_id: str) -> Language:
    """Language はスレッド間で共有してよい（不変）。読み込み失敗は例外のまま上げ、呼び出し側で登録表から外す。"""
    mod_name, func = LANGUAGES[lang_id]
    return Language(getattr(importlib.import_module(mod_name), func)())


@functools.cache
def get_query(lang_id: str, source: str) -> Query:
    """Query は不変。コンパイル（遅延）して共有する。QueryError は ValueError のサブクラス。"""
    return Query(get_language(lang_id), source)


# ---- Parser はスレッドごと・言語ごとに 1 個（ADR-13） ------------------------------------------
class ParserPool:
    def __init__(self) -> None:
        self._tl = threading.local()

    def parser(self, lang_id: str) -> Parser:
        cache: dict[str, Parser] = self._tl.__dict__.setdefault("parsers", {})
        p = cache.get(lang_id)
        if p is None:
            p = cache[lang_id] = Parser(get_language(lang_id))
        return p

    def parse(self, lang_id: str, source: bytes, old_tree: Tree | None = None) -> Tree:
        # 注意: parse(source, None) は TypeError（old_tree に None を渡せない）。無いときは引数を省く。
        parser = self.parser(lang_id)
        return parser.parse(source) if old_tree is None else parser.parse(source, old_tree)


def parse_with_deadline(parser: Parser, source: bytes, seconds: float, chunk: int = 16 * 1024) -> tuple[Tree, bool]:
    """期限つき解析。(tree, timed_out)。timed_out のとき tree は途中までなので捨てる。

    read_callback が期限後に b"" を返すと入力終端として解析がすぐ終わる（4.8 MB・期限 50 ms で実測 53 ms）。
    ついでに read_callback 版はチャンクごとにインタプリタへ戻るので、他スレッド（イベントループ）の停止が最長約 20 ms に縮む（並列にはならない）。
    例外を投げて中断する方式は不安定（SystemError になることがある）。progress_callback は 0.26.0/Windows でクラッシュする。
    """
    t_end = time.perf_counter() + seconds
    flag = [False]

    def read(offset: int, _point: object) -> bytes:
        if flag[0] or time.perf_counter() > t_end:
            flag[0] = True
            return b""
        return source[offset : offset + chunk]

    return parser.parse(read), flag[0]


# ---- タグ風クエリの実行（matches() を使う。captures() は @name と @definition の対応が失われる） ----
@dataclass(frozen=True)
class Tag:
    kind: str  # "definition.function" など（@definition.* / @reference.* の捕捉名）
    name: str | None
    node: Node  # 定義ノード全体
    name_node: Node | None


def run_tags(lang_id: str, query_src: str, tree: Tree) -> list[Tag]:
    q = get_query(lang_id, query_src)
    out: list[Tag] = []
    for _pattern_index, caps in QueryCursor(q).matches(tree.root_node):  # caps: dict[str, list[Node]]
        name_node = (caps.get("name") or [None])[0]
        for cap, nodes in caps.items():
            if cap.startswith(("definition.", "reference.")):
                for n in nodes:
                    out.append(Tag(cap, name_node.text.decode("utf-8", "replace") if name_node else None, n, name_node))
    return out


# ---- ERROR / MISSING の列挙 --------------------------------------------------------------------
@dataclass(frozen=True)
class ErrorSpan:
    kind: str  # ERROR ノードは "ERROR"、MISSING は欠けているトークン名（例 ")"）
    is_missing: bool
    start_byte: int
    end_byte: int
    start_point: tuple[int, int]  # (row, byte_col)
    end_point: tuple[int, int]


def error_spans(root: Node) -> list[ErrorSpan]:
    """has_error で枝刈りしつつ、ERROR / MISSING ノードを重ならないように列挙する（ERROR の中へは降りない）。"""
    out: list[ErrorSpan] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.is_error or n.is_missing:
            out.append(ErrorSpan(n.type, n.is_missing, n.start_byte, n.end_byte,
                                 tuple(n.start_point), tuple(n.end_point)))
        elif n.has_error:
            stack.extend(reversed(n.children))
    out.sort(key=lambda e: e.start_byte)
    return out


# ---- LineIndex: バイト位置 <-> (行, バイト列/コードポイント列/UTF-16 列) ---------------------------
class LineIndex:
    """行は b"\\n" だけで区切る（tree-sitter と同じ。\\r は行末の 1 文字として残る）。"""

    def __init__(self, source: bytes) -> None:
        self.src = source
        starts = [0]
        i = source.find(b"\n")
        while i != -1:
            starts.append(i + 1)
            i = source.find(b"\n", i + 1)
        self.starts = starts

    def line_count(self) -> int:
        return len(self.starts)

    def offset_to_point(self, offset: int) -> tuple[int, int]:
        row = bisect_right(self.starts, offset) - 1
        return row, offset - self.starts[row]

    def _prefix(self, row: int, byte_col: int) -> bytes:
        s = self.starts[row]
        return self.src[s : s + byte_col]

    def byte_col_to_cp(self, row: int, byte_col: int) -> int:
        p = self._prefix(row, byte_col)
        return byte_col if p.isascii() else len(p.decode("utf-8", "replace"))

    def byte_col_to_utf16(self, row: int, byte_col: int) -> int:
        p = self._prefix(row, byte_col)
        return byte_col if p.isascii() else len(p.decode("utf-8", "replace").encode("utf-16-le")) // 2

    def cp_col_to_byte(self, row: int, cp_col: int) -> int:
        s = self.starts[row]
        e = self.starts[row + 1] if row + 1 < len(self.starts) else len(self.src)
        line = self.src[s:e].decode("utf-8", "replace")
        return len(line[:cp_col].encode("utf-8"))

    def utf16_col_to_byte(self, row: int, u16_col: int) -> int:
        s = self.starts[row]
        e = self.starts[row + 1] if row + 1 < len(self.starts) else len(self.src)
        line16 = self.src[s:e].decode("utf-8", "replace").encode("utf-16-le")
        return len(line16[: u16_col * 2].decode("utf-16-le", "replace").encode("utf-8"))

    def point_to_cp(self, point: tuple[int, int]) -> tuple[int, int]:
        return point[0], self.byte_col_to_cp(*point)
