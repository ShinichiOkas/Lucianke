"""Tier 4（文法の無いファイル）のヒューリスティック（仕様書 §8.3・§8.4 段 3/4、設計書 §7.4.8）。

* ``outline``: アウトライン。ctags があれば子プロセスで（``ProcessRegistry`` 経由・タイムアウト付き）、
  無い・失敗・0 件なら自前のブロック推定（``block_outline``）。どちらも ``confidence=heuristic``。何も取れなければ
  ``([], lexical_fallback)``。URN は ``ctags:NAME@L<行>``（行は 1 始まり）。
* ``lexical_find``: 単語境界つきの字句検索（段 4）。定義らしい一致を先頭に並べ、前後 ``context`` 行のスニペットを付ける。
* ``block_window``: T-05 の ``lexical_fallback`` が返す窓（一致行の前後 15 行、最大 31 行）。
* ``find_ctags``: PATH から Universal Ctags を探す。GPL のため同梱せず、無くても動く（仕様書 §16.2.1）。

子プロセスを起動する関数（``find_ctags`` / ``outline``）だけが ``async``。ほかは純粋な同期関数で、
ctags の JSON の解釈（``parse_ctags_json``）も同期のまま単体で試せる。

位置は内部表現（0 始まりの行・コードポイント列。ADR-14）。URN のセレクタの行だけが 1 始まり。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from lucianke.core.limits import (
    DEGRADED_GREP_MS,
    GREP_MATCHES_MAX,
    REGEX_PATTERN_MAX_CHARS,
    SHUTDOWN_GRACE_MS,
    SNIPPET_MAX_CHARS,
)
from lucianke.core.model import Confidence, Kind, Position, Range, RelPath, SymbolRecord
from lucianke.core.procs import ProcessRegistry
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import TagSel, Urn

__all__ = [
    "BLOCK_WINDOW_RADIUS",
    "LexHit",
    "LexScan",
    "block_outline",
    "block_window",
    "find_ctags",
    "lexical_find",
    "lexical_scan",
    "outline",
    "parse_ctags_json",
]

_log = logging.getLogger("lucianke.syntax.heuristic")

BLOCK_WINDOW_RADIUS: Final = 15
"""T-05 の ``lexical_fallback`` の窓の半径（仕様書 §11 T-05 処理規約 5: 前後 15 行 = 最大 31 行）。"""

_BRACE_SCAN_LINES_MAX: Final = 5000
"""閉じていない ``{`` を追いかける行数の上限（巨大なファイルで二乗にならないように）。"""

_CTAGS_CANDIDATES: Final = ("ctags", "universal-ctags", "ctags.exe")
_CTAGS_MAX_ENTRIES: Final = 20000

# --- 定義らしい行の判定 ------------------------------------------------------------------

_START = r"(?:[^\W\d]|\$)"  # 名前の先頭: 文字・``_``・``$``（Unicode の文字を含む）
_NAME = rf"{_START}[\w$-]*(?:(?:::|\.){_START}[\w$-]*)*"
_MODIFIERS = (
    r"(?:(?:public|private|protected|static|export|async|pub|final|abstract|override|inline|local|global"
    r"|sealed|partial|extern|unsafe|default|virtual|const)\s+)*"
)
# ``PROCEDURE DIVISION.`` を拾わないよう、名前の直後は空白・括弧・``:=<{;[`` か行末に限る（``.`` は不可）。
_KEYWORD_DEF = re.compile(
    rf"^\s*{_MODIFIERS}(?P<kw>def|function|fn|sub|proc|procedure|class|module)\s+(?P<name>{_NAME})"
    r"(?=[\s(:=<{;\[]|$)",
    re.IGNORECASE,
)
_COBOL_PARAGRAPH = re.compile(r"^\s{7}(?P<name>[A-Z0-9-]+)\.\s*$")
# 比較（``==``）・``=~``・``=>`` は代入ではない。字下げ 0 の行だけを見る（ブロック内の局所変数を拾わない）。
_ASSIGN = re.compile(
    rf"^(?:(?P<kw>rule|const|let|var|val|define|set|export)\s+)?(?P<name>{_NAME})\s*=(?![=~>])",
    re.IGNORECASE,
)
_RESERVED_NAMES = frozenset({"DIVISION", "SECTION"})  # COBOL の ``PROCEDURE DIVISION USING …`` など

_KEYWORD_KINDS: Final = {
    "def": Kind.FUNCTION,
    "function": Kind.FUNCTION,
    "fn": Kind.FUNCTION,
    "sub": Kind.FUNCTION,
    "proc": Kind.FUNCTION,
    "procedure": Kind.FUNCTION,
    "class": Kind.CLASS,
    "module": Kind.MODULE,
}

# ブロックを閉じる行（定義と同じ字下げに現れる）。``end``・``END-IF``・``endfunction``・``}``・``fi`` など。
_CLOSER = re.compile(
    r"^(?:end\b.*|end(?:function|sub|proc|procedure|def|class|module|if)\b.*|[})\]][;,)]*|fi|done|esac)\s*$",
    re.IGNORECASE,
)

# 括弧の数え方から外すもの: 文字列リテラル・``//``・``#`` のコメント。
_BRACE_TOKEN = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|//.*|#.*|[{}]")


@dataclass(frozen=True, slots=True)
class _Def:
    line: int
    name: str
    kind: Kind
    col: int
    keyword: bool  # キーワード形（``def foo`` など）。ブロックの終わりを ``end`` で探してよい


class _Doc:
    """行ごとの前処理（字下げ・括弧の収支）。ブロックの終わりの推定が使う。"""

    def __init__(self, source: bytes) -> None:
        self.index = LineIndex(source)
        count = self.index.content_line_count
        self.lines: list[str] = [self.index.line_text(i) for i in range(count)]
        self.indents: list[int | None] = []
        for text in self.lines:
            expanded = text.expandtabs(4)
            stripped = expanded.lstrip()
            self.indents.append(len(expanded) - len(stripped) if stripped else None)
        self._net: dict[int, int] = {}

    def __len__(self) -> int:
        return len(self.lines)

    def net(self, i: int) -> int:
        """``i`` 行目の ``{`` の数 - ``}`` の数（文字列・コメントの中は数えない）。"""
        cached = self._net.get(i)
        if cached is not None:
            return cached
        text = self.lines[i]
        total = 0
        if "{" in text or "}" in text:
            for m in _BRACE_TOKEN.finditer(text):
                tok = m.group()
                if tok == "{":
                    total += 1
                elif tok == "}":
                    total -= 1
        self._net[i] = total
        return total

    def has_open(self, i: int) -> bool:
        return "{" in self.lines[i]

    # --- 終了行の推定 --------------------------------------------------------------

    def block_end(self, start: int, *, keyword: bool) -> int:
        """``start`` 行から始まるブロックの推定終了行（0 始まり、含む）。

        1. ``{`` が定義行で開いて閉じていない（または次の行が ``{`` で始まる）なら、対応する ``}`` の行。
        2. 定義行より深い字下げの行が続く間。そのあと定義と同じ字下げの ``end`` / ``}`` などの閉じ行があれば含める。
        3. 深い字下げの本体が無いキーワード形は、同じ字下げの閉じ行（``end`` など）を、次の定義の手前まで探す。
        どれも当たらなければ 1 行（開始行）。末尾の空行は含めない。
        """
        braced = self._brace_end(start)
        if braced is not None:
            return braced
        n = len(self)
        base = self.indents[start]
        assert base is not None
        last = start
        i = start + 1
        deeper = False
        while i < n:
            ind = self.indents[i]
            if ind is None:
                i += 1
                continue
            if ind > base:
                last = i
                deeper = True
                i += 1
                continue
            break
        if deeper:
            if i < n and self.indents[i] == base and _CLOSER.match(self.lines[i].strip()):
                last = i
            return last
        if keyword:
            j = start + 1
            while j < n:
                ind = self.indents[j]
                if ind is None:
                    j += 1
                    continue
                text = self.lines[j]
                if ind == base and _CLOSER.match(text.strip()):
                    return j
                if ind <= base and _KEYWORD_DEF.match(text):
                    break
                j += 1
        return last

    def _brace_end(self, start: int) -> int | None:
        n = len(self)
        depth = self.net(start)
        i = start
        if depth <= 0:
            if self.has_open(start):
                return None  # ``def f(a={}):`` のように定義行の中で閉じている → 字下げで見る
            nxt = start + 1
            while nxt < n and self.indents[nxt] is None:
                nxt += 1
            if nxt >= n or not self.lines[nxt].lstrip().startswith("{"):
                return None
            i = nxt
            depth = self.net(i)
            if depth <= 0:
                return i
        limit = min(n, i + 1 + _BRACE_SCAN_LINES_MAX)
        for j in range(i + 1, limit):
            depth += self.net(j)
            if depth <= 0:
                return j
        return None


def _definitions(doc: _Doc) -> Iterator[_Def]:
    for i, text in enumerate(doc.lines):
        if doc.indents[i] is None:
            continue
        m = _KEYWORD_DEF.match(text)
        if m is not None:
            name = m["name"]
            if name.upper() not in _RESERVED_NAMES:
                yield _Def(i, name, _KEYWORD_KINDS[m["kw"].lower()], m.start("name"), True)
            continue
        m = _COBOL_PARAGRAPH.match(text)
        if m is not None:
            yield _Def(i, m["name"], Kind.SECTION, m.start("name"), False)
            continue
        if doc.indents[i] == 0 and not text[:1].isspace():
            m = _ASSIGN.match(text)
            if m is not None:
                name = m["name"]
                if (kw := m["kw"]) is not None and kw.lower() == "rule":
                    kind = Kind.FUNCTION
                elif name.isupper():
                    kind = Kind.CONSTANT
                else:
                    kind = Kind.VARIABLE
                yield _Def(i, name, kind, m.start("name"), False)


def _clip(text: str, limit: int = SNIPPET_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _make_record(
    doc: _Doc,
    *,
    rel: RelPath,
    workspace_id: str,
    name: str,
    kind: Kind,
    start: int,
    end: int,
    qualified: str | None = None,
    col: int | None = None,
) -> SymbolRecord | None:
    """``start``〜``end``（0 始まり・含む）の定義から ``SymbolRecord``。URN が作れない名前は ``None``。"""
    try:
        urn = Urn(workspace_id, str(rel), TagSel(name, start + 1))
    except ValueError:
        return None
    text = doc.lines[start]
    if col is None:
        # ctags の項目は桁を持たない。単語として最初に現れる位置を採る（``def f`` の ``f`` が ``def`` に当たらないように）
        m = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) if re.match(r"\w", name) else None
        col = m.start() if m is not None else text.find(name)
    selection = Range(Position(start, col), Position(start, col + len(name))) if col >= 0 else None
    return SymbolRecord(
        urn=urn,
        path=rel,
        name=name,
        qualified_name=qualified or name,
        kind=kind,
        range=Range(Position(start, 0), Position(end, doc.index.line_length(end))),
        selection=selection,
        signature=_clip(text.strip()) or None,
        confidence=Confidence.HEURISTIC,
    )


# --- ブロック推定 ------------------------------------------------------------------------


def block_outline(source: bytes, *, rel: RelPath, workspace_id: str) -> tuple[list[SymbolRecord], Confidence]:
    """ctags なしのアウトライン（段 3 の ``block-heuristic``）。

    定義らしい行（``def|function|fn|sub|proc|procedure|class|module`` で始まる行、COBOL の段落名、字下げ 0 の
    ``name = …`` / ``rule NAME = …``）を開始行とし、終了行は括弧の対応・字下げの戻り・``end`` で推定する。
    取れたら ``heuristic``、何も取れなければ ``([], lexical_fallback)``。
    """
    doc = _Doc(source)
    records: list[SymbolRecord] = []
    for d in _definitions(doc):
        end = doc.block_end(d.line, keyword=d.keyword)
        rec = _make_record(
            doc,
            rel=rel,
            workspace_id=workspace_id,
            name=d.name,
            kind=d.kind,
            start=d.line,
            end=end,
            col=d.col,
        )
        if rec is not None:
            records.append(rec)
    return (records, Confidence.HEURISTIC) if records else ([], Confidence.LEXICAL_FALLBACK)


# --- ctags -------------------------------------------------------------------------------

_CTAGS_KINDS: Final = {
    "function": Kind.FUNCTION,
    "func": Kind.FUNCTION,
    "subroutine": Kind.FUNCTION,
    "procedure": Kind.FUNCTION,
    "routine": Kind.FUNCTION,
    "def": Kind.FUNCTION,
    "method": Kind.METHOD,
    "class": Kind.CLASS,
    "struct": Kind.STRUCT,
    "interface": Kind.INTERFACE,
    "enum": Kind.ENUM,
    "trait": Kind.TRAIT,
    "module": Kind.MODULE,
    "namespace": Kind.MODULE,
    "package": Kind.MODULE,
    "type": Kind.TYPE,
    "typedef": Kind.TYPE,
    "constant": Kind.CONSTANT,
    "const": Kind.CONSTANT,
    "macro": Kind.CONSTANT,
    "define": Kind.CONSTANT,
    "variable": Kind.VARIABLE,
    "var": Kind.VARIABLE,
    "field": Kind.FIELD,
    "member": Kind.FIELD,
    "property": Kind.PROPERTY,
}
_CTAGS_SKIP_KINDS: Final = frozenset({"parameter", "local", "localvar", "anonymous"})


def parse_ctags_json(
    stdout: bytes | str, *, source: bytes, rel: RelPath, workspace_id: str
) -> list[SymbolRecord]:
    """``ctags --output-format=json --fields=+ne`` の標準出力（1 行 1 JSON）から ``SymbolRecord`` を作る。

    ``_type`` が ``tag`` 以外（``ptag`` など）・壊れた行・``name`` や ``line`` の無い行・範囲外の行は読み飛ばす。
    ``end`` が無ければ ``block_outline`` と同じ規則で推定する。JSON の ``path`` は読まない（``rel`` を使う）。
    ``kind`` の未知の値は ``Section``。同じ ``(name, line)`` は 1 件にまとめる。
    """
    doc = _Doc(source)
    text = stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else stdout
    seen: set[tuple[str, int]] = set()
    records: list[SymbolRecord] = []
    for raw in text.splitlines():
        if len(records) >= _CTAGS_MAX_ENTRIES:
            break
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        rec = _tag_to_record(entry, doc, rel=rel, workspace_id=workspace_id, seen=seen)
        if rec is not None:
            records.append(rec)
    return records


def _tag_to_record(
    entry: Any, doc: _Doc, *, rel: RelPath, workspace_id: str, seen: set[tuple[str, int]]
) -> SymbolRecord | None:
    if not isinstance(entry, dict) or entry.get("_type") != "tag":
        return None
    name, line1 = entry.get("name"), entry.get("line")
    if not isinstance(name, str) or not name or isinstance(line1, bool) or not isinstance(line1, int):
        return None
    start = line1 - 1
    if not 0 <= start < len(doc) or doc.indents[start] is None or (name, line1) in seen:
        return None
    kind_name = str(entry.get("kind") or "").lower()
    if kind_name in _CTAGS_SKIP_KINDS:
        return None
    end1 = entry.get("end")
    if isinstance(end1, int) and not isinstance(end1, bool) and start <= end1 - 1 < len(doc):
        end = end1 - 1
        while end > start and doc.indents[end] is None:  # 末尾の空行は含めない
            end -= 1
    else:
        end = doc.block_end(start, keyword=False)
    scope = entry.get("scope")
    qualified = f"{scope}.{name}" if isinstance(scope, str) and scope else None
    rec = _make_record(
        doc,
        rel=rel,
        workspace_id=workspace_id,
        name=name,
        kind=_CTAGS_KINDS.get(kind_name, Kind.SECTION),
        start=start,
        end=end,
        qualified=qualified,
    )
    if rec is not None:
        seen.add((name, line1))
    return rec


async def find_ctags(
    registry: ProcessRegistry | None = None, *, timeout_ms: int = DEGRADED_GREP_MS
) -> list[str] | None:
    """PATH の Universal Ctags を探して、起動に使う argv の先頭（``[実行ファイルのパス]``）を返す。無ければ ``None``。

    ``ctags`` / ``universal-ctags`` / ``ctags.exe`` を順に ``--version`` で試し、出力に ``Universal Ctags`` を含むものだけ
    採る（macOS の BSD ctags などは除く）。起動は ``ProcessRegistry`` 経由。呼び出し側は結果を使い回す
    （設計書 §7.4.8: 起動時に一度だけ）。
    """
    owned = registry is None
    reg = registry or ProcessRegistry()
    try:
        tried: set[str] = set()
        for candidate in _CTAGS_CANDIDATES:
            exe = shutil.which(candidate)
            if exe is None or exe in tried:
                continue
            tried.add(exe)
            run = await _run(reg, [exe, "--version"], cwd=None, timeout_ms=timeout_ms)
            if run is not None and b"Universal Ctags" in run:
                return [exe]
        return None
    finally:
        if owned:
            await reg.terminate_all(SHUTDOWN_GRACE_MS)


async def _run(
    registry: ProcessRegistry, argv: list[str], *, cwd: Path | None, timeout_ms: int
) -> bytes | None:
    """``argv`` を台帳経由で走らせ、終了コード 0 なら標準出力を返す。起動失敗・タイムアウト・非 0 は ``None``。"""
    try:
        proc = await registry.spawn(argv, cwd=cwd, name="ctags")
    except (OSError, ValueError, RuntimeError) as exc:
        _log.warning("could not start %s: %s", argv[0], exc)
        return None
    try:
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
        except TimeoutError:
            _log.warning("%s timed out after %d ms", argv[0], timeout_ms)
            return None
        if proc.returncode != 0:
            _log.warning(
                "%s exited with %s: %s", argv[0], proc.returncode, err[:200].decode("utf-8", "replace")
            )
            return None
        return out
    finally:
        await registry.terminate(proc, SHUTDOWN_GRACE_MS)


async def _ctags_outline(
    source: bytes,
    *,
    rel: RelPath,
    workspace_id: str,
    ctags_cmd: list[str],
    registry: ProcessRegistry,
    timeout_ms: int,
    staging_root: Path | None = None,
) -> list[SymbolRecord]:
    """ctags で ``source`` のアウトラインを取る。失敗はすべて空（呼び出し側がブロック推定に降りる）。"""
    tmp = Path(tempfile.mkdtemp(prefix="lucianke-ctags-", dir=staging_root))
    try:
        # 言語の判定に拡張子・ファイル名を使うので、元のファイル名で一時ファイルを作る。``-f -`` が無いとカレントに ``tags`` を書く。
        name = PurePosixPath(str(rel)).name or "source"
        try:
            (tmp / name).write_bytes(source)
        except OSError as exc:
            _log.warning("could not stage %s for ctags: %s", rel, exc)
            return []
        argv = [*ctags_cmd, "--output-format=json", "--fields=+ne", "-f", "-", name]
        out = await _run(registry, argv, cwd=tmp, timeout_ms=timeout_ms)
        if out is None:
            return []
        return parse_ctags_json(out, source=source, rel=rel, workspace_id=workspace_id)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def outline(
    source: bytes,
    *,
    rel: RelPath,
    workspace_id: str,
    ctags_cmd: list[str] | None,
    registry: ProcessRegistry | None = None,
    timeout_ms: int = DEGRADED_GREP_MS,
    staging_root: Path | None = None,
) -> tuple[list[SymbolRecord], Confidence]:
    """Tier 4 ファイルのアウトライン。

    ``ctags_cmd``（``find_ctags()`` の結果）があれば ctags を走らせ、無い・失敗・タイムアウト・0 件なら
    ``block_outline`` に降りる。``registry`` が無ければ一時の台帳を作って、終わりに必ず落とす。
    ctags に渡す一時ファイルは ``staging_root`` の下の専用ディレクトリに置き、終わりに消す（既定は OS の一時ディレクトリ。
    テストは自分専用のディレクトリを渡して、同時に走る別のプロセスの一時物と混ざらないようにする）。
    取れたら ``(records, heuristic)``、何も取れなければ ``([], lexical_fallback)``。
    """
    if ctags_cmd:
        owned = registry is None
        reg = registry or ProcessRegistry()
        try:
            records = await _ctags_outline(
                source,
                rel=rel,
                workspace_id=workspace_id,
                ctags_cmd=ctags_cmd,
                registry=reg,
                timeout_ms=timeout_ms,
                staging_root=staging_root,
            )
        finally:
            if owned:
                await reg.terminate_all(SHUTDOWN_GRACE_MS)
        if records:
            return records, Confidence.HEURISTIC
    return block_outline(source, rel=rel, workspace_id=workspace_id)


# --- 字句検索（段 4） --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LexHit:
    """字句一致 1 件（1 行 1 件）。行は 0 始まり、列はコードポイント（ADR-14）。"""

    line: int
    col: int
    end_col: int
    line_text: str
    is_definition: bool
    snippet_start: int  # スニペットの最初の行（0 始まり）
    snippet: str  # 前後 ``context`` 行を ``\n`` でつないだもの（各行は ``SNIPPET_MAX_CHARS`` で切る）

    @property
    def snippet_end(self) -> int:
        return self.snippet_start + self.snippet.count("\n")

    @property
    def selector_line(self) -> int:
        """``lex:NAME@L<行>`` の行（1 始まり）。"""
        return self.line + 1


@dataclass(frozen=True, slots=True)
class LexScan:
    hits: list[LexHit]
    timed_out: bool = False  # ``budget_ms`` で打ち切った（hits は部分結果）
    truncated: bool = False  # ``GREP_MATCHES_MAX`` で打ち切った


_DEF_BEFORE = re.compile(r"\b(?:def|function|fn|sub|proc|procedure|class|module|rule)\s+$", re.IGNORECASE)
_DEF_AFTER = re.compile(r"\s*(?::|=(?![=~>])|\()")
_PARAGRAPH_AFTER = re.compile(r"\.\s*$")


def _word_pattern(name: str) -> re.Pattern[str]:
    """``\\b<name>\\b`` 相当。名前は ``re.escape`` する（``a.b`` や ``x(`` を安全に扱う）。

    境界は名前の端が単語文字のときだけ付ける（``x(`` の末尾に ``\\b`` を付けると ``x(1)`` に当たらないため）。
    """
    if not name:
        raise ValueError("name is empty")
    pattern = re.escape(name)
    if re.match(r"\w", name[0]):
        pattern = r"(?<!\w)" + pattern
    if re.match(r"\w", name[-1]):
        pattern += r"(?!\w)"
    if len(pattern) > REGEX_PATTERN_MAX_CHARS:
        raise ValueError(f"pattern is longer than {REGEX_PATTERN_MAX_CHARS} characters")
    return re.compile(pattern)


def _looks_like_definition(text: str, start: int, end: int) -> bool:
    if _DEF_BEFORE.search(text[:start]) or _DEF_AFTER.match(text, end):
        return True
    return (
        text[:start].strip() == "" and start == 7 and bool(_PARAGRAPH_AFTER.match(text, end))
    )  # COBOL の段落名


def _window_line(text: str, center: int | None = None) -> str:
    if len(text) <= SNIPPET_MAX_CHARS:
        return text
    if center is None:
        return _clip(text)
    lo = max(0, min(center - SNIPPET_MAX_CHARS // 2, len(text) - SNIPPET_MAX_CHARS))
    head = "…" if lo > 0 else ""
    tail = "…" if lo + SNIPPET_MAX_CHARS < len(text) else ""
    return head + text[lo : lo + SNIPPET_MAX_CHARS - len(head) - len(tail)] + tail


def lexical_scan(
    source: bytes,
    name: str,
    *,
    context: int = 2,
    budget_ms: int = DEGRADED_GREP_MS,
    max_hits: int = GREP_MATCHES_MAX,
) -> LexScan:
    """単語境界つきで ``name`` を探す。定義らしい一致を先頭に、同じ組の中は行の順。

    定義らしさ: ``<name>\\s*[:=(]``、``def|function|fn|sub|proc|procedure|class|module|rule`` の直後、COBOL の段落名。
    1 行に複数一致するときは定義らしい一致（なければ最初）を採る。``budget_ms`` を超えたら打ち切り、そこまでの結果を返す。
    ``name`` が空、またはパターンが ``REGEX_PATTERN_MAX_CHARS`` を超えるときは ``ValueError``。
    """
    if context < 0:
        raise ValueError("context must be >= 0")
    pattern = _word_pattern(name)
    needle = name.encode("utf-8", "surrogateescape")
    idx = LineIndex(source)
    total = idx.content_line_count
    deadline = time.monotonic() + budget_ms / 1000
    found: list[tuple[int, re.Match[str], str, bool]] = []
    timed_out = truncated = False
    for i in range(total):
        if i % 256 == 0 and time.monotonic() > deadline:
            timed_out = True
            break
        if needle not in idx.line_bytes(i):
            continue
        text = idx.line_text(i)
        best: re.Match[str] | None = None
        best_def = False
        for m in pattern.finditer(text):
            is_def = _looks_like_definition(text, m.start(), m.end())
            if best is None or (is_def and not best_def):
                best, best_def = m, is_def
            if best_def:
                break
        if best is None:
            continue
        if len(found) >= max_hits:
            truncated = True
            break
        found.append((i, best, text, best_def))
    found.sort(key=lambda t: (not t[3], t[0]))
    hits: list[LexHit] = []
    for i, m, text, is_def in found:
        lo, hi = max(0, i - context), min(total - 1, i + context)
        rows = [
            _window_line(text, m.start()) if j == i else _window_line(idx.line_text(j))
            for j in range(lo, hi + 1)
        ]
        hits.append(
            LexHit(
                line=i,
                col=m.start(),
                end_col=m.end(),
                line_text=_window_line(text, m.start()),
                is_definition=is_def,
                snippet_start=lo,
                snippet="\n".join(rows),
            )
        )
    return LexScan(hits, timed_out=timed_out, truncated=truncated)


def lexical_find(source: bytes, name: str, *, context: int = 2) -> list[LexHit]:
    """``lexical_scan`` の結果の一致だけ（打ち切りの旗が要らないとき）。"""
    return lexical_scan(source, name, context=context).hits


def block_window(source: bytes, line: int, *, radius: int = BLOCK_WINDOW_RADIUS) -> Range:
    """``line``（0 始まり）の前後 ``radius`` 行の窓。行全体を覆う範囲（終端は次行の 0 列）。

    ``radius`` は ``BLOCK_WINDOW_RADIUS``（15）を超えられない（最大 31 行。仕様書 §11 T-05 処理規約 5）。
    ファイルの先頭・末尾で切り詰める。``line`` が範囲外なら最寄りの行に寄せる。空のファイルは先頭の空の範囲。
    """
    if radius < 0:
        raise ValueError("radius must be >= 0")
    radius = min(radius, BLOCK_WINDOW_RADIUS)
    total = LineIndex(source).content_line_count
    if total == 0:
        return Range(Position(0, 0), Position(0, 0))
    line = min(max(line, 0), total - 1)
    start, end = max(0, line - radius), min(total - 1, line + radius)
    return Range.from_line_span_external(start + 1, end + 1)
