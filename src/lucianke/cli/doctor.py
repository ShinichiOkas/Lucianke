"""``lucianke doctor``: 環境の自己診断（設計書 §11.2・R-10、ADR-16）。

結果は **stderr だけ** に出す（``stdout`` は MCP の JSON-RPC 専用。診断コマンドでも 1 バイトも書かない）。

調べる項目（グループ順）:

* ``runtime``: Python の版（3.12 以上。必須）。
* ``dependencies``: 実行に要る依存パッケージ（必須）と extras（欠けは warn）。
* ``stdout``: ``stdout`` 隔離の自己診断（必須）。子プロセスで ``bootstrap_io`` を呼び、重い import・``print``・``os.write(1, …)`` が
  すべて stderr へ出て、退避 fd のトランスポートだけが本物の stdout に届くことを確かめる。
* ``cache``: キャッシュディレクトリの書き込み可否（必須）と使用量（R-10。肥大は warn）。
* ``tools``: Node・ctags・git の有無（任意。無くても動く。git は dulwich で直読するので不要）。
* ``lsp``: PATH（と ``.venv`` / ``node_modules/.bin`` / 環境変数の上書き）にある言語サーバー（任意）。

終了コード: 必須項目の欠け（``fail``）が 1 つでもあれば 1、無ければ 0。``--strict`` なら warn も 1。引数の誤りは 2。
``--json`` は同じ内容を 1 行の JSON にして stderr に出す。

この層は合成ルート（``config`` / ``app`` / ``bootstrap_io``）を import できない。キャッシュの場所は呼び出し側（``__main__``）が渡す。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Final, Literal, TextIO

from lucianke import __version__
from lucianke.core.procs import minimal_env
from lucianke.lsp.servers import SERVERS, LspServerDef, find_command, find_tsserver

Status = Literal["ok", "warn", "fail", "info"]
VersionRunner = Callable[[Sequence[str]], str | None]

MIN_PYTHON: Final = (3, 12)
CACHE_WARN_BYTES: Final = 5 * 1024**3  # R-10: キャッシュがこれを超えたら warn
DISK_WARN_FREE_BYTES: Final = 1024**3  # キャッシュのあるディスクの空きがこれ未満なら warn
VERSION_TIMEOUT_S: Final = 5.0
SELFTEST_TIMEOUT_S: Final = 60.0
SELFTEST_MARKER: Final = "LUCIANKE-DOCTOR-STDOUT-OK"
CTAGS_CANDIDATES: Final = (
    "ctags",
    "universal-ctags",
)  # syntax/heuristic.find_ctags と同じ。Universal Ctags だけ採る

# メタデータが無いとき（PYTHONPATH=src で動かす等）の実行時の必須依存。通常は ``Requires-Dist`` から読む。
FALLBACK_CORE_DISTS: Final = (
    "mcp", "pydantic", "tree-sitter", "tree-sitter-python", "tree-sitter-typescript", "tree-sitter-javascript",
    "tree-sitter-json", "tree-sitter-yaml", "tree-sitter-toml", "tree-sitter-xml", "tree-sitter-markdown",
    "watchfiles", "PyYAML", "jsonpath-ng", "dulwich",
)  # fmt: skip

# 言語サーバーを入れる手がかり（best effort の言語は載せない）。
INSTALL_HINTS: Final = {
    "basedpyright": "pip install 'lucianke[python]'",
    "pyright": "npm install -g pyright",
    "ruff": "pip install 'lucianke[python]'",
    "typescript-language-server": "npm install -g typescript-language-server typescript",
    "vtsls": "npm install -g @vtsls/language-server",
}

_IS_WINDOWS = sys.platform == "win32"
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA_RE = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")


@dataclass(frozen=True, slots=True)
class Check:
    """診断 1 項目。``required`` は「欠けたら動かない」項目（``fail`` になりうるのは必須だけ）。"""

    group: str
    name: str
    status: Status
    detail: str
    required: bool = False
    path: str | None = None
    version: str | None = None


# --- 実行ファイルの探索 -----------------------------------------------------------------


def _env_get(env: Mapping[str, str], name: str) -> str | None:
    if name in env:
        return env[name]
    if _IS_WINDOWS:
        upper = name.upper()
        for key, value in env.items():
            if key.upper() == upper:
                return value
    return None


def find_executable(name: str, env: Mapping[str, str]) -> str | None:
    """``env`` の ``PATH`` から ``name`` の実行ファイルを探す（絶対ディレクトリだけ。カレントは見ない）。

    Windows は ``PATHEXT``（無ければ ``.COM;.EXE;.BAT;.CMD``）を補う。``shutil.which`` を使わないのは、Windows の ``which`` が
    カレントディレクトリを先に見るのと、``env`` を差し替えて試せるようにするため。
    """
    names = [name]
    if _IS_WINDOWS and not Path(name).suffix:
        pathext = _env_get(env, "PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        names += [name + ext.lower() for ext in pathext.split(";") if ext]
    for raw in (_env_get(env, "PATH") or "").split(os.pathsep):
        raw = raw.strip().strip('"')
        if not raw or not os.path.isabs(raw):
            continue
        for candidate in names:
            path = Path(raw) / candidate
            try:
                if path.is_file() and (_IS_WINDOWS or os.access(path, os.X_OK)):
                    return str(path)
            except OSError:
                continue
    return None


def run_version(argv: Sequence[str]) -> str | None:
    """``argv`` を走らせ、出力（標準出力、無ければ標準エラー）の最初の空でない行を返す。失敗・タイムアウトは ``None``。"""
    try:
        cp = subprocess.run(  # noqa: S603  シェルを介さない argv。stdin は閉じ、出力はパイプで捕捉する
            list(argv), capture_output=True, stdin=subprocess.DEVNULL, timeout=VERSION_TIMEOUT_S,
            env=minimal_env(), check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    text = (cp.stdout or cp.stderr).decode("utf-8", "replace")
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


# --- 各グループ -------------------------------------------------------------------------


def check_runtime() -> list[Check]:
    version = ".".join(str(p) for p in sys.version_info[:3])
    ok = sys.version_info[:2] >= MIN_PYTHON
    return [
        Check(
            "runtime", "python", "ok" if ok else "fail",
            f"{version}" if ok else f"{version} (Lucianke needs Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
            required=True, path=sys.executable, version=version,
        ),
        Check("runtime", "lucianke", "ok", __version__, required=True, version=__version__),
    ]  # fmt: skip


def _split_requirement(req: str) -> tuple[str, str | None]:
    """``Requires-Dist`` の 1 行 → (配布名, extra 名または None)。"""
    head, _, marker = req.partition(";")
    m = _REQ_NAME_RE.match(head)
    name = m.group(1) if m else head.strip()
    em = _EXTRA_RE.search(marker)
    return name, (em.group(1) if em else None)


def _dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def check_dependencies() -> list[Check]:
    """必須の依存（欠けは fail）と、extras ごとの導入状況（欠けは warn）。"""
    requires = metadata.requires("lucianke") if _dist_version("lucianke") else None
    core: list[str] = []
    extras: dict[str, list[str]] = {}
    if requires is None:
        core = list(FALLBACK_CORE_DISTS)
    else:
        for req in requires:
            name, extra = _split_requirement(req)
            if extra is None:
                core.append(name)
            elif extra != "all" and name.lower() != "lucianke":  # ``all`` は他の extras の寄せ集め
                extras.setdefault(extra, []).append(name)
    checks: list[Check] = []
    missing = [n for n in core if _dist_version(n) is None]
    found = len(core) - len(missing)
    if missing:
        checks.append(
            Check(
                "dependencies",
                "required",
                "fail",
                f"missing: {', '.join(missing)} ({found}/{len(core)} installed)",
                required=True,
            )
        )
    else:
        checks.append(
            Check("dependencies", "required", "ok", f"{found}/{len(core)} installed", required=True)
        )
    for extra, names in sorted(extras.items()):
        gone = [n for n in names if _dist_version(n) is None]
        if gone:
            checks.append(
                Check(
                    "dependencies", f"extra:{extra}", "warn",
                    f"missing: {', '.join(gone)} ({len(names) - len(gone)}/{len(names)} installed; "
                    f"install with 'lucianke[{extra}]')",
                )
            )  # fmt: skip
        else:
            checks.append(
                Check("dependencies", f"extra:{extra}", "ok", f"{len(names)}/{len(names)} installed")
            )
    return checks


# 子プロセスで実行する自己診断。``bootstrap_io`` の後に重い import と各経路の出力を行い、本物の stdout には
# 退避 fd のトランスポートの 1 行だけが届くことを親が確かめる。
_SELFTEST_CODE: Final = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from lucianke.bootstrap_io import bootstrap_io, open_transport_streams
fds = bootstrap_io()
try:
    import mcp.server, pydantic, tree_sitter, watchfiles, yaml, jsonpath_ng, dulwich.repo  # noqa: F401
except ImportError as exc:
    sys.stderr.write("import failed: %s\n" % exc)
    raise SystemExit(3)
print("doctor-noise-print")
os.write(1, b"doctor-noise-fd1\n")
sys.__stdout__.write("doctor-noise-dunder\n")
sys.__stdout__.flush()
_, out = open_transport_streams(fds)
out.write(sys.argv[2] + "\n")
out.flush()
"""


