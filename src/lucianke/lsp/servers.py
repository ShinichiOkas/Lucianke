"""LSP サーバー定義の表と、起動コマンドの探索（設計書 §7.5.1、仕様書 §12.3・§12.7）。

``LspServerDef`` は 1 つの LSP サーバー（言語 × 役）の静的な定義で、プロセスも状態も持たない。状態機械（``pool``）が
``servers_for`` で候補を引き、``find_command`` で起動コマンドを決めて ``ProcessRegistry`` に渡す。

* ``language_id`` は ``workspace.languages`` の言語 ID（``python`` / ``typescript`` / ``tsx`` / ``c_sharp`` …）。
  同じサーバーが複数の言語 ID を受け持つ（vtsls は ``typescript`` / ``tsx`` / ``javascript``）ときは、言語 ID ごとに
  定義を並べる（``lsp_language_id`` は ``didOpen`` に載せる LSP 側の名前）。
* ``role`` は同一言語に並列で持てる 2 役（``types`` = 型解決、``lint`` = Lint / CodeAction。仕様書 §12.3）。
  同じ言語・役の定義は ``SERVERS`` の並び順が優先順（既定 → 代替）。
* ``initialization_options`` は ``initialize`` の ``initializationOptions``、``settings`` は
  ``workspace/configuration`` の応答の元になる設定木（``LspClient.set_settings`` に渡す。セクション名は ``.`` 区切り）。
  実行時にしか決まらない値（Python のインタープリタなど）は ``build_settings`` で重ねる。

起動コマンドの探索順（``find_command``）: 環境変数 ``LUCIANKE_LSP_<LANG>_CMD`` → 指定された venv の ``Scripts|bin`` →
ワークスペースの ``.venv/Scripts|bin`` → ``node_modules/.bin`` → ``PATH``。Windows は ``.exe`` / ``.cmd`` / ``.bat`` も探す。
探索は ``shutil.which`` を使わず自前で行う（Windows の ``which`` はカレントディレクトリを先に見るため、ワークスペース直下に
置かれた実行ファイルを掴まないようにする）。
"""

from __future__ import annotations

import copy
import logging
import os
import re
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_log = logging.getLogger("lucianke.lsp.servers")

Role = Literal["types", "lint"]

_WIN_EXTS = (".exe", ".cmd", ".bat", ".com")
_IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True, slots=True, eq=False)
class LspServerDef:
    """LSP サーバー 1 つの静的な定義。``eq=False``（同一性で比較する。設定の dict を持つのでハッシュ化しない）。

    Attributes:
        language_id: ``workspace.languages`` の言語 ID。環境変数名（``LUCIANKE_LSP_<LANG>_CMD``）にも使う。
        name: サーバーの名前（``basedpyright`` / ``ruff`` / ``vtsls`` …）。ログと ``status`` に出る。
        argv_candidates: 起動コマンドの候補。各候補は ``(実行ファイル名, 引数...)``。先に見つかったものを使う。
        initialization_options: ``initialize`` に載せる ``initializationOptions``。無ければ ``None``（キーごと省く）。
        settings: ``workspace/configuration`` の応答の元になる設定木。
        role: ``"types"``（型解決。環境変数の上書き先）または ``"lint"``。
        lsp_language_id: ``didOpen`` の ``languageId``。``None`` なら ``language_id``。
        best_effort: PATH に無ければ ``unavailable`` で済ませる（スパイクで実機確認していない）サーバー。
    """

    language_id: str
    name: str
    argv_candidates: tuple[tuple[str, ...], ...]
    initialization_options: Mapping[str, Any] | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    role: Role = "types"
    lsp_language_id: str | None = None
    best_effort: bool = False

    @property
    def document_language_id(self) -> str:
        """``textDocument/didOpen`` の ``languageId``。"""
        return self.lsp_language_id or self.language_id

    @property
    def env_var(self) -> str:
        """起動コマンドを上書きする環境変数名。``types`` は ``LUCIANKE_LSP_<LANG>_CMD``、``lint`` は ``..._<LANG>_LINT_CMD``。"""
        return env_var_name(self.language_id, self.role)


