"""LSP サーバー定義の表と、起動コマンドの探索（設計書 §7.5.1、仕様書 §12.3・§12.7、スパイク S-04・S-05）。

``LspServerDef`` は 1 つの LSP サーバー（言語 × 役）の静的な定義で、プロセスも状態も持たない。状態機械（``pool``）が
``servers_for`` で候補を引き、``find_command`` で起動コマンドを決めて ``ProcessRegistry`` に渡す。

* ``language_id`` は ``workspace.languages`` の言語 ID（``python`` / ``typescript`` / ``tsx`` / ``c_sharp`` …）。
  同じサーバーが複数の言語 ID を受け持つ（TypeScript 系は ``typescript`` / ``tsx`` / ``javascript``）ときは、言語 ID ごとに
  定義を並べる（``lsp_language_id`` は ``didOpen`` に載せる LSP 側の名前）。
* ``role`` は同一言語に並列で持てる 2 役（``types`` = 型解決、``lint`` = Lint / CodeAction。仕様書 §12.3）。
  同じ言語・役の定義は ``SERVERS`` の並び順が優先順（既定 → 代替）。
* ``initialization_options`` は ``initialize`` の ``initializationOptions``、``settings`` は
  ``workspace/configuration`` の応答の元になる設定木（``LspClient.set_settings`` に渡す。セクション名は ``.`` 区切り）。
  実行時にしか決まらない値（Python のインタープリタ、TypeScript の ``tsserver``）は ``build_settings`` /
  ``build_initialization_options`` で重ねる。
* ``locale``: ``initialize`` の ``locale``。basedpyright / pyright は OS のロケールで診断が日本語になるので ``"en"`` に固定する（S-4）。
* ``requests_m1``: ADR-15 の範囲でこのサーバーに送ってよい LSP の method。Python の ``codeAction``（quickfix・organizeImports・
  fixAll）の供給元は **ruff だけ**（basedpyright の quickfix は ``# pyright: ignore`` の追加だけ。S-4）。
  ruff の organizeImports / fixAll は ``codeAction`` が ``edit`` 無しで返し、``codeAction/resolve`` で ``edit`` が付く。

サーバーの位置の単位（``positionEncoding``）: basedpyright・pyright・tsls・vtsls は応答に無く UTF-16、ruff は ``utf-8`` を返す
（S-4・S-5）。交渉の結果は ``LspClient.negotiated_encoding`` が応答から読むので、ここには持たない。

TypeScript / JavaScript（S-5）: **既定は typescript-language-server（tsls）、代替は vtsls**。vtsls は編集を元に戻した後に古い診断を
返すことがある。tsls はワークスペースに ``node_modules/typescript/lib/tsserver.js`` が無いと ``initialize`` が失敗する
（TypeScript 7 には ``tsserver.js`` が無い）ので、``needs_workspace_tsserver`` の定義は ``find_tsserver`` が ``None`` のとき
使わず vtsls（TypeScript を同梱）に切り替える。tsserver の場所は tsls へ ``initializationOptions.tsserver.path``、vtsls へ
``workspace/configuration`` の応答（``vtsls.autoUseWorkspaceTsdk`` + ``typescript.tsdk``）で渡す。

起動コマンドの探索順（``find_command``）: 環境変数（サーバー名 ``LUCIANKE_LSP_<NAME>_CMD`` → 言語 ``LUCIANKE_LSP_<LANG>_CMD``）→
指定された venv の ``Scripts|bin`` → ワークスペースの ``.venv/Scripts|bin`` → ``node_modules/.bin`` →
**Lucianke 自身の Python 環境**（``host_venv``。``pip install lucianke[python]`` / ``uv sync`` が入れた basedpyright・ruff）→ ``PATH``。
Windows は ``.exe`` / ``.cmd`` / ``.bat`` も探す。探索は ``shutil.which`` を使わず自前で行う（Windows の ``which`` は
カレントディレクトリを先に見るため、ワークスペース直下に置かれた実行ファイルを掴まないようにする）。
"""

from __future__ import annotations

import copy
import logging
import os
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_log = logging.getLogger("lucianke.lsp.servers")

Role = Literal["types", "lint"]
TsserverVia = Literal["initialization_options", "settings"]
ReadySignal = Literal["progress", "initialized"]

_WIN_EXTS = (".exe", ".cmd", ".bat", ".com")
_IS_WINDOWS = sys.platform == "win32"

