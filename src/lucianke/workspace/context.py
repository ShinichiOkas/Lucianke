"""ワークスペースの登録・解決（設計書 §7.3.1、§9.3、仕様書 §3.1 FR-WS-1/2、§7.2、§11 T-00 処理規約 2・3・7）。

``WorkspaceContext`` は登録済みワークスペースの表とアクティブなものを持つ。``Workspace`` は不変なので、更新は
差し替え（``dataclasses.replace``）で行う。同期のメソッドだけで、await を挟まない（イベントループ上の
呼び出しは互いに割り込まない）。

* ``register_roots``: 実体パス化・ID 割り当て（衝突は ``-2``, ``-3``）・サブプロジェクト検出・Python 環境検出・
  ``.lucianke.toml`` の適用までを **全部成功するか、何も変えないか** で行う。
* ``resolve``: ``workspace_id`` 省略時の解決（①パスを含む登録済みルート ②アクティブ ③上方向の最寄りのマーカー）。
* 警告（自動登録、無視した設定など）は ``warnings`` に溜まり、``take_warnings`` で取り出す。T-00 ``status`` と
  各ツールの ``warnings[]`` に載せるのは呼び出し側の仕事。
"""

from __future__ import annotations

import builtins
import dataclasses
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, workspace_id_from_dirname
from lucianke.workspace import settings_file
from lucianke.workspace.paths import confine
from lucianke.workspace.pyenv import detect_python_env
from lucianke.workspace.types import SubProject, Workspace

__all__ = [
    "SUBPROJECT_MARKERS",
    "SUBPROJECT_MAX_DEPTH",
    "WorkspaceContext",
    "detect_subprojects",
]

SUBPROJECT_MARKERS: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "package.json",
    "Cargo.toml",
    "go.mod",
    ".git",
)
"""サブプロジェクトの境界を示すマーカー（仕様書 §11 T-00 処理規約 2）。並びは出力の安定した順序にも使う。"""

SUBPROJECT_MAX_DEPTH = 6
"""ルートから下方向に走査する深さの上限（設計書 §7.3.1 手順 3）。ルート自身が 0。"""

_SKIP_DIRS = frozenset({".venv", "node_modules", ".git"})  # 中には潜らない（大文字小文字は区別しない）
_MAX_SCAN_DIRS = 20_000  # 巨大なツリーでの暴走防止。超えたら打ち切る
_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_CASE_FOLDING_PLATFORMS = ("win32", "darwin")


# --- 小道具 -------------------------------------------------------------------------------


def _root_key(path: str | os.PathLike[str]) -> str:
    """ルートの同一性を比べるキー。大文字小文字を区別しない OS では畳む。"""
    key = os.path.normcase(os.fspath(path))
    return key.casefold() if sys.platform in _CASE_FOLDING_PLATFORMS else key


def _probe_case_insensitive(root: Path) -> bool:
    """``root`` のあるファイルシステムが大文字小文字を区別しないか。ディレクトリ名の大小を入れ替えて引いてみる。"""
    swapped = root.name.swapcase()
    if swapped != root.name:
        try:
            return os.path.samefile(root.parent / swapped, root)
        except OSError:
            return False
    return sys.platform in _CASE_FOLDING_PLATFORMS  # 名前に英字が無いときは OS の既定


def _is_link(entry: os.DirEntry[str]) -> bool:
    return entry.is_symlink() or entry.is_junction()


def _not_configured(known: Sequence[Workspace], message: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_WORKSPACE_NOT_CONFIGURED,
        message,
        details={
            "known_workspaces": [
                {"workspace_id": ws.workspace_id, "root_path": ws.root.as_posix()} for ws in known
            ]
        },
    )


def _file_uri_to_path(uri: str) -> str | None:
    """MCP ``roots/list`` の ``file://`` URI を絶対パスにする。``file`` 以外・解釈できない URI は None。"""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme.lower() != "file":
        return None
    path = unquote(parts.path)
    host = parts.netloc
    if host and host.lower() != "localhost":
        if sys.platform != "win32":
            return None
        path = f"//{host}{path}"  # UNC
    elif sys.platform == "win32" and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path or None


# --- サブプロジェクト ---------------------------------------------------------------------


