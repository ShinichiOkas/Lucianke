"""T-11 ``rename_symbol`` の降格経路の計画（仕様書 T-11「精度と降格」、§8.4、§10.5）。

LSP が使えないときの 2 つの経路。どちらも ``edit.transaction.PlanBuilder`` が返す ``Plan`` を作るだけで、ロック・前提検証・
降格インターロック・ジャーナル・原子的書き込みはトランザクションの仕事。

* ``lexical_plan``（``heuristic`` / ``lexical_fallback``）: 文法の無いファイル。単語境界 ``(?<!\\w)旧名(?!\\w)`` の字句置換。
  対象は **同じ拡張子（同じ言語）のファイル**（``scope_paths`` 指定時はその配下）。コメント・文字列内の一致も置換対象になる
  （応答の ``accuracy_warning`` に明記する）。書き換えるファイルのいずれかに ``new_name`` が単語として既にあれば、
  ``allow_heuristic_mutation: true`` のとき ``E_MUTATION_BLOCKED``（許可が無いときは強制 ``dry_run`` なので警告だけ）。
  実書き込みには ``expected_file_sha256s`` に対象ファイルを入れること（T-10 の ``lexical_fallback`` と同じ規則）。
* ``structural_plan``（``structural``）: 文法のあるファイルで LSP が使えないとき、参照が 1 つの関数に閉じるローカル変数・
  引数に限る同一ファイル内の改名（``_t11_scope``）。対象にならなければ ``None``（呼び手が元の ``E_LSP_UNAVAILABLE`` を返す）。

このモジュールは ``t11_rename_symbol`` を import しない（``t11`` が import する）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import MAX_FILE_BYTES
from lucianke.core.model import Position, Range, RelPath, TargetRef
from lucianke.core.textpos import LineIndex
from lucianke.core.urn import CodeSel, parse
from lucianke.edit.plan import Hunk, Plan
from lucianke.edit.transaction import PlanContext
from lucianke.tools import _t11_scope
from lucianke.workspace.languages import HEAD_BYTES, detect
from lucianke.workspace.paths import confine, is_excluded, to_abs
from lucianke.workspace.types import Workspace

__all__ = [
    "LEXICAL_ENGINE",
    "LEXICAL_WARNING_EXTRA",
    "DegradedPlan",
    "check_lexical_identifier",
    "lexical_plan",
    "structural_plan",
]

LEXICAL_ENGINE = "lexical-replace"
LEXICAL_WARNING_EXTRA = (
    "単語境界の字句置換のため、コメント・文字列内の一致と、同名の別スコープ・別シンボルの識別子も置換対象になります。"
    "dry_run の差分を確かめてから `allow_heuristic_mutation: true` で書き込んでください。"
)
_NEW_NAME = re.compile(r"[^\W\d]\w*")
_WORD = re.compile(r"\w+")
_DEFINITION_HINT = re.compile(
    r"\b(?:def|function|fn|func|sub|proc|procedure|class|struct|enum|interface|module|trait|type)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class DegradedPlan:
    """降格経路の計画と、応答に使う付随情報。``sources`` は計画の元にした各ファイルの内容（hunk のあるファイル）。"""

    plan: Plan
    sources: dict[RelPath, bytes]
    name_pos: Position
    old_name: str
    warnings: list[str] = field(default_factory=list)


def _invalid(message: str, **details: object) -> LuciankeError:
    return LuciankeError(ErrorCode.E_INVALID_ARGUMENT, message, details=dict(details))


def _word_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")


def check_lexical_identifier(name: str) -> None:
    """文法が無い言語の ``new_name`` の検査（空・数字始まり・空白や記号を含むものは ``E_INVALID_ARGUMENT``）。"""
    if not _NEW_NAME.fullmatch(name):
        raise _invalid(
            f"new_name is not an identifier: {name!r}", param="new_name", reason="not_an_identifier"
        )


# --- 改名する名前の位置 -----------------------------------------------------------------------


def _locate_name(ctx: PlanContext, target: TargetRef) -> tuple[str, Position | None]:
    """旧名と、その位置（``path`` + ``line`` + ``character`` の指定のときだけ。定義の位置を知る手掛かり）。"""
    index = LineIndex(ctx.source)
    if target.line is not None:
        if target.character is None:
            raise _invalid(
                "the lexical rename needs the identifier: give character with line, or symbol_name",
                param="target",
                reason="identifier_required",
            )
        if target.line >= index.line_count:
            raise LuciankeError(
                ErrorCode.E_TARGET_NOT_FOUND,
                f"no identifier at {ctx.path}:{target.line + 1}:{target.character + 1}",
                details={"path": str(ctx.path), "line": target.line + 1},
            )
        text = index.line_text(target.line)
        for m in _WORD.finditer(text):
            if m.start() <= target.character <= m.end():
                return m.group(), Position(target.line, m.start())
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"no identifier at {ctx.path}:{target.line + 1}:{target.character + 1}",
            details={"path": str(ctx.path), "line": target.line + 1, "character": target.character + 1},
        )
    name: str | None = target.symbol_name
    if name is None and target.urn is not None:
        try:
            selector = parse(target.urn).selector
        except ValueError as exc:
            raise _invalid(f"target.urn: {exc}", param="target.urn") from None
        if isinstance(selector, CodeSel):
            name = selector.qualified
    if not name:
        raise _invalid("target must name the identifier to rename", param="target")
    last = re.split(r"[.:#/]+", name)[-1]
    if not _NEW_NAME.fullmatch(last):
        raise _invalid(f"target: not an identifier: {last!r}", param="target")
    return last, None


# --- 候補ファイルの列挙 ---------------------------------------------------------------------


def _ext(name: str) -> str:
    low = name.casefold()
    idx = low.rfind(".")
    return low[idx:] if idx > 0 else ""


def _ignore_filter(ws: Workspace) -> object | None:
    try:
        from dulwich.ignore import IgnoreFilter, IgnoreFilterManager

        globals_: list[object] = []
        exclude = ws.root / ".git" / "info" / "exclude"
        if exclude.is_file():
            try:
                globals_.append(IgnoreFilter.from_path(str(exclude)))
            except OSError:
                pass
        return IgnoreFilterManager(str(ws.root), globals_, ws.case_insensitive)
    except Exception:
        return None


def _ignored(ignore: object | None, path: str) -> bool:
    if ignore is None:
        return False
    try:
        return bool(ignore.is_ignored(path))  # type: ignore[attr-defined]
    except Exception:
        return False


def _in_scope(rel: str, scope: Sequence[str] | None) -> bool:
    if not scope:
        return True
    return any(rel == s or rel.startswith(s + "/") for s in scope)


def _may_contain_scope(directory: str, scope: Sequence[str] | None) -> bool:
    """``directory`` の配下に ``scope`` に入るファイルがあり得るか（枝刈り）。"""
    if not scope:
        return True
    return any(
        directory == s or directory.startswith(s + "/") or s.startswith(directory + "/") for s in scope
    )


def _candidates(ws: Workspace, ext: str, scope: Sequence[str] | None) -> list[RelPath]:
    """``ws`` の中の、拡張子が ``ext`` のファイル（昇順）。``.git``・シンボリックリンク・除外グロブ・``.gitignore`` を除く。"""
    ignore = _ignore_filter(ws)
    root = str(ws.root)
    out: list[RelPath] = []
    stack: list[str] = [""]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(os.path.join(root, directory) if directory else root) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name == ".git":
                continue
            rel = f"{directory}/{name}" if directory else name
            try:
                if entry.is_symlink() or entry.is_junction():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if (
                    is_excluded(ws, RelPath(rel + "/."))
                    or _ignored(ignore, rel + "/")
                    or not _may_contain_scope(rel, scope)
                ):
                    continue
                stack.append(rel)
            elif is_file and _ext(name) == ext and _in_scope(rel, scope):
                if is_excluded(ws, RelPath(rel)) or _ignored(ignore, rel):
                    continue
                out.append(RelPath(rel))
    out.sort()
    return out


# --- 字句置換 ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _Scan:
    hunks: list[Hunk]
    first_match: Position | None
    definition: Position | None
    new_name_line: int | None  # new_name が単語として既にある最初の行（0 始まり）。無ければ None


def _scan(rel: RelPath, data: bytes, old: re.Pattern[str], new: re.Pattern[str], new_name: str) -> _Scan:
    index = LineIndex(data)
    hunks: list[Hunk] = []
    first: Position | None = None
    definition: Position | None = None
    new_line: int | None = None
    for line in range(index.line_count):
        text = index.line_text(line)
        if new_line is None and new.search(text):
            new_line = line
        for m in old.finditer(text):
            pos = Position(line, m.start())
            hunks.append(Hunk(rel, Range(pos, Position(line, m.end())), new_name, label="rename"))
            if first is None:
                first = pos
            if definition is None and _DEFINITION_HINT.search(text[: m.start()]):
                definition = pos
    return _Scan(hunks, first, definition, new_line)


def _read_candidate(ws: Workspace, rel: RelPath, language_id: str) -> tuple[bytes, str] | str:
    """``(内容, sha256)``。読まない理由があれば理由の文字列（``binary`` / ``language`` / ``too_large`` / ``non_utf8`` / ``unreadable``）。"""
    path = to_abs(ws, rel)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return "too_large"
        data = path.read_bytes()
    except OSError:
        return "unreadable"
    det = detect(rel, data[:HEAD_BYTES])
    if not det.index_content:
        return "binary"
    if det.language_id != language_id:
        return "language"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "non_utf8"
    return data, hashlib.sha256(data).hexdigest()


def _scope_of(ws: Workspace, scope_paths: Sequence[str] | None) -> list[str] | None:
    if not scope_paths:
        return None
    return [str(confine(ws, p, param="scope_paths")) for p in scope_paths]


def _definition_expected(ws: Workspace, expected: dict[str, str] | None, target_path: RelPath) -> bool:
    for key in expected or {}:
        if confine(ws, key, param="expected_file_sha256s") == target_path:
            return True
    return False


async def lexical_plan(
    ctx: PlanContext,
    *,
    target: TargetRef,
    new_name: str,
    scope_paths: Sequence[str] | None,
    allow_heuristic_mutation: bool,
    dry_run: bool,
    expected_file_sha256s: dict[str, str] | None,
) -> DegradedPlan:
    """単語境界の字句置換の計画（モジュールの docstring）。"""
    ws = ctx.workspace
    old_name, located = _locate_name(ctx, target)
    if old_name == new_name:
        raise _invalid("new_name is the same as the current name", param="new_name", reason="same_name")
    if (
        allow_heuristic_mutation
        and not dry_run
        and not _definition_expected(ws, expected_file_sha256s, ctx.path)
    ):
        raise _invalid(
            "writing a lexical rename needs expected_file_sha256s with the file that holds the definition",
            param="expected_file_sha256s",
            reason="required_for_lexical_fallback",
            path=str(ctx.path),
        )
    scope = _scope_of(ws, scope_paths)
    warnings: list[str] = []
    if scope is not None and not _in_scope(str(ctx.path), scope):
        raise _invalid(
            f"scope_paths does not include the file that holds the symbol: {ctx.path}",
            param="scope_paths",
            reason="target_outside_scope",
            path=str(ctx.path),
        )
    old_re, new_re = _word_pattern(old_name), _word_pattern(new_name)
    ext = _ext(str(ctx.path).rsplit("/", 1)[-1])
    paths = await asyncio.to_thread(_candidates, ws, ext, scope)
    if ctx.path not in paths:
        paths.append(ctx.path)  # 無視設定に入っていても、対象のファイルは含める
    skipped: dict[str, int] = {}
    hunks: list[Hunk] = []
    sources: dict[RelPath, bytes] = {}
    shas: dict[RelPath, str] = {}
    name_pos: Position | None = None
    first_in_target: Position | None = None
    conflicts: list[tuple[RelPath, int]] = []

    def work(rel: RelPath) -> tuple[RelPath, bytes | None, str | None, _Scan | None, str | None]:
        if rel == ctx.path:
            data, sha = ctx.source, ctx.sha256  # 解決段で読んだ内容を使う（読み直しの競合を作らない）
        else:
            got = _read_candidate(ws, rel, ctx.language_id)
            if isinstance(got, str):
                return rel, None, None, None, got
            data, sha = got
        scan = _scan(rel, data, old_re, new_re, new_name)
        return rel, data, sha, scan, None

    for rel, data, sha, scan, why in await asyncio.gather(*(asyncio.to_thread(work, p) for p in paths)):
        if why is not None:
            if why != "language":
                skipped[why] = skipped.get(why, 0) + 1
            continue
        assert data is not None and sha is not None and scan is not None
        if not scan.hunks:
            continue
        hunks.extend(scan.hunks)
        sources[rel] = data
        shas[rel] = sha
        if scan.new_name_line is not None:
            conflicts.append((rel, scan.new_name_line))
        if rel == ctx.path:
            first_in_target = scan.first_match
            name_pos = located if located is not None else (scan.definition or scan.first_match)
    if ctx.path not in sources or name_pos is None:
        raise LuciankeError(
            ErrorCode.E_TARGET_NOT_FOUND,
            f"{old_name!r} does not appear as a word in {ctx.path}",
            details={"path": str(ctx.path), "symbol_name": old_name},
        )
    del first_in_target
    if conflicts:
        path, line = conflicts[0]
        message = (
            f"new_name {new_name!r} already appears as a word in {path}:{line + 1} "
            f"(and {len(conflicts) - 1} other file(s)); after the replacement it could not be told apart"
            if len(conflicts) > 1
            else f"new_name {new_name!r} already appears as a word in {path}:{line + 1}; "
            "after the replacement it could not be told apart"
        )
        if allow_heuristic_mutation:
            raise LuciankeError(
                ErrorCode.E_MUTATION_BLOCKED,
                message,
                details={
                    "reason": "new_name_already_present",
                    "path": str(path),
                    "line": line + 1,
                    "new_name": new_name,
                    "files": [str(p) for p, _ in conflicts[:10]],
                },
            )
        warnings.append(
            f"{message}. A write with allow_heuristic_mutation: true would be E_MUTATION_BLOCKED."
        )
    if skipped:
        parts = ", ".join(f"{n} {why}" for why, n in sorted(skipped.items()))
        warnings.append(f"skipped files with the same extension: {parts}")
    if scope is None:
        warnings.append(
            f"every file with the extension {ext or '(none)'} in the workspace is searched; "
            "pass scope_paths to limit the rename"
        )
    warnings.append("urn_map is empty: the lexical rename does not resolve symbols")
    plan = Plan(
        hunks=tuple(hunks),
        touched_paths=tuple(sources),
        resolved_shas=shas,
        target_urns=(),
        expects_syntax=False,
    )
    return DegradedPlan(plan, sources, name_pos, old_name, warnings)


# --- 構文木のスコープ解析（structural） --------------------------------------------------------


async def structural_plan(ctx: PlanContext, *, target: TargetRef, new_name: str) -> DegradedPlan | None:
    """参照が 1 つの関数に閉じるローカル変数・引数の同一ファイル内改名の計画。対象にならなければ ``None``。"""
    if ctx.parsed is None or target.line is None or target.character is None:
        return None
    found = await asyncio.to_thread(
        _t11_scope.rename_local,
        ctx.parsed,
        ctx.source,
        ctx.language_id,
        target.line,
        target.character,
        new_name,
    )
    if found is None:
        return None
    old_name, name_pos, ranges = found
    hunks = tuple(Hunk(ctx.path, rng, new_name, label="rename") for rng in ranges)
    plan = Plan(
        hunks=hunks,
        touched_paths=(ctx.path,),
        resolved_shas={ctx.path: ctx.sha256},
        target_urns=(),
        expects_syntax=True,
    )
    return DegradedPlan(
        plan,
        {ctx.path: ctx.source},
        name_pos,
        old_name,
        ["structural rename: only the references inside the one function that binds the name were renamed"],
    )
