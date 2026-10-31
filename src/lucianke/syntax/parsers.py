"""Parser の持ち方と構文木キャッシュ（設計書 §7.4.2、ADR-13。実測は ``docs/spikes/S-02.md``）。

``TreeCache.get_or_parse`` は、同じ版（``(ws_id, rel, sha256)``）のファイルを **1 回しか解析しない**。

* ``Parser`` はスレッド安全でないので、スレッドローカル × 言語ごとに 1 個持つ。``Language`` / ``Tree`` / ``Node`` / ``Query`` は
  スレッド間で共有してよい（``QueryCursor`` だけは呼び出しごとに作る）。
* 同じキーの解析が進行中なら、2 本目以降はその完了を待ち、同じ結果を受け取る（二重解析の抑止）。
* 上限は件数（``TREE_CACHE_MAX_FILES``）とバイト数（``TREE_CACHE_MAX_BYTES``）の両方で、LRU の古い順に落とす。
  **バイト数はソースの長さ × 言語ごとの係数で見積もる**: 木はソースの 20〜26 倍のメモリを食う（S-02 実測: python 20.3、
  json 25.3、markdown 25.9）ので、ソースのバイト数で数えると実メモリは上限の 20 倍を超える。
* ``MAX_FILE_BYTES`` を超えるファイルは解析せず ``E_UNSUPPORTED``（``details.reason == "file_too_large"``）。

GIL と期限（S-02 の結論。設計書 ADR-13 の「スレッドプールで並列化する」前提は成り立たない）:

* ``Parser.parse(bytes)`` は **解析の間ずっと GIL を握る**。スレッドで走らせても並列にはならず（4 スレッドで直列の 0.8〜0.98 倍）、
  解析中は他のスレッド（asyncio のイベントループを含む）が止まる。2,000 行で約 8 ms、5 MB 近いファイルでは約 750 ms、
  ループが止まる。
* そこで解析は **常に ``read_callback`` を使う**（速度は同等）。チャンク（16 KiB）ごとにインタプリタへ戻るので、他スレッドの
  停止は最長でも約 20 ms に縮む（並列にはならない。並列度が要るなら別プロセス）。
* ``parse_deadline_ms`` を過ぎると ``read_callback`` が ``b""`` を返して解析を打ち切る。打ち切られた木はソースの先頭部分
  だけを覆うので、``ParsedFile.has_errors = True`` とし、``error_ranges`` の末尾に「解析が届かなかった範囲」を足す。
  **期限が効くのは解析の間だけ**: 木は ``read_callback`` を保持し、``Node.text`` のたびに呼び直す。解析の後も ``b""`` を返すと
  ``Node.text`` が戻らず、イベントループが空回りする（BUG-0020）ので、解析が終わった後は期限と打ち切りを無視してソースをそのまま返す。
  打ち切られた結果は **キャッシュしない**（次の要求で改めて解析する）。``progress_callback`` は Windows でプロセスが落ちるので
  使わない。例外を投げる方式も不安定（``SystemError``）なので使わない。
* ``async aget_or_parse`` は ``asyncio.to_thread`` の包み。イベントループ上で直接 ``get_or_parse`` を呼んではならない。

位置: Tree-sitter の列は行頭からの **バイト数**、行は ``\\n`` だけで切る。``ParsedFile.error_ranges`` は列を含め ``LineIndex`` の
バイトオフセットから作り直す（``\\r`` 単独・BOM があっても Lucianke の ``Range`` = 0 始まり・コードポイントに揃う）。
``ParsedFile.source`` は渡されたバイト列そのまま（BOM つきならそのまま。Tree-sitter はどの文法でも BOM を受理する）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import tree_sitter

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.foreground import FOREGROUND, cooperate
from lucianke.core.limits import (
    MAX_FILE_BYTES,
    STRUCTURAL_SEARCH_MS,
    TREE_CACHE_MAX_BYTES,
    TREE_CACHE_MAX_FILES,
)
from lucianke.core.model import Position, Range, RelPath
from lucianke.core.textpos import LineIndex
from lucianke.syntax.support import SUPPORT
from lucianke.syntax.types import ParsedFile

__all__ = [
    "DEFAULT_PARSE_DEADLINE_MS",
    "ERROR_RANGES_MAX",
    "PARSE_CHUNK_BYTES",
    "TreeCache",
    "TreeCacheStats",
    "estimate_tree_bytes",
    "iter_matches",
    "parse_source",
]

DEFAULT_PARSE_DEADLINE_MS = (
    STRUCTURAL_SEARCH_MS  # 1 ファイルの解析の既定の期限。5 MB でも通常は 1 秒未満なので安全網
)
PARSE_CHUNK_BYTES = (
    16 * 1024
)  # read_callback が 1 回に返す量（S-02: 4〜64 KiB で他スレッドの停止が 16〜23 ms）
ERROR_RANGES_MAX = (
    10_000  # 壊れた巨大ファイルで ERROR が際限なく出るのを抑える。超えた分は数えない（has_errors は立つ）
)
QUERY_WINDOW_BYTES = 256 * 1024
"""``iter_matches`` がこれより大きいソースをこの幅のバイト範囲ごとに照合する（BUG-0112）。"""

# 木のメモリ ÷ ソースのバイト数（S-02 run_memory.py の実測。python 20.3、json 25.3、markdown 25.9 を切り上げ）
_TREE_MEMORY_FACTOR: dict[str, int] = {"python": 21, "json": 26, "markdown": 26}
_TREE_MEMORY_FACTOR_DEFAULT = 24


def estimate_tree_bytes(language_id: str, source_len: int) -> int:
    """木が使うメモリの見積もり（ソースの長さ × 言語ごとの係数）。キャッシュのバイト上限はこの値で数える。"""
    return source_len * _TREE_MEMORY_FACTOR.get(language_id, _TREE_MEMORY_FACTOR_DEFAULT)


# --- Parser（スレッドローカル × 言語ごと） --------------------------------------------------

_local = threading.local()


def _parser_for(language_id: str, language: tree_sitter.Language) -> tree_sitter.Parser:
    parsers: dict[str, tree_sitter.Parser] | None = getattr(_local, "parsers", None)
    if parsers is None:
        parsers = _local.parsers = {}
    parser = parsers.get(language_id)
    if parser is None:
        parser = parsers[language_id] = tree_sitter.Parser(language)
    return parser


def _grammar(language_id: str) -> tree_sitter.Language:
    spec = SUPPORT.get(language_id)
    if spec is None:
        raise LuciankeError(
            ErrorCode.E_UNSUPPORTED,
            f"no grammar registered for language {language_id!r}",
            details={"reason": "no_grammar", "language_id": language_id},
        )
    return spec.grammar()


def _parse_with_deadline(
    parser: tree_sitter.Parser, source: bytes, deadline_ms: int | None
) -> tuple[tree_sitter.Tree, int | None]:
    """``read_callback`` 版の解析。``(木, 打ち切った位置)``。打ち切らなければ位置は ``None``。"""
    n = len(source)
    t_end: list[float | None] = [None if deadline_ms is None else time.perf_counter() + deadline_ms / 1000.0]
    stopped: list[int | None] = [None]
    parsing = [True]
    # 直近に返した塊をつなぎとめる（BUG-0051）。py-tree-sitter 0.26.0 の ``Node.text`` は、コールバックの戻り値の ``bytes`` を
    # 返す前に解放して使う（解放後の読み取り）。16 KiB の塊は解放で OS に返ることがあり、木の走査が続くとアクセス違反になる。
    # 木はこの関数を保持するので、直近の塊は木と同じ寿命で残る（最大 16 KiB）。
    last: list[bytes] = [b""]

    def read(offset: int, _point: Any) -> bytes:
        # 期限と打ち切りは **解析の間だけ** 効かせる。木はこの関数を保持し、``Node.text`` のたびに呼び直す（解析が終わった後も）。
        # 解析後に ``b""`` を返すと ``Node.text`` が戻らない（BUG-0020: 期限〈5 s〉後の ``get_symbol_bodies`` でイベントループが空回り）。
        if offset >= n:
            return b""  # 末尾の確認は期限切れにしない
        if parsing[0]:
            if stopped[0] is not None:
                return b""
            # 背景の索引の解析なら、前景のツールの呼び出しの間は塊ごとに眠って譲る。眠った分は期限を延ばす（譲って打ち切られない。BUG-0112）
            slept = FOREGROUND.pause()
            deadline = t_end[0]
            if deadline is not None:
                deadline = t_end[0] = deadline + slept
                if time.perf_counter() >= deadline:
                    stopped[0] = offset
                    return b""
        chunk = last[0] = source[offset : offset + PARSE_CHUNK_BYTES]
        return chunk

    try:
        tree = parser.parse(read)
    finally:
        parsing[0] = False
    return tree, stopped[0]


def _iter_error_spans(root: tree_sitter.Node) -> Iterator[tuple[int, int]]:
    """ERROR / MISSING ノードのバイト範囲を先頭から順に返す（ERROR の中へは降りない。MISSING は幅 0）。"""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            yield node.start_byte, node.end_byte
        elif node.has_error:
            stack.extend(reversed(node.children))


def iter_matches(
    query: tree_sitter.Query, root: tree_sitter.Node, source_len: int
) -> Iterator[tuple[int, dict[str, list[tree_sitter.Node]]]]:
    """``QueryCursor(query).matches(root)`` と同じ照合の結果を返す。``QUERY_WINDOW_BYTES`` より大きいソースは、その幅のバイト範囲
    （``set_byte_range``）ごとに照合し、範囲の間で背景の索引なら前景に譲る（``core/foreground.cooperate``。BUG-0112）。

    1 回の ``matches`` は照合と結果の組み立ての全体で GIL を握る。圧縮した JS の 1 行 4.5 MB では 70 万件・2〜10 秒になり、その間
    イベントループもツールの呼び出しも止まっていた。範囲をまたぐ一致は両方の範囲で返る（同じ一致が 2 度来る）ので、受け手は
    同じノードを 2 度受けても結果が変わらない使い方にすること（``relations._collect`` と ``symbols`` の抽出は、ノードの開始位置・
    ``id`` で引いて上書きしない・同じ値で上書きする）。範囲ごとの照合と全体の照合の一致の集合が同じことは実ファイル
    （圧縮した JS・Lucianke 自身）で確かめている。
    """
    cursor = tree_sitter.QueryCursor(query)
    if source_len <= QUERY_WINDOW_BYTES:
        yield from cursor.matches(root)
        return
    for start in range(0, source_len, QUERY_WINDOW_BYTES):
        cooperate()
        cursor.set_byte_range(start, min(source_len, start + QUERY_WINDOW_BYTES))
        yield from cursor.matches(root)


def parse_source(
    source: bytes,
    language_id: str,
    sha256: str,
    *,
    deadline_ms: int | None = DEFAULT_PARSE_DEADLINE_MS,
) -> tuple[ParsedFile, bool]:
    """``source`` を解析して ``(ParsedFile, 打ち切られたか)`` を返す。キャッシュは使わない（呼ぶスレッドの Parser で解析する）。

    打ち切られた（``deadline_ms`` 超過）ときは ``has_errors=True`` で、``error_ranges`` の末尾に解析の届かなかった範囲が入る。
    """
    language = _grammar(language_id)
    parser = _parser_for(language_id, language)
    tree, stopped_at = _parse_with_deadline(parser, source, deadline_ms)
    line_index = LineIndex(source)

    def pos(offset: int) -> Position:
        return line_index.offset_to_position(offset).value

    spans: list[tuple[int, int]] = []
    if tree.root_node.has_error:
        for span in _iter_error_spans(tree.root_node):
            spans.append(span)
            if len(spans) >= ERROR_RANGES_MAX:
                break
    timed_out = stopped_at is not None and stopped_at < len(source)
    if timed_out:
        assert stopped_at is not None
        spans.append((stopped_at, len(source)))
    ranges = tuple(Range(pos(a), pos(b)) for a, b in spans)
    parsed = ParsedFile(
        language_id=language_id,
        sha256=sha256,
        source=source,
        tree=tree,
        line_index=line_index,
        has_errors=bool(spans) or tree.root_node.has_error,
        error_ranges=ranges,
    )
    return parsed, timed_out


# --- TreeCache ----------------------------------------------------------------------------

_Key = tuple[str, RelPath, str]


@dataclass(frozen=True, slots=True)
class TreeCacheStats:
    """``TreeCache.stats()`` の結果。``bytes`` は見積もり（``estimate_tree_bytes``）の合計。"""

    files: int
    bytes: int
    max_files: int
    max_bytes: int
    hits: int
    misses: int
    parses: int  # 実際に Tree-sitter を呼んだ回数
    waits: int  # 進行中の解析を待って結果を受け取った回数
    evictions: int
    timeouts: int  # 期限で打ち切った回数（キャッシュしない）


class _Flight:
    """進行中の解析 1 件。待つ側は ``event`` を待ち、``result`` か ``error`` を受け取る。"""

    __slots__ = ("error", "event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: ParsedFile | None = None
        self.error: BaseException | None = None


class TreeCache:
    """構文木の LRU キャッシュ。全メソッドはスレッドから呼んでよい（内部ロックは短時間しか持たない）。解析中はロックを持たない。"""

    def __init__(
        self,
        *,
        max_files: int = TREE_CACHE_MAX_FILES,
        max_bytes: int = TREE_CACHE_MAX_BYTES,
        max_file_bytes: int = MAX_FILE_BYTES,
        parse_deadline_ms: int | None = DEFAULT_PARSE_DEADLINE_MS,
    ) -> None:
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._max_file_bytes = max_file_bytes
        self._parse_deadline_ms = parse_deadline_ms
        self._lock = threading.Lock()
        self._entries: OrderedDict[_Key, tuple[ParsedFile, int]] = OrderedDict()  # 末尾が最近
        self._by_path: dict[tuple[str, RelPath], set[str]] = {}
        self._inflight: dict[_Key, _Flight] = {}
        self._total = 0
        self._hits = self._misses = self._parses = self._waits = self._evictions = self._timeouts = 0

    # --- 公開 API ---

    def get_or_parse(
        self,
        ws_id: str,
        rel: RelPath,
        sha256: str,
        language_id: str,
        load: Callable[[], bytes],
        *,
        parse_deadline_ms: int | None = None,
    ) -> ParsedFile:
        """``(ws_id, rel, sha256)`` の構文木を返す。無ければ ``load()`` でバイト列を読んで解析する（ブロッキング）。

        ``load`` は解析する側のスレッドで 1 回だけ呼ばれる（待つ側は呼ばない）。``MAX_FILE_BYTES`` 超は ``E_UNSUPPORTED``。
        ``parse_deadline_ms`` を省くと ``TreeCache`` の既定。イベントループ上では ``aget_or_parse`` を使うこと。
        """
        key: _Key = (ws_id, rel, sha256)
        with self._lock:
            hit = self._lookup(key, language_id)
            if hit is not None:
                self._hits += 1
                return hit
            flight = self._inflight.get(key)
            leader = flight is None
            if flight is None:
                flight = self._inflight[key] = _Flight()
                self._misses += 1
            else:
                self._waits += 1
        if not leader:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            assert flight.result is not None
            return flight.result
        try:
            result = self._load_and_parse(key, language_id, load, parse_deadline_ms)
        except BaseException as e:
            flight.error = e
            raise
        else:
            flight.result = result
            return result
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            flight.event.set()

    async def aget_or_parse(
        self,
        ws_id: str,
        rel: RelPath,
        sha256: str,
        language_id: str,
        load: Callable[[], bytes],
        *,
        parse_deadline_ms: int | None = None,
    ) -> ParsedFile:
        """``get_or_parse`` を ``asyncio.to_thread`` で走らせる。解析中も GIL は握られる（モジュールの docstring を参照）。"""
        return await asyncio.to_thread(
            self.get_or_parse, ws_id, rel, sha256, language_id, load, parse_deadline_ms=parse_deadline_ms
        )

    def peek(self, ws_id: str, rel: RelPath, sha256: str) -> ParsedFile | None:
        """キャッシュにあれば返す。解析も読み込みもしない。LRU の順序と統計は変えない。"""
        with self._lock:
            entry = self._entries.get((ws_id, rel, sha256))
            return None if entry is None else entry[0]

    def evict(self, ws_id: str, rel: RelPath) -> None:
        """``(ws_id, rel)`` のすべての版を落とす（ファイルの変更・削除時）。"""
        with self._lock:
            for sha in list(self._by_path.get((ws_id, rel), ())):
                self._drop((ws_id, rel, sha))

    def clear(self, ws_id: str | None = None) -> None:
        """全部、または ``ws_id`` のものだけを落とす（ワークスペースの登録解除時）。"""
        with self._lock:
            for key in [k for k in self._entries if ws_id is None or k[0] == ws_id]:
                self._drop(key)

    def stats(self) -> TreeCacheStats:
        with self._lock:
            return TreeCacheStats(
                files=len(self._entries),
                bytes=self._total,
                max_files=self._max_files,
                max_bytes=self._max_bytes,
                hits=self._hits,
                misses=self._misses,
                parses=self._parses,
                waits=self._waits,
                evictions=self._evictions,
                timeouts=self._timeouts,
            )

    # --- 内部（``_lookup`` / ``_drop`` / ``_store`` は ``self._lock`` を持って呼ぶ） ---

    def _lookup(self, key: _Key, language_id: str) -> ParsedFile | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry[0].language_id != language_id:  # 同じ版でも言語が変わった（判定の差し替え）ときは作り直す
            self._drop(key)
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def _drop(self, key: _Key) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        self._total -= entry[1]
        shas = self._by_path.get((key[0], key[1]))
        if shas is not None:
            shas.discard(key[2])
            if not shas:
                del self._by_path[(key[0], key[1])]
        self._evictions += 1

    def _store(self, key: _Key, parsed: ParsedFile) -> None:
        old = self._entries.get(key)
        if old is not None:
            self._total -= old[1]
        cost = estimate_tree_bytes(parsed.language_id, len(parsed.source))
        self._entries[key] = (parsed, cost)
        self._entries.move_to_end(key)
        self._total += cost
        self._by_path.setdefault((key[0], key[1]), set()).add(key[2])
        while self._entries and (len(self._entries) > self._max_files or self._total > self._max_bytes):
            oldest = next(
                iter(self._entries)
            )  # 1 件で上限を超えるなら、入れたばかりのものも落ちる（呼び手には返る）
            self._drop(oldest)

    def _load_and_parse(
        self, key: _Key, language_id: str, load: Callable[[], bytes], parse_deadline_ms: int | None
    ) -> ParsedFile:
        _grammar(language_id)  # 文法が無い言語は、読み込む前に E_UNSUPPORTED
        source = load()
        if len(source) > self._max_file_bytes:
            raise LuciankeError(
                ErrorCode.E_UNSUPPORTED,
                f"file too large to parse: {len(source)} bytes > {self._max_file_bytes}",
                details={"reason": "file_too_large", "size": len(source), "limit": self._max_file_bytes},
            )
        deadline = self._parse_deadline_ms if parse_deadline_ms is None else parse_deadline_ms
        parsed, timed_out = parse_source(source, language_id, key[2], deadline_ms=deadline)
        with self._lock:
            self._parses += 1
            if timed_out:
                self._timeouts += 1
            else:
                self._store(key, parsed)
        return parsed