def detect_subprojects(root: Path | str, *, max_depth: int = SUBPROJECT_MAX_DEPTH) -> tuple[SubProject, ...]:
    """``root`` 直下から深さ ``max_depth`` までマーカーを走査する（設計書 §7.3.1 手順 3）。

    マーカー 1 つにつき ``SubProject(path, marker)`` を 1 件返す（同じディレクトリに複数あれば複数件）。
    ``path`` はルート相対で ``/`` 区切り、ルート自身は ``"."``。``.venv``・``node_modules``・``.git`` の中、
    シンボリックリンク・ジャンクションの先には潜らない。並びはパス順（ルートが先頭）、同じパスではマーカーの定義順。
    """
    order = {name: i for i, name in enumerate(SUBPROJECT_MARKERS)}
    found: list[tuple[str, int, str]] = []
    stack: list[tuple[str, str, int]] = [(os.fspath(root), ".", 0)]
    scanned = 0
    while stack and scanned < _MAX_SCAN_DIRS:
        directory, rel, depth = stack.pop()
        scanned += 1
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if entry.name in order:
                found.append((rel, order[entry.name], entry.name))
        if depth >= max_depth:
            continue
        for entry in entries:
            if entry.name.casefold() in _SKIP_DIRS:
                continue
            try:
                if _is_link(entry) or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            child = entry.name if rel == "." else f"{rel}/{entry.name}"
            stack.append((entry.path, child, depth + 1))
    found.sort(key=lambda item: (item[0] != ".", item[0], item[1]))
    return tuple(SubProject(path=rel, marker=marker) for rel, _, marker in found)


def _nearest_marker_dir(start: Path) -> tuple[Path, str] | None:
    """``start``（ファイルでもよい。存在しなくてもよい）から上方向に、マーカーを持つ最初のディレクトリ。"""
    current = start
    while not current.is_dir():
        if current.parent == current:
            return None
        current = current.parent
    for directory in (current, *current.parents):
        for marker in SUBPROJECT_MARKERS:
            try:
                if (directory / marker).exists():
                    return directory, marker
            except OSError:
                continue
    return None


# --- WorkspaceContext ---------------------------------------------------------------------