def check_stdout_isolation(*, timeout_s: float = SELFTEST_TIMEOUT_S) -> list[Check]:
    """子プロセスで ``bootstrap_io`` を試し、本物の stdout が純粋であることを確かめる（必須）。"""
    pkg_root = str(Path(__file__).resolve().parents[2])  # .../src（lucianke の親）
    argv = [sys.executable, "-I", "-c", _SELFTEST_CODE, pkg_root, SELFTEST_MARKER]
    name = "stdout isolation"
    try:
        cp = subprocess.run(  # noqa: S603  自分自身の Python を起動する（シェルを介さない）
            argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout_s, env=minimal_env(), check=False
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check("stdout", name, "fail", f"self-test could not run: {exc}", required=True)]
    err_tail = cp.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["(no output)"]
    if cp.returncode != 0:
        return [
            Check("stdout", name, "fail", f"self-test exited {cp.returncode}: {err_tail[0]}", required=True)
        ]
    expected = (SELFTEST_MARKER + "\n").encode()
    if cp.stdout != expected:
        return [
            Check("stdout", name, "fail", f"stdout was not pure: got {cp.stdout[:80]!r}, expected only the transport line", required=True)
        ]  # fmt: skip
    leaked = [
        n for n in (b"doctor-noise-print", b"doctor-noise-fd1", b"doctor-noise-dunder") if n not in cp.stderr
    ]
    if leaked:
        return [
            Check(
                "stdout",
                name,
                "fail",
                f"noise was lost instead of redirected to stderr: {leaked}",
                required=True,
            )
        ]
    return [
        Check(
            "stdout",
            name,
            "ok",
            "print / os.write(1) / sys.__stdout__ / heavy imports all went to stderr",
            required=True,
        )
    ]


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # 到達しない