def env_var_name(language_id: str, role: Role = "types") -> str:
    """``LUCIANKE_LSP_<LANG>_CMD``（``role="lint"`` は ``LUCIANKE_LSP_<LANG>_LINT_CMD``）。英数字以外は ``_`` にする。"""
    lang = re.sub(r"[^A-Za-z0-9]+", "_", language_id).strip("_").upper()
    suffix = "" if role == "types" else f"_{role.upper()}"
    return f"LUCIANKE_LSP_{lang}{suffix}_CMD"


# --- 表 ---------------------------------------------------------------------------------

# S-4 の実測で確認した設定（basedpyright は workspace/configuration で python / basedpyright(.analysis) を問い合わせる）。
_PYRIGHT_ANALYSIS: dict[str, Any] = {"typeCheckingMode": "standard", "diagnosticMode": "openFilesOnly"}


def _python_settings(*, based: bool) -> dict[str, Any]:
    settings: dict[str, Any] = {"python": {"analysis": dict(_PYRIGHT_ANALYSIS)}}
    if based:
        settings["basedpyright"] = {"analysis": dict(_PYRIGHT_ANALYSIS)}
    return settings


def _ts_family(name: str, argv: tuple[str, ...]) -> list[LspServerDef]:
    """TypeScript / TSX / JavaScript を 1 つのサーバーで受け持つ定義（言語 ID ごとに 1 件）。"""
    langs = (
        ("typescript", "typescript"),
        ("tsx", "typescriptreact"),
        ("javascript", "javascript"),
    )
    return [
        LspServerDef(language_id=lid, name=name, argv_candidates=(argv,), lsp_language_id=lsp_lid)
        for lid, lsp_lid in langs
    ]


def _best_effort(language_id: str, name: str, *argv: str, lsp_language_id: str | None = None) -> LspServerDef:
    return LspServerDef(
        language_id=language_id,
        name=name,
        argv_candidates=(argv,),
        lsp_language_id=lsp_language_id,
        best_effort=True,
    )


SERVERS: tuple[LspServerDef, ...] = (
    # Python: 型解決は basedpyright が既定、pyright が代替。Lint / CodeAction は ruff を並列で持つ。
    LspServerDef(
        language_id="python",
        name="basedpyright",
        argv_candidates=(("basedpyright-langserver", "--stdio"),),
        settings=_python_settings(based=True),
    ),
    LspServerDef(
        language_id="python",
        name="pyright",
        argv_candidates=(("pyright-langserver", "--stdio"),),
        settings=_python_settings(based=False),
    ),
    LspServerDef(language_id="python", name="ruff", argv_candidates=(("ruff", "server"),), role="lint"),
    # TypeScript / JavaScript: vtsls が既定、typescript-language-server が代替（どちらも Node が要る）。
    *_ts_family("vtsls", ("vtsls", "--stdio")),
    *_ts_family("typescript-language-server", ("typescript-language-server", "--stdio")),
    # best effort（PATH に無ければ unavailable）。
    _best_effort("rust", "rust-analyzer", "rust-analyzer"),
    _best_effort("go", "gopls", "gopls"),
    _best_effort("c", "clangd", "clangd"),
    _best_effort("cpp", "clangd", "clangd"),
    _best_effort("c_sharp", "csharp-ls", "csharp-ls", lsp_language_id="csharp"),
    _best_effort("kotlin", "kotlin-language-server", "kotlin-language-server"),
    _best_effort("swift", "sourcekit-lsp", "sourcekit-lsp"),
)


def servers_for(language_id: str, role: Role | None = None) -> tuple[LspServerDef, ...]:
    """``language_id`` の定義を優先順（``SERVERS`` の並び）で返す。``role`` を指定するとその役だけ。"""
    return tuple(d for d in SERVERS if d.language_id == language_id and (role is None or d.role == role))


def default_server(language_id: str, role: Role = "types") -> LspServerDef | None:
    """``language_id`` の ``role`` の既定（先頭）の定義。無ければ ``None``。"""
    found = servers_for(language_id, role)
    return found[0] if found else None