class WorkspaceContext:
    """登録済みワークスペースの表とアクティブなもの。

    Args:
        cwd: ``from_mcp_roots`` で ``roots/list`` が取れなかったときと、相対パスの基準（アクティブが無いとき）に使う。
        env: 環境変数（既定は ``os.environ``）。``CONDA_PREFIX``・``PATH``・``LUCIANKE_*`` を読む。
        known_roots: 前回までに割り当てた ``{ルートのパス: workspace_id}``（``workspaces`` テーブルの内容）。
            同じルートには同じ ID を再利用し（URN をセッションをまたいで安定させる。設計書 §7.3.1 手順 2）、
            そこにある ID は別のルートに割り当てない。
    """

    def __init__(
        self,
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        known_roots: Mapping[str, str] | None = None,
    ) -> None:
        self._cwd = cwd
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._table: dict[str, Workspace] = {}  # workspace_id -> Workspace（登録順）
        self._active: str | None = None
        self._file_settings: dict[str, dict[str, Any]] = {}  # .lucianke.toml の内容（正規化済み）
        self._tool_args: dict[str, dict[str, Any]] = {}  # ツールでの明示（set_excludes など）
        self._ids: dict[str, str] = {
            _root_key(root): ws_id for root, ws_id in (known_roots or {}).items() if _ID_RE.fullmatch(ws_id)
        }
        self.warnings: list[str] = []

    # --- 参照 ---------------------------------------------------------------------------

    def list(self) -> builtins.list[Workspace]:
        return list(self._table.values())

    def get(self, workspace_id: str) -> Workspace:
        try:
            return self._table[workspace_id]
        except KeyError:
            raise _not_configured(self.list(), f"workspace_id is not registered: {workspace_id!r}") from None

    def active(self) -> Workspace:
        if self._active is None:
            raise _not_configured(self.list(), "no workspace is registered; call configure_workspace first")
        return self._table[self._active]

    def set_active(self, workspace_id: str) -> None:
        self.get(workspace_id)
        self._active = workspace_id

    def settings(self, workspace_id: str) -> Mapping[str, Any]:
        """合成済みの設定（ツール > 環境変数 > ``.lucianke.toml`` > 既定。``settings_file.merge`` の形）。"""
        self.get(workspace_id)
        merged = settings_file.merge(
            self._tool_args.get(workspace_id), self._env, self._file_settings.get(workspace_id)
        )
        return MappingProxyType(merged)

    def take_warnings(self) -> builtins.list[str]:
        """溜まった警告を取り出して空にする。"""
        taken, self.warnings = self.warnings, []
        return taken

    # --- 登録 ---------------------------------------------------------------------------

    def register_roots(self, paths: Sequence[str], *, replace: bool = False) -> builtins.list[Workspace]:
        """ルートを登録し、登録したワークスペースを ``paths`` の順（重複は除く）で返す。最初のものがアクティブになる。

        存在するディレクトリでないパスが 1 つでもあれば、何も変えずに ``E_INVALID_ARGUMENT``。
        既に登録済みのルートはそのまま（同じ ID・同じオブジェクト）。``replace=True`` のときは、``paths`` に
        無いワークスペースを登録から外す（ID は覚えておき、再登録で同じ ID を返す）。
        """
        if isinstance(paths, str) or not isinstance(paths, Sequence) or not paths:
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT,
                "root_paths must be a non-empty array of directory paths",
                details={"param": "root_paths", "reason": "empty"},
            )
        roots: list[Path] = []
        bad: list[str] = []
        for raw in paths:
            real = self._existing_dir(raw)
            if real is None:
                bad.append(raw if isinstance(raw, str) else repr(raw))
            elif all(_root_key(real) != _root_key(r) for r in roots):
                roots.append(real)
        if bad:
            shown = ", ".join(repr(p) for p in bad[:10])
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT,
                f"root_paths contains paths that are not existing directories: {shown}",
                details={"param": "root_paths", "reason": "not_a_directory", "paths": bad[:10]},
            )

        # ここから先は例外を投げない: 検出系は失敗を警告に変える。計算が済んでから表に反映する。
        table = {} if replace else dict(self._table)
        by_key = {_root_key(ws.root): ws for ws in self._table.values()}
        ids = dict(self._ids)
        file_settings = {} if replace else dict(self._file_settings)
        tool_args = {} if replace else dict(self._tool_args)
        warnings: list[str] = list(settings_file.env_settings(self._env).warnings)
        result: list[Workspace] = []
        for root in roots:
            key = _root_key(root)
            if (existing := by_key.get(key)) is not None:
                table[existing.workspace_id] = existing
                result.append(existing)
                continue
            ws_id = self._assign_id(root, key, ids, table)
            ids[key] = ws_id
            ws, loaded = self._build(ws_id, root, warnings)
            table[ws_id] = ws
            file_settings[ws_id] = loaded
            result.append(ws)

        self._table = table
        self._ids = ids
        self._file_settings = {i: v for i, v in file_settings.items() if i in table}
        self._tool_args = {i: v for i, v in tool_args.items() if i in table}
        self._active = result[0].workspace_id
        self.warnings.extend(warnings)
        return result

    def from_mcp_roots(
        self, uris: Sequence[str] | None, *, replace: bool = False
    ) -> builtins.list[Workspace]:
        """MCP ``roots/list`` の ``file://`` URI を登録する。取れなければ（空・None・全部使えない）cwd を登録する。

        ``set_roots`` と違い、使えない URI（``file`` 以外・存在しない）は警告にして飛ばす。
        """
        paths: list[str] = []
        for uri in uris or ():
            path = _file_uri_to_path(uri) if isinstance(uri, str) else None
            if path is not None and os.path.isdir(path):
                paths.append(path)
            else:
                self.warnings.append(f"roots/list entry ignored (not an existing local directory): {uri!r}")
        if not paths:
            if uris:
                self.warnings.append("roots/list had no usable root; using the current directory")
            paths = [os.fspath(self._cwd_path())]
        return self.register_roots(paths, replace=replace)

    def _cwd_path(self) -> Path:
        return Path(self._cwd) if self._cwd is not None else Path.cwd()

    def _base_dir(self) -> str:
        """相対パスの基準: アクティブなワークスペースのルート、無ければ cwd。"""
        return os.fspath(self._table[self._active].root) if self._active else os.fspath(self._cwd_path())

    def _absolute(self, raw: str) -> str:
        return raw if os.path.isabs(raw) else os.path.join(self._base_dir(), raw)

    def _existing_dir(self, raw: object) -> Path | None:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            return None
        try:
            real = os.path.realpath(self._absolute(raw))
            return Path(real) if os.path.isdir(real) else None
        except (OSError, ValueError):
            return None

    def _assign_id(self, root: Path, key: str, ids: dict[str, str], table: dict[str, Workspace]) -> str:
        """前回の ID があれば再利用。無ければディレクトリ名から作り、衝突したら ``-2``, ``-3`` を付す。"""
        remembered = ids.get(key)
        if remembered is not None and remembered not in table:
            return remembered
        taken = set(ids.values()) | set(table) | {EXTERNAL_WORKSPACE_ID}
        base = workspace_id_from_dirname(root.name)
        candidate, n = base, 1
        while candidate in taken:
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def _build(
        self, ws_id: str, root: Path, warnings: builtins.list[str]
    ) -> tuple[Workspace, dict[str, Any]]:
        loaded = settings_file.load_settings(root)
        warnings.extend(f"{ws_id}: {w}" for w in loaded.warnings)
        merged = settings_file.merge(None, self._env, loaded.values)
        explicit = merged.get("python_interpreter")
        try:
            python_env = detect_python_env(root, explicit, env=self._env)
        except LuciankeError:
            warnings.append(
                f"{ws_id}: python_interpreter {explicit!r} ({settings_file.SETTINGS_FILENAME}) is not executable; "
                "using auto-detection"
            )
            python_env = detect_python_env(root, None, env=self._env)
        ws = Workspace(
            workspace_id=ws_id,
            root=root,
            exclude_globs=merged["exclude_globs"],
            python_env=python_env,
            sub_projects=detect_subprojects(root),
            case_insensitive=_probe_case_insensitive(root),
        )
        return ws, dict(loaded.values)

    # --- 解決 ---------------------------------------------------------------------------

    def resolve(self, workspace_id: str | None = None, *, path_hint: str | None = None) -> Workspace:
        """``workspace_id`` 省略時の解決（仕様書 §11 T-00 処理規約 7、設計書 §7.3.1）。

        ``workspace_id`` を渡したときは、それが登録済みでなければ ``E_WORKSPACE_NOT_CONFIGURED``。省略時は順に

        1. ``path_hint`` を含む登録済みルート（入れ子なら最も深いルート）。
        2. アクティブなワークスペース。
        3. ``path_hint`` から上方向に最も近いマーカーを持つディレクトリ（自動登録し、``warnings`` に記録する）。

        いずれも無ければ ``E_WORKSPACE_NOT_CONFIGURED``（``details.known_workspaces[]``）。
        ``path_hint`` が相対パスなら、アクティブなワークスペースのルート（無ければ cwd）からのパスとして扱う。
        """
        if workspace_id is not None:
            return self.get(workspace_id)
        if path_hint:
            if (ws := self._containing(path_hint)) is not None:
                return ws
        if self._active is not None:
            return self._table[self._active]
        if path_hint:
            nearest = _nearest_marker_dir(Path(os.path.realpath(self._absolute(path_hint))))
            if nearest is not None:
                directory, marker = nearest
                (ws,) = self.register_roots([os.fspath(directory)])
                self.warnings.append(
                    f"auto-registered workspace {ws.workspace_id!r} at {ws.root.as_posix()} "
                    f"(nearest marker: {marker}) for path {path_hint!r}"
                )
                return ws
        raise _not_configured(
            self.list(),
            "workspace_id was omitted and no registered root contains the path, no workspace is active, "
            "and no project marker was found above the path; call configure_workspace first",
        )

    def _containing(self, path_hint: str) -> Workspace | None:
        target = self._absolute(path_hint)
        best: Workspace | None = None
        for ws in self._table.values():
            try:
                confine(ws, target, param="path_hint")
            except LuciankeError as exc:
                if exc.details.get("reason") in ("outside_root", "symlink_escape"):
                    continue
                raise
            if best is None or len(ws.root.parts) > len(best.root.parts):
                best = ws
        return best

    # --- 更新 ---------------------------------------------------------------------------

    @staticmethod
    def detect_subprojects(root: Path | str) -> tuple[SubProject, ...]:
        return detect_subprojects(root)

    def update(self, workspace_id: str, /, **changes: Any) -> Workspace:
        """``Workspace`` の項目を差し替える（``python_env``・``default_base_ref`` など）。ID とルートは変えられない。"""
        ws = self.get(workspace_id)
        if "workspace_id" in changes or "root" in changes:
            raise ValueError("workspace_id and root cannot be changed")
        new = dataclasses.replace(ws, **changes)
        self._table[workspace_id] = new
        return new

    def set_excludes(self, workspace_id: str, globs: Sequence[str]) -> Workspace:
        """除外 glob を **置換** する（追記ではない。仕様書 §11 T-00 処理規約 5）。"""
        if isinstance(globs, str) or not all(isinstance(g, str) for g in globs):
            raise LuciankeError(
                ErrorCode.E_INVALID_ARGUMENT,
                "exclude_globs must be an array of strings",
                details={"param": "exclude_globs", "reason": "not_a_string_array"},
            )
        self.get(workspace_id)
        self._tool_args.setdefault(workspace_id, {})["exclude_globs"] = tuple(globs)
        return self.update(workspace_id, exclude_globs=tuple(globs))

    def set_python_env(self, workspace_id: str, interpreter: str) -> Workspace:
        """Python 環境を明示指定する。実行できなければ ``E_INVALID_ARGUMENT``（何も変えない）。"""
        ws = self.get(workspace_id)
        python_env = detect_python_env(ws.root, interpreter, env=self._env)
        self._tool_args.setdefault(workspace_id, {})["python_interpreter"] = interpreter
        return self.update(workspace_id, python_env=python_env)
