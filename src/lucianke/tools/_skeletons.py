"""T-02 の 1 ファイルの骨格を、ファイルの種類に応じて作る（仕様書 §11 T-02 処理規約 2〜7、BUG-0018）。

``file_skeleton`` が入口で、``revision`` の断面（``_rev.RevisionView``）から読む。種類の振り分け:

1. データ形式（JSON・JSONL・YAML・TOML・XML・``.ipynb``）→ ``_data_files.data_skeleton``（アダプタ。``structural``）。
2. コード（Python・TypeScript・JavaScript）→ 構文木（``syntax.skeleton.build_file_record``。``structural``）。
3. Markdown・reStructuredText → 見出しの入れ子を ``[Section]``（``structural``。Tier 3）。
4. それ以外（未知の言語・拡張子なし・文法を結線していない言語。Tier 4）→ ブロック推定（``syntax.heuristic.block_outline``）の
   ``[Function]`` / ``[Class]`` などを入れ子にして出す。取れれば ``heuristic``、何も取れなければ見出しだけの空の骨格で
   ``lexical_fallback``。降格の警告行（``! DEGRADED``）はパイプラインがレコードの水準から付ける（仕様書 §5.4.2）。

ctags は起動しない（子プロセスと PATH の探索を T-02 の呼び出しに持ち込まない。索引側は ``engine.indexer`` が使う）。
ブロック推定は ctags 互換の規則で定義らしい行を取り、終了行を括弧の対応と字下げから推定する。
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, RelPath, SymbolRecord, Tier
from lucianke.core.records import Record
from lucianke.syntax.adapters._common import SkelNode, build_record
from lucianke.syntax.heuristic import block_outline
from lucianke.syntax.skeleton import build_file_record
from lucianke.syntax.types import SkeletonOptions
from lucianke.tools._data_files import adapter_for, data_skeleton
from lucianke.tools._rev import LoadedFile, RevisionView, analyze_at, analyze_bytes, read_checked
from lucianke.tools._services import SYMBOL_LANGUAGES, FileAnalysis, ToolServices
from lucianke.workspace.languages import detect
from lucianke.workspace.paths import confine
from lucianke.workspace.types import Workspace

__all__ = ["ENGINE_ORDER", "FileSkeleton", "engine_name", "file_skeleton", "markup_nodes"]

Engine = Literal["tree-sitter", "data-adapter", "markup-outline", "block-heuristic"]
ENGINE_ORDER: tuple[Engine, ...] = ("tree-sitter", "data-adapter", "markup-outline", "block-heuristic")
_MARKUP = frozenset({"markdown", "rst"})


@dataclass(frozen=True, slots=True)
class FileSkeleton:
    """1 ファイルの骨格。``record`` は ``file-block``（``priority`` は呼び手が付けた順）。"""

    record: Record
    language_id: str
    tier: Tier
    engine: Engine


def engine_name(engines: Sequence[str] | set[str]) -> str:
    """``engine_used``: 使った経路を ``ENGINE_ORDER`` の順に ``+`` でつなぐ（コードだけなら ``tree-sitter``）。"""
    used = [e for e in ENGINE_ORDER if e in engines]
    return "+".join(used) if used else "tree-sitter"


# --- Markdown / reStructuredText の見出し ---------------------------------------------------


_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_RST_ADORN = re.compile(r"^([!-/:-@\[-`{-~])\1{2,}[ \t]*$")
_FRONT_MATTER_MAX = 200


def _front_matter_end(lines: Sequence[str]) -> int:
    """先頭の YAML front matter（``---`` 〜 ``---`` / ``...``）の次の行番号。無ければ 0。"""
    if not lines or lines[0].rstrip() != "---":
        return 0
    for i in range(1, min(len(lines), _FRONT_MATTER_MAX)):
        if lines[i].rstrip() in ("---", "..."):
            return i + 1
    return 0


def _markdown_headings(lines: Sequence[str]) -> list[tuple[int, str, int]]:
    """``(level, text, line)``（line は 0 始まり）。フェンスつきコードブロックと front matter の中は見ない。"""
    out: list[tuple[int, str, int]] = []
    fence: str | None = None
    i = _front_matter_end(lines)
    prev_text = False  # 直前が見出しでない非空の 1 行（セテクスト見出しの下線の相手）
    while i < len(lines):
        line = lines[i]
        m_fence = _FENCE.match(line)
        if fence is not None:
            if m_fence and m_fence.group(1)[0] == fence[0] and len(m_fence.group(1)) >= len(fence):
                fence = None
            i += 1
            prev_text = False
            continue
        if m_fence:
            fence = m_fence.group(1)
            prev_text = False
        elif (m := _ATX.match(line)) is not None:
            out.append((len(m.group(1)), (m.group(2) or "").strip(), i))
            prev_text = False
        elif prev_text and (m2 := _SETEXT.match(line)) is not None:
            out.append((1 if m2.group(1)[0] == "=" else 2, lines[i - 1].strip(), i - 1))
            prev_text = False
        else:
            prev_text = bool(line.strip()) and not line.lstrip().startswith(("|", ">", "<"))
        i += 1
    return out


def _rst_headings(lines: Sequence[str]) -> list[tuple[int, str, int]]:
    """reStructuredText の見出し（下線のみ、または上下線つき）。水準は記号と上線の有無の組が初めて現れた順。"""
    out: list[tuple[int, str, int]] = []
    styles: dict[tuple[str, bool], int] = {}
    i = 0
    n = len(lines)
    while i < n:
        a = _RST_ADORN.match(lines[i])
        if a is not None and i + 2 < n and lines[i + 1].strip() and not _RST_ADORN.match(lines[i + 1]):
            title = lines[i + 1].rstrip()
            under = _RST_ADORN.match(lines[i + 2])
            if (
                under is not None
                and under.group(1) == a.group(1)
                and len(lines[i + 2].rstrip()) >= len(title.strip())
            ):
                level = styles.setdefault((a.group(1), True), len(styles) + 1)
                out.append((level, title.strip(), i + 1))
                i += 3
                continue
        if (
            a is None
            and lines[i].strip()
            and i + 1 < n
            and (u := _RST_ADORN.match(lines[i + 1])) is not None
            and len(lines[i + 1].rstrip()) >= len(lines[i].strip())
        ):
            level = styles.setdefault((u.group(1), False), len(styles) + 1)
            out.append((level, lines[i].strip(), i))
            i += 2
            continue
        i += 1
    return out


def _sections(
    headings: Sequence[tuple[int, str, int]], total_lines: int, lines: Sequence[str]
) -> list[SkelNode]:
    """見出しの並びを入れ子の ``[Section]`` にする。節の終わりは次の同位以上の見出しの前（末尾の空行は含めない）。"""
    nodes: list[SkelNode] = []
    stack: list[tuple[int, SkelNode]] = []  # (level, node)
    for idx, (level, text, line) in enumerate(headings):
        end = total_lines - 1
        for lvl2, _t2, line2 in headings[idx + 1 :]:
            if lvl2 <= level:
                end = line2 - 1
                break
        while end > line and not lines[end].strip():
            end -= 1
        node = SkelNode(kind="Section", name=text or "(empty heading)", start=line, end=max(end, line))
        while stack and stack[-1][0] >= level:
            stack.pop()
        (stack[-1][1].children if stack else nodes).append(node)
        stack.append((level, node))
    return nodes


def markup_nodes(language_id: str, text: str) -> list[SkelNode]:
    """Markdown / reStructuredText の見出しの入れ子（``[Section]``）。"""
    lines = text.splitlines()
    headings = _rst_headings(lines) if language_id == "rst" else _markdown_headings(lines)
    return _sections(headings, len(lines), lines)


def _markup_skeleton(loaded: LoadedFile, opts: SkeletonOptions, *, priority: int) -> FileSkeleton:
    language_id = loaded.detected.language_id
    text = loaded.data.decode("utf-8", errors="replace")
    nodes = markup_nodes(language_id, text)
    notes = () if nodes else ("no headings found",)
    record = build_record(
        loaded.data,
        nodes,
        rel=str(loaded.rel),
        sha256=loaded.sha256,
        language=language_id,
        opts=opts,
        notes=notes,
    )
    return FileSkeleton(
        dataclasses.replace(record, priority=priority), language_id, Tier.T3, "markup-outline"
    )


# --- Tier 4（ブロック推定） ---------------------------------------------------------------


def _outline_nodes(symbols: Sequence[SymbolRecord]) -> list[SkelNode]:
    """平らな定義の一覧を、範囲の包含で入れ子にする。"""
    nodes: list[SkelNode] = []
    stack: list[SkelNode] = []
    for sym in sorted(symbols, key=lambda s: (s.range.start.line, -s.range.end.line)):
        node = SkelNode(
            kind=sym.kind.value, name=sym.name, start=sym.range.start.line, end=sym.range.end.line
        )
        while stack and not (stack[-1].start <= node.start and node.end <= stack[-1].end):
            stack.pop()
        (stack[-1].children if stack else nodes).append(node)
        stack.append(node)
    return nodes


def _retag(rec: Record, confidence: Confidence, tier: Tier, language: str) -> Record:
    """``build_record`` の結果（と、その縮退版）を、水準 ``confidence``・ティア ``tier`` に付け替える。"""
    inner = rec.shrink
    shrink: Callable[[int], Record | None] | None = None
    if inner is not None:

        def shrink(budget: int) -> Record | None:
            shrunk = inner(budget)
            return None if shrunk is None else _retag(shrunk, confidence, tier, language)

    inner_json = rec.shrink_json
    shrink_json: Callable[[int], Record | None] | None = None
    if inner_json is not None:

        def shrink_json(budget: int) -> Record | None:
            shrunk = inner_json(budget)
            return None if shrunk is None else _retag(shrunk, confidence, tier, language)

    data = {**rec.data, "tier": int(tier), "language": language, "confidence_level": confidence.value}
    return dataclasses.replace(rec, confidence=confidence, data=data, shrink=shrink, shrink_json=shrink_json)


def _heuristic_skeleton(
    ws: Workspace, loaded: LoadedFile, opts: SkeletonOptions, *, priority: int
) -> FileSkeleton:
    language_id = loaded.detected.language_id
    symbols, confidence = block_outline(
        loaded.data, rel=RelPath(str(loaded.rel)), workspace_id=ws.workspace_id
    )
    notes = () if symbols else ("no definitions found (lexical fallback)",)
    record = build_record(
        loaded.data,
        _outline_nodes(symbols),
        rel=str(loaded.rel),
        sha256=loaded.sha256,
        language=language_id,
        opts=opts,
        notes=notes,
    )
    record = _retag(dataclasses.replace(record, priority=priority), confidence, Tier.T4, language_id)
    return FileSkeleton(record, language_id, Tier.T4, "block-heuristic")


# --- 入口 --------------------------------------------------------------------------------


async def file_skeleton(
    svc: ToolServices,
    ws: Workspace,
    view: RevisionView,
    user_path: str,
    opts: SkeletonOptions,
    *,
    priority: int,
    param: str = "paths",
) -> FileSkeleton:
    """``user_path`` の骨格。失敗は ``LuciankeError``（項目単位。呼び手が ``failure_record`` にする）。"""
    rel = confine(ws, user_path, param=param)
    if adapter_for(str(rel)) is not None:
        data = await data_skeleton(ws, user_path, opts, priority=priority, param=param, view=view)
        if data is None:  # adapter_for が答えたので通常は来ない
            raise LuciankeError(ErrorCode.E_INTERNAL, f"{rel}: data skeleton unavailable")
        return FileSkeleton(data.record, data.language_id, data.tier, "data-adapter")
    if detect(rel, b"").language_id in SYMBOL_LANGUAGES:  # 名前で決まるコード: 読み込みは analyze_at に任せる
        analysis = await analyze_at(svc, ws, view, user_path, param=param)
        return _code_skeleton(analysis, opts, priority)
    loaded = await read_checked(view, ws, user_path, param=param)
    language_id = loaded.detected.language_id
    if language_id in SYMBOL_LANGUAGES:  # シバンなどで中身から決まったコード
        return _code_skeleton(await analyze_bytes(svc, ws, loaded), opts, priority)
    if language_id in _MARKUP:
        return _markup_skeleton(loaded, opts, priority=priority)
    return _heuristic_skeleton(ws, loaded, opts, priority=priority)


def _code_skeleton(analysis: FileAnalysis, opts: SkeletonOptions, priority: int) -> FileSkeleton:
    record = build_file_record(
        analysis.parsed, list(analysis.symbols), rel=str(analysis.rel), opts=opts, priority=priority
    )
    return FileSkeleton(record, analysis.language_id, analysis.tier, "tree-sitter")