def _tree_usage(path: Path) -> tuple[int, int]:
    """``path`` 以下の (合計バイト, ファイル数)。リンクはたどらない。読めない項目は飛ばす。"""
    total = files = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def check_cache(cache_root: Path) -> list[Check]:
    """書き込み可否（必須）と使用量（R-10）。ディレクトリが無ければ作る（サーバーも起動時に作る）。"""
    root = Path(cache_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, prefix=".doctor-", delete=True) as probe:
            probe.write(b"ok")
            probe.flush()
    except OSError as exc:
        return [
            Check(
                "cache", "writable", "fail", f"cannot write to {root}: {exc}", required=True, path=str(root)
            )
        ]
    checks = [Check("cache", "writable", "ok", str(root), required=True, path=str(root))]

    parts: list[tuple[str, int]] = []
    total = files = 0
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        size, count = _tree_usage(Path(entry.path))
                    else:
                        size, count = entry.stat(follow_symlinks=False).st_size, 1
                except OSError:
                    continue
                parts.append((entry.name, size))
                total += size
                files += count
    except OSError:
        pass
    parts.sort(key=lambda p: (-p[1], p[0]))
    top = ", ".join(f"{name} {_fmt_bytes(size)}" for name, size in parts[:3] if size)
    detail = f"{_fmt_bytes(total)} in {files} files" + (f" ({top})" if top else "")
    over = total > CACHE_WARN_BYTES
    checks.append(
        Check(
            "cache", "usage", "warn" if over else "ok",
            detail + (f"; over {_fmt_bytes(CACHE_WARN_BYTES)}: delete old workspace directories" if over else ""),
            path=str(root),
        )
    )  # fmt: skip
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = None
    if free is not None:
        low = free < DISK_WARN_FREE_BYTES
        checks.append(
            Check("cache", "disk free", "warn" if low else "ok", _fmt_bytes(free) + (" (low)" if low else ""), path=str(root))
        )  # fmt: skip
    return checks