# ADR-15 の LSP 要求。ナビゲーション系（定義・参照・ホバー・リネーム・呼び出し階層）と、コードアクション系。
_NAVIGATION = frozenset(
    {
        "textDocument/definition",
        "textDocument/references",
        "textDocument/hover",
        "textDocument/prepareRename",
        "textDocument/rename",
        "textDocument/prepareCallHierarchy",
        "callHierarchy/incomingCalls",
        "callHierarchy/outgoingCalls",
    }
)
_CODE_ACTION = frozenset({"textDocument/codeAction", "codeAction/resolve"})


@dataclass(frozen=True, slots=True, eq=False)
class LspServerDef:
    """LSP サーバー 1 つの静的な定義。``eq=False``（同一性で比較する。設定の dict を持つのでハッシュ化しない）。

    Attributes:
        language_id: ``workspace.languages`` の言語 ID。環境変数名（``LUCIANKE_LSP_<LANG>_CMD``）にも使う。
        name: サーバーの名前（``basedpyright`` / ``ruff`` / ``vtsls`` …）。ログと ``status`` に出る。
        argv_candidates: 起動コマンドの候補。各候補は ``(実行ファイル名, 引数...)``。先に見つかったものを使う。
        initialization_options: ``initialize`` に載せる ``initializationOptions``。無ければ ``None``（キーごと省く）。
        settings: ``workspace/configuration`` の応答の元になる設定木。
        role: ``"types"``（型解決。言語単位の環境変数の上書き先）または ``"lint"``。
        lsp_language_id: ``didOpen`` の ``languageId``。``None`` なら ``language_id``。
        best_effort: PATH に無ければ ``unavailable`` で済ませる（スパイクで実機確認していない）サーバー。
        locale: ``initialize`` の ``locale``。``None`` なら送らない。
        requests_m1: このサーバーに送ってよい LSP の method（ADR-15）。
        tsserver_via: ``tsserver`` の場所の渡し方。``None`` は不要（TypeScript 系以外）。
        needs_workspace_tsserver: ワークスペースに ``tsserver.js`` が無いと ``initialize`` が失敗する（tsls）。
        ready_signal: ``ready`` とみなす合図。``"progress"``（既定）は ``$/progress`` の ``end`` が揃う・最初の
            ``publishDiagnostics``・``ready_fallback_s`` 経過のいちばん早いもの（解析に時間がかかるサーバー用）。
            ``"initialized"`` は ``initialize`` の応答が返った時点（索引を持たず、起動の直後から要求に答えるサーバー用。ruff。BUG-0050）。
        no_cancel_methods: 期限切れ・取り消しのときも ``$/cancelRequest`` を **送らない** method。遅れて届いた応答は client が捨てる。
            取り消しを受けるとプロセスがその method に以後答えられなくなるサーバー用（basedpyright / pyright の
            references・implementation・executeCommand。BUG-0090）。
        restart_on_errors: エラー応答の ``message`` にこのどれかを含むと、そのプロセスはその method に以後答えられない壊れ方を
            したとみなして起動し直す（``LspHandle.record_broken``。BUG-0090）。``no_cancel_methods`` で防げなかったときの回復。
    """

    language_id: str
    name: str
    argv_candidates: tuple[tuple[str, ...], ...]
    initialization_options: Mapping[str, Any] | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    role: Role = "types"
    lsp_language_id: str | None = None
    best_effort: bool = False
    locale: str | None = None
    requests_m1: frozenset[str] = frozenset()
    tsserver_via: TsserverVia | None = None
    needs_workspace_tsserver: bool = False
    ready_signal: ReadySignal = "progress"
    no_cancel_methods: frozenset[str] = frozenset()
    restart_on_errors: tuple[str, ...] = ()

    @property
    def document_language_id(self) -> str:
        """``textDocument/didOpen`` の ``languageId``。"""
        return self.lsp_language_id or self.language_id

    @property
    def env_var(self) -> str:
        """言語単位の環境変数名。``types`` は ``LUCIANKE_LSP_<LANG>_CMD``、``lint`` は ``LUCIANKE_LSP_<LANG>_LINT_CMD``。"""
        return env_var_name(self.language_id, self.role)

    @property
    def server_env_var(self) -> str:
        """サーバー名単位の環境変数名 ``LUCIANKE_LSP_<NAME>_CMD``（例 ``LUCIANKE_LSP_RUFF_CMD``）。"""
        return server_env_var_name(self.name)

    def supports(self, method: str) -> bool:
        """``method`` をこのサーバーに送ってよいか（``requests_m1``）。"""
        return method in self.requests_m1