def build_settings(defn: LspServerDef, *, python_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """``defn.settings`` の複製に、実行時にしか決まらない値を重ねる。

    ``python_path`` はワークスペースの Python インタープリタ（``python.pythonPath``）。basedpyright / pyright が
    import を解決するのに使う（S-4: 渡さないと依存パッケージが ``reportMissingImports`` になる）。
    """
    settings: dict[str, Any] = copy.deepcopy(dict(defn.settings))
    if python_path is not None and defn.language_id == "python" and defn.role == "types":
        settings.setdefault("python", {})["pythonPath"] = os.fspath(python_path)
    return settings


# --- 起動コマンドの探索 -----------------------------------------------------------------


def split_command(text: str) -> list[str]:
    """環境変数の起動コマンド文字列を argv に分ける。Windows ではバックスラッシュを escape と見なさない。

    引用符で囲んだパス（``"C:\\Program Files\\x\\ls.exe" --stdio``）も扱える。閉じていない引用符は ``ValueError``。
    """
    if _IS_WINDOWS:
        parts = shlex.split(text, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return shlex.split(text)


def _probe(directory: Path, exe: str) -> str | None:
    """``directory`` の中の実行ファイル ``exe`` を探す。Windows は ``.exe`` / ``.cmd`` / ``.bat`` / ``.com`` を補う。"""
    try:
        if not directory.is_dir():
            return None
    except OSError:
        return None
    if _IS_WINDOWS:
        names = [exe] if Path(exe).suffix.lower() in _WIN_EXTS else [exe + ext for ext in _WIN_EXTS]
    else:
        names = [exe]
    for name in names:
        path = directory / name
        try:
            if path.is_file() and (_IS_WINDOWS or os.access(path, os.X_OK)):
                return str(path)
        except OSError:
            continue
    return None


def _venv_bin_dirs(venv: Path) -> list[Path]:
    """venv の実行ファイルの置き場（``Scripts`` は Windows、``bin`` は他。実行環境の側を先に見る）。"""
    return [venv / "Scripts", venv / "bin"] if _IS_WINDOWS else [venv / "bin", venv / "Scripts"]


def _path_dirs(path_value: str | None) -> list[Path]:
    """``PATH`` の絶対ディレクトリ（相対や空の項目は、起動時のカレントに依存するので見ない）。"""
    dirs: list[Path] = []
    for raw in (path_value or "").split(os.pathsep):
        raw = raw.strip().strip('"')
        if raw and os.path.isabs(raw):
            dirs.append(Path(raw))
    return dirs


def _env_get(env: Mapping[str, str], name: str) -> str | None:
    """環境変数を引く。Windows は名前の大文字小文字を区別しない（``Path`` / ``PATH``）。"""
    if name in env:
        return env[name]
    if _IS_WINDOWS:
        upper = name.upper()
        for key, value in env.items():
            if key.upper() == upper:
                return value
    return None


def find_command(
    defn: LspServerDef,
    *,
    workspace_root: str | os.PathLike[str],
    venv: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str] | None:
    """``defn`` の起動 argv を探す。見つからなければ ``None``（呼び手が ``unavailable: not_found`` にする）。

    探索順: ``defn.env_var`` → ``venv``（Python 環境の根。省略可）の ``Scripts|bin`` → ``<workspace_root>/.venv/Scripts|bin`` →
    ``<workspace_root>/node_modules/.bin`` → ``PATH``。候補（``argv_candidates``）は先頭から順に、候補ごとに上の順で探す。
    環境変数の値は **そのまま argv になる**（存在確認はしない。起動に失敗すれば呼び手が ``init_failed`` にする）。
    環境変数の上書きは、同じ言語・役の定義すべてに効く（代替サーバーの定義でも同じ argv になる）。
    ``environ`` を省略すると ``os.environ``。
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    override = (_env_get(env, defn.env_var) or "").strip()
    if override:
        try:
            argv = split_command(override)
        except ValueError as e:
            _log.warning("%s is not a valid command line (%s); ignoring it", defn.env_var, e)
        else:
            if argv:
                return argv

    root = Path(workspace_root)
    dirs: list[Path] = []
    if venv is not None:
        dirs += _venv_bin_dirs(Path(venv))
    dirs += _venv_bin_dirs(root / ".venv")
    dirs.append(root / "node_modules" / ".bin")
    dirs += _path_dirs(_env_get(env, "PATH"))

    for exe, *args in defn.argv_candidates:
        for directory in dirs:
            hit = _probe(directory, exe)
            if hit is not None:
                return [hit, *args]
    return None