def check_tools(env: Mapping[str, str], run: VersionRunner, *, ts_server_found: bool = False) -> list[Check]:
    """Node・ctags・git（すべて任意）。"""
    checks: list[Check] = []
    node = find_executable("node", env)
    if node:
        node_version = run([node, "--version"])
        checks.append(
            Check("tools", "node", "ok", node_version or "version unknown", path=node, version=node_version)
        )
    else:
        needed = (
            "needed by typescript-language-server / vtsls"
            if ts_server_found
            else "TypeScript / JavaScript LSP unavailable without it"
        )
        checks.append(
            Check("tools", "node", "warn" if ts_server_found else "info", f"not found on PATH ({needed})")
        )

    ctags_found = None
    not_universal = None
    for cand in CTAGS_CANDIDATES:
        exe = find_executable(cand, env)
        if exe is None:
            continue
        out = run([exe, "--version"])
        if out and "Universal Ctags" in out:
            ctags_found = (exe, out)
            break
        not_universal = not_universal or (exe, out)
    if ctags_found:
        checks.append(Check("tools", "ctags", "ok", ctags_found[1], path=ctags_found[0]))
    elif not_universal:
        checks.append(
            Check("tools", "ctags", "warn", "found but not Universal Ctags (ignored); built-in block estimation is used", path=not_universal[0])
        )  # fmt: skip
    else:
        checks.append(
            Check(
                "tools",
                "ctags",
                "info",
                "not found on PATH (optional; built-in block estimation is used for unknown languages)",
            )
        )

    git = find_executable("git", env)
    if git:
        checks.append(
            Check(
                "tools",
                "git",
                "ok",
                (run([git, "--version"]) or "version unknown") + " (not required: .git is read via dulwich)",
                path=git,
            )
        )
    else:
        checks.append(
            Check("tools", "git", "info", "not found on PATH (not required: .git is read via dulwich)")
        )
    return checks


def _resolves(argv0: str, env: Mapping[str, str]) -> bool:
    """起動コマンドの先頭が実在するか（環境変数の上書きは存在確認されずに argv になるため）。"""
    if os.path.isabs(argv0):
        return Path(argv0).is_file()
    return find_executable(argv0, env) is not None


def check_lsp(root: Path, env: Mapping[str, str]) -> tuple[list[Check], bool]:
    """言語サーバーの有無。戻り値は (結果, TypeScript 系が 1 つでも見つかったか)。"""
    by_name: dict[str, list[LspServerDef]] = {}
    for defn in SERVERS:
        by_name.setdefault(defn.name, []).append(defn)

    found: dict[str, list[str]] = {}
    broken: dict[str, str] = {}
    for name, defs in by_name.items():
        argv = find_command(defs[0], workspace_root=root, environ=env)
        if argv is None:
            continue
        overridden = any((env.get(v) or "").strip() for d in defs for v in (d.server_env_var, d.env_var))
        if overridden and not _resolves(argv[0], env):
            broken[name] = argv[0]
            continue
        found[name] = argv

    def peers_found(name: str) -> list[str]:
        keys = {(d.language_id, d.role) for d in by_name[name]}
        return sorted(
            {
                d.name
                for d in SERVERS
                if (d.language_id, d.role) in keys and d.name != name and d.name in found
            }
        )

    ts_found = any(n in found for n in ("typescript-language-server", "vtsls"))
    group_default: dict[tuple[str, str], str] = {}
    for d in SERVERS:
        group_default.setdefault((d.language_id, d.role), d.name)
    checks: list[Check] = []
    for name, defs in by_name.items():
        langs = ", ".join(dict.fromkeys(d.language_id for d in defs))
        label = f"{name} ({langs})"
        hint = INSTALL_HINTS.get(name)
        if name in found:
            argv = found[name]
            detail = argv[0]
            if name == "typescript-language-server" and find_tsserver(root) is None:
                detail += " (workspace has no node_modules/typescript/lib/tsserver.js: falls back to vtsls)"
            checks.append(Check("lsp", label, "ok", detail, path=argv[0]))
        elif name in broken:
            checks.append(
                Check(
                    "lsp", label, "warn", f"environment override points to a missing command: {broken[name]}"
                )
            )
        elif others := peers_found(name):
            checks.append(
                Check("lsp", label, "info", f"not found; not needed while {', '.join(others)} is available")
            )
        elif defs[0].best_effort:
            checks.append(
                Check("lsp", label, "info", "not found on PATH (best effort; falls back to Tree-sitter)")
            )
        else:
            defaults = {group_default[(d.language_id, d.role)] for d in defs}
            if name not in defaults:  # 既定が無くても、代替まで warn を重ねない
                checks.append(
                    Check("lsp", label, "info", f"not found (alternative to {', '.join(sorted(defaults))})")
                )
            else:
                where = f"; try: {hint}" if hint else ""
                note = (
                    " — F-TS exact postconditions unverifiable; contract tests skip exact checks"
                    if name in ("typescript-language-server", "vtsls")
                    else ""
                )
                checks.append(
                    Check("lsp", label, "warn", f"not found (falls back to Tree-sitter{where}){note}")
                )
    return checks, ts_found