def _screaming(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()


def env_var_name(language_id: str, role: Role = "types") -> str:
    """``LUCIANKE_LSP_<LANG>_CMD``（``role="lint"`` は ``LUCIANKE_LSP_<LANG>_LINT_CMD``）。英数字以外は ``_`` にする。"""
    suffix = "" if role == "types" else f"_{role.upper()}"
    return f"LUCIANKE_LSP_{_screaming(language_id)}{suffix}_CMD"


def server_env_var_name(server_name: str) -> str:
    """``LUCIANKE_LSP_<NAME>_CMD``（``typescript-language-server`` → ``LUCIANKE_LSP_TYPESCRIPT_LANGUAGE_SERVER_CMD``）。"""
    return f"LUCIANKE_LSP_{_screaming(server_name)}_CMD"


# --- 表 ---------------------------------------------------------------------------------

# S-4 の実測で確認した設定（basedpyright は workspace/configuration で python / basedpyright(.analysis) を問い合わせる）。
_PYRIGHT_ANALYSIS: dict[str, Any] = {"typeCheckingMode": "standard", "diagnosticMode": "openFilesOnly"}


# basedpyright / pyright（pyright の languageServerBase と cancellationUtils）は、要求の取り消しが **処理の始まる前** に届くと
# （要求がキューで待っている間、または ``window/workDoneProgress/create`` の往復を待っている間）、その種類の要求に以後ずっと
# ``this._token.cancel is not a function`` を返す（BUG-0090。basedpyright 1.40.2 = pyright 1.1.414 で再現）。
#   1. ``CancelAfter`` → ``setupCombinedTokensFor`` は、要求の token が取り消し済みなら ``source.token`` に触れる前に
#      ``source.cancel()`` を呼ぶ。vscode-jsonrpc の ``CancellationTokenSource`` は token が未生成だと ``_token`` に凍結された
#      ``CancellationToken.Cancelled``（``cancel`` を持たない）を入れる。
#   2. ``onReferences`` / ``onImplementation`` / 長い ``onExecuteCommand`` はその source を ``_pending*CancellationSource`` に残し、
#      ``finally`` では ``dispose()`` するだけ（凍結 token には何もしない）。
#   3. 次の同じ種類の要求は、まず ``_pending*CancellationSource.cancel()`` を呼び、``this._token.cancel()`` が TypeError で落ちる。
#      欄を ``undefined`` に戻す前に落ちるので、プロセスが続く間ずっと失敗する（他の method は影響を受けない）。前の要求の処理中に
#      次が届いたときは、次の要求が欄を健全な source で上書きするので壊れない（壊れるのは、前の要求が終わってから次が届いたとき。
#      期限切れで降格した後の次の問い合わせはふつうこれに当たる）。
# 取り消しを送らなければ token は取り消されない（次の同じ種類の要求が、サーバー自身の健全な source で前の要求を止める）。
_PYRIGHT_NO_CANCEL = frozenset(
    {"textDocument/references", "textDocument/implementation", "workspace/executeCommand"}
)
_PYRIGHT_BROKEN = ("this._token.cancel is not a function",)


def _python_settings(*, based: bool) -> dict[str, Any]:
    settings: dict[str, Any] = {"python": {"analysis": dict(_PYRIGHT_ANALYSIS)}}
    if based:
        settings["basedpyright"] = {"analysis": dict(_PYRIGHT_ANALYSIS)}
    return settings


def _ts_family(name: str, argv: tuple[str, ...], **kwargs: Any) -> list[LspServerDef]:
    """TypeScript / TSX / JavaScript を 1 つのサーバーで受け持つ定義（言語 ID ごとに 1 件）。"""
    langs = (
        ("typescript", "typescript"),
        ("tsx", "typescriptreact"),
        ("javascript", "javascript"),
    )
    return [
        LspServerDef(
            language_id=lid,
            name=name,
            argv_candidates=(argv,),
            lsp_language_id=lsp_lid,
            requests_m1=_NAVIGATION | _CODE_ACTION,
            **kwargs,
        )
        for lid, lsp_lid in langs
    ]


def _best_effort(language_id: str, name: str, *argv: str, lsp_language_id: str | None = None) -> LspServerDef:
    return LspServerDef(
        language_id=language_id,
        name=name,
        argv_candidates=(argv,),
        lsp_language_id=lsp_language_id,
        best_effort=True,
        requests_m1=_NAVIGATION | _CODE_ACTION,
    )


SERVERS: tuple[LspServerDef, ...] = (
    # Python: 型解決は basedpyright が既定、pyright が代替。Lint / CodeAction は ruff を並列で持つ（ruff は utf-8 を返す）。
    LspServerDef(
        language_id="python",
        name="basedpyright",
        argv_candidates=(("basedpyright-langserver", "--stdio"),),
        settings=_python_settings(based=True),
        locale="en",
        requests_m1=_NAVIGATION,
        no_cancel_methods=_PYRIGHT_NO_CANCEL,
        restart_on_errors=_PYRIGHT_BROKEN,
    ),
    LspServerDef(
        language_id="python",
        name="pyright",
        argv_candidates=(("pyright-langserver", "--stdio"),),
        settings=_python_settings(based=False),
        locale="en",
        requests_m1=_NAVIGATION,
        no_cancel_methods=_PYRIGHT_NO_CANCEL,
        restart_on_errors=_PYRIGHT_BROKEN,
    ),
    LspServerDef(
        language_id="python",
        name="ruff",
        argv_candidates=(("ruff", "server"),),
        role="lint",
        requests_m1=_CODE_ACTION,
        # ruff は $/progress を出さず、診断も文書を開くまで出さない（起動は 12 ms）。progress の合図のままだと、最初の
        # publishDiagnostics は ready を待たれた文書からしか来ないので ready_fallback_s（15 秒）まで ready にならない（BUG-0050）。
        ready_signal="initialized",
    ),
    # TypeScript / JavaScript: typescript-language-server が既定（診断が常に最新）、vtsls が代替（TypeScript を同梱するので
    # ワークスペースに tsserver.js が無いときの逃げ先）。どちらも Node が要る。
    *_ts_family(
        "typescript-language-server",
        ("typescript-language-server", "--stdio"),
        tsserver_via="initialization_options",
        needs_workspace_tsserver=True,
    ),
    *_ts_family("vtsls", ("vtsls", "--stdio"), tsserver_via="settings"),
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


def find_tsserver(
    workspace_root: str | os.PathLike[str], *, explicit: str | os.PathLike[str] | None = None
) -> Path | None:
    """tsserver（``tsserver.js``）の場所。``explicit`` が実在すればそれ、無ければ ``<root>/node_modules/typescript/lib/tsserver.js``。

    見つからなければ ``None``（TypeScript 7 には ``tsserver.js`` が無い。S-5）。``needs_workspace_tsserver`` の定義
    （typescript-language-server）は ``None`` のとき起動に失敗するので、呼び手は vtsls に切り替える。
    """
    if explicit is not None:
        path = Path(explicit)
        if path.is_file():
            return path
    path = Path(workspace_root) / "node_modules" / "typescript" / "lib" / "tsserver.js"
    return path if path.is_file() else None


def build_initialization_options(
    defn: LspServerDef, *, tsserver_path: str | os.PathLike[str] | None = None
) -> dict[str, Any] | None:
    """``defn.initialization_options`` の複製に、実行時にしか決まらない値を重ねる。無ければ ``None``（キーごと省く）。

    ``tsserver_path`` は typescript-language-server の ``initializationOptions.tsserver.path``（S-5）。
    """
    options = (
        None if defn.initialization_options is None else copy.deepcopy(dict(defn.initialization_options))
    )
    if defn.tsserver_via == "initialization_options" and tsserver_path is not None:
        options = options if options is not None else {}
        options.setdefault("tsserver", {})["path"] = os.fspath(tsserver_path)
    return options


def build_settings(
    defn: LspServerDef,
    *,
    python_path: str | os.PathLike[str] | None = None,
    extra_paths: Sequence[str | os.PathLike[str]] = (),
    tsserver_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """``defn.settings`` の複製に、実行時にしか決まらない値を重ねる（``LspClient.set_settings`` に渡す）。

    * ``python_path``: ワークスペースの Python インタープリタ（``python.pythonPath``）。basedpyright / pyright が import を
      解決するのに使う。**``workspace/configuration`` の ``python`` セクションでしか渡せない**（``initializationOptions`` は
      効かない。渡さないと依存パッケージが ``reportMissingImports`` になる。S-4）。
    * ``extra_paths``: import の根に足すディレクトリ（src レイアウトの ``src`` など。絶対パスで渡す。``PythonEnv.extra_paths``）。
      ``python.analysis.extraPaths``（basedpyright は ``basedpyright.analysis.extraPaths`` にも）に入れる。渡さないと
      ``tests/`` などから ``src/`` の下のパッケージが解決されず（hover が Unknown）、参照・rename が取りこぼす。
      ``defn.settings`` が既に ``extraPaths`` を持つならそれが先。利用者の ``pyrightconfig.json`` は pyright 自身が優先する。
    * ``tsserver_path``: vtsls へ ``vtsls.autoUseWorkspaceTsdk`` と ``typescript.tsdk``（``tsserver.js`` のある ``lib``）を返して
      ワークスペースの TypeScript を使わせる。返さないと同梱の TypeScript を使う（S-5）。
    """
    settings: dict[str, Any] = copy.deepcopy(dict(defn.settings))
    if python_path is not None and defn.language_id == "python" and defn.role == "types":
        settings.setdefault("python", {})["pythonPath"] = os.fspath(python_path)
    if extra_paths and defn.language_id == "python" and defn.role == "types":
        for section in ("python", "basedpyright"):
            if section == "python" or section in settings:
                settings.setdefault(section, {}).setdefault("analysis", {}).setdefault(
                    "extraPaths", [os.fspath(p) for p in extra_paths]
                )
    if defn.tsserver_via == "settings" and tsserver_path is not None:
        settings.setdefault("vtsls", {})["autoUseWorkspaceTsdk"] = True
        settings.setdefault("typescript", {})["tsdk"] = os.fspath(Path(tsserver_path).parent)
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


def _override_argv(defn: LspServerDef, env: Mapping[str, str]) -> list[str] | None:
    """環境変数による上書き。サーバー名単位 → 言語単位の順に見て、最初に有効なものを採る。"""
    for var in (defn.server_env_var, defn.env_var):
        value = (_env_get(env, var) or "").strip()
        if not value:
            continue
        try:
            argv = split_command(value)
        except ValueError as e:
            _log.warning("%s is not a valid command line (%s); ignoring it", var, e)
            continue
        if argv:
            return argv
    return None


def host_venv() -> Path:
    """Lucianke 自身が動いている Python 環境の根（``sys.prefix``。venv の中なら venv の根）。``find_command`` の ``host_venv`` に渡す。"""
    return Path(sys.prefix)


def find_command(
    defn: LspServerDef,
    *,
    workspace_root: str | os.PathLike[str],
    venv: str | os.PathLike[str] | None = None,
    host_venv: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str] | None:
    """``defn`` の起動 argv を探す。見つからなければ ``None``（呼び手が ``unavailable: not_found`` にする）。

    探索順: 環境変数 → ``venv``（Python 環境の根。省略可）の ``Scripts|bin`` → ``<workspace_root>/.venv/Scripts|bin`` →
    ``<workspace_root>/node_modules/.bin`` → ``host_venv``（Lucianke 自身の Python 環境の根。省略可。``host_venv()`` が返す）の
    ``Scripts|bin`` → ``PATH``。候補（``argv_candidates``）は先頭から順に、候補ごとに上の順で探す。

    ``host_venv`` は **ワークスペースに venv が無いとき**（``.venv`` の無いプロジェクト、使い捨て作業ツリーのコピーなど）に、Lucianke と
    一緒に入った basedpyright・ruff を拾うためのもの。これが無いと、起動した venv を有効化していない限り PATH にしか無く、
    PATH 上の別の ``pyright-langserver`` を代替として掴んでしまう（BUG-0037）。ワークスペースの venv は先に見る（プロジェクトが
    固定したサーバーの版が勝つ）。省略（``None``）すると見ない（探索の単体テストが環境に依らないように、既定では足さない）。

    環境変数は ``defn.server_env_var``（``LUCIANKE_LSP_<サーバー名>_CMD``。そのサーバーだけ上書き）→ ``defn.env_var``
    （``LUCIANKE_LSP_<言語>_CMD``。同じ言語・役の定義すべてに効く）の順。値は **そのまま argv になる**
    （存在確認はしない。起動に失敗すれば呼び手が ``init_failed`` にする）。``environ`` を省略すると ``os.environ``。
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    override = _override_argv(defn, env)
    if override is not None:
        return override

    root = Path(workspace_root)
    dirs: list[Path] = []
    if venv is not None:
        dirs += _venv_bin_dirs(Path(venv))
    dirs += _venv_bin_dirs(root / ".venv")
    dirs.append(root / "node_modules" / ".bin")
    if host_venv is not None:
        dirs += _venv_bin_dirs(Path(host_venv))
    dirs += _path_dirs(_env_get(env, "PATH"))

    for exe, *args in defn.argv_candidates:
        for directory in dirs:
            hit = _probe(directory, exe)
            if hit is not None:
                return [hit, *args]
    return None