# --- 全体 -------------------------------------------------------------------------------


def collect(
    *,
    cache_root: Path,
    env: Mapping[str, str] | None = None,
    root: Path | None = None,
    run: VersionRunner | None = None,
    selftest: bool = True,
) -> list[Check]:
    """全項目を調べる。``env`` / ``run`` は差し替え用（テスト）。``selftest=False`` は stdout 隔離の子プロセスを省く。"""
    environment: Mapping[str, str] = os.environ if env is None else env
    runner = run or run_version
    lsp_checks, ts_found = check_lsp(Path(root) if root is not None else Path.cwd(), environment)
    checks = check_runtime() + check_dependencies()
    if selftest:
        checks += check_stdout_isolation()
    checks += check_cache(cache_root)
    checks += check_tools(environment, runner, ts_server_found=ts_found)
    checks += lsp_checks
    return checks


def summarize(checks: Sequence[Check]) -> dict[str, int]:
    counts = {"ok": 0, "warn": 0, "fail": 0, "info": 0}
    for c in checks:
        counts[c.status] += 1
    return counts


def exit_code(checks: Sequence[Check], *, strict: bool = False) -> int:
    counts = summarize(checks)
    return 1 if counts["fail"] or (strict and counts["warn"]) else 0


def render_text(checks: Sequence[Check], *, cache_root: Path) -> str:
    labels = {"ok": "ok", "warn": "WARN", "fail": "FAIL", "info": "info"}
    lines = [
        f"lucianke doctor {__version__} (Python {'.'.join(map(str, sys.version_info[:3]))}, {sys.platform})",
        f"cache: {cache_root}",
    ]
    groups: dict[str, list[Check]] = {}
    for c in checks:
        groups.setdefault(c.group, []).append(c)
    for group, items in groups.items():
        width = max(len(c.name) for c in items)
        lines += ["", f"[{group}]"]
        for c in items:
            lines.append(f"  {labels[c.status]:<4}  {c.name:<{width}}  {c.detail}")
    counts = summarize(checks)
    lines += [
        "",
        f"result: {counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail, {counts['info']} info"
        + ("" if not counts["fail"] else "  (required items are missing: exit 1)"),
    ]
    return "\n".join(lines) + "\n"


def render_json(checks: Sequence[Check], *, cache_root: Path) -> str:
    payload = {
        "lucianke": __version__,
        "python": ".".join(map(str, sys.version_info[:3])),
        "platform": sys.platform,
        "cache_root": str(cache_root),
        "ok": not summarize(checks)["fail"],
        "summary": summarize(checks),
        "checks": [asdict(c) for c in checks],
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"


class _Parser(argparse.ArgumentParser):
    """``--help`` も stderr へ（診断コマンドは stdout に何も書かない）。"""

    def print_help(self, file: TextIO | None = None) -> None:
        super().print_help(file or sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="lucianke doctor", description="Diagnose the environment (output goes to stderr).")
    parser.add_argument("--json", action="store_true", help="print one line of JSON instead of the table")
    parser.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    parser.add_argument(
        "--no-selftest",
        action="store_true",
        help="skip the stdout-isolation self-test (it spawns a Python child)",
    )
    parser.add_argument(
        "--root", type=Path, default=None, help="workspace root to probe for LSP servers (default: cwd)"
    )
    return parser


def main(
    argv: Sequence[str],
    *,
    cache_root: Path,
    env: Mapping[str, str] | None = None,
    run: VersionRunner | None = None,
    selftest: bool = True,
    stream: TextIO | None = None,
) -> int:
    """``lucianke doctor`` の本体。``cache_root`` は ``__main__`` が ``config`` から渡す。出力先は ``stream``（既定 stderr）。"""
    args = build_parser().parse_args(list(argv))
    out = stream if stream is not None else sys.stderr
    checks = collect(
        cache_root=cache_root, env=env, root=args.root, run=run, selftest=selftest and not args.no_selftest
    )
    out.write(
        render_json(checks, cache_root=cache_root)
        if args.json
        else render_text(checks, cache_root=cache_root)
    )
    out.flush()
    return exit_code(checks, strict=args.strict)
