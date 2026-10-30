"""exe（PyInstaller でビルドした ``lucianke.exe``）の確かめ。build.ps1 がビルドの後に呼ぶ。

使い方: ``uv run --extra langs python tools/smoke_exe.py dist/lucianke/lucianke.exe``

確かめること（どれか 1 つでも外れたら終了コード 1）:

1. ``--version`` がソースの ``lucianke.__version__`` と一致する
2. ``doctor --json`` で必須の依存（``required``）・全言語の文法（``extra:langs``）・stdout 隔離（``stdout isolation``）が ok
   （exe の中の metadata・自己診断の経路）
3. MCP stdio で ``tools/list`` の名前の集合が、ソースから起動したサーバー（``python -m lucianke``）と同じ
   （``tools/`` の pkgutil 列挙・各ツールの import が exe の中でも漏れていない）
4. ``tests/fixtures/poly`` の写しを索引して、T-03・T-07 が正しい答えを返す（文法・SQL スキーマ・``.scm`` のクエリが同梱されている）
5. stdout に JSON-RPC 以外の 1 行も出ない（stdio の純度）。stdin を閉じると終了コード 0 で終わる

キャッシュは一時フォルダ（``LUCIANKE_CACHE_DIR``）に分け、利用者のキャッシュを汚さない。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lucianke import __version__

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "poly"
INDEX_TIMEOUT_S = 180.0
PAY = "src/shop/services/payment.py"
PROCESS_CHARGE = "PaymentProcessor.process_charge"


class SmokeError(AssertionError):
    pass


def check(cond: bool, what: str) -> None:
    if not cond:
        raise SmokeError(what)
    print(f"  ok  {what}")


class Session:
    """MCP stdio（改行区切りの JSON-RPC）の最小のクライアント。stdout の全行が JSON であることも確かめる。"""

    def __init__(self, argv: Sequence[str], *, cwd: Path, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(  # noqa: S603  自分でビルドした exe / 自分の Python を起動する
            list(argv), cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._id = 0

    def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise SmokeError(f"server closed stdout during {method}; stderr tail:\n{self.stderr_tail()}")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                raise SmokeError(f"stdout is not pure JSON-RPC: {line[:120]!r}") from None
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise SmokeError(f"{method} failed: {reply['error']}")
                return reply["result"]

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke_exe", "version": "0"}},
        )
        self.notify("notifications/initialized")
        return result

    def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        result = self.request("tools/call", {"name": name, "arguments": args})
        text = "".join(c.get("text", "") for c in result.get("content", []))
        return bool(result.get("isError")), text

    def close(self, timeout_s: float = 30.0) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise SmokeError(f"server did not exit within {timeout_s}s after stdin EOF") from None

    def stderr_tail(self) -> str:
        if self.proc.poll() is None:
            self.proc.kill()
        assert self.proc.stderr is not None
        return self.proc.stderr.read().decode("utf-8", "replace")[-2000:]


def tool_names(argv: Sequence[str], *, cwd: Path, env: dict[str, str]) -> set[str]:
    s = Session(argv, cwd=cwd, env=env)
    s.initialize()
    names = {t["name"] for t in s.request("tools/list")["tools"]}
    s.close()
    return names


def wait_for_index(s: Session) -> str:
    deadline = time.monotonic() + INDEX_TIMEOUT_S
    while True:
        _, text = s.call("configure_workspace", {"action": "status"})
        if "running" not in text and "queued" not in text:
            return text
        if time.monotonic() > deadline:
            raise SmokeError(f"index did not finish within {INDEX_TIMEOUT_S}s:\n{text[:500]}")
        time.sleep(1)


def smoke(exe: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="lucianke-smoke-") as tmp:
        work = Path(tmp)
        ws = work / "poly"
        shutil.copytree(FIXTURE, ws)
        env = {**os.environ, "LUCIANKE_CACHE_DIR": str(work / "cache"), "PYTHONUTF8": "1"}

        print("[version]")
        cp = subprocess.run([str(exe), "--version"], capture_output=True, timeout=60, check=False)  # noqa: S603
        check(cp.returncode == 0 and cp.stderr.decode().strip() == f"lucianke {__version__}",
              f"--version prints 'lucianke {__version__}'")  # fmt: skip
        check(cp.stdout == b"", "--version writes nothing to stdout")

        print("[doctor]")
        cp = subprocess.run(  # noqa: S603
            [str(exe), "doctor", "--json", "--root", str(ws)], capture_output=True, env=env, timeout=180, check=False
        )
        lines = cp.stderr.decode("utf-8", "replace").strip().splitlines()
        payload = json.loads(lines[-1]) if lines else {}
        status = {c["name"]: c["status"] for c in payload.get("checks", [])}
        check(status.get("required") == "ok", f"doctor: required dependencies ok (got {status.get('required')})")
        check(status.get("stdout isolation") == "ok", f"doctor: stdout isolation ok (got {status.get('stdout isolation')})")
        check(status.get("extra:langs") == "ok", f"doctor: all grammars bundled (extra:langs got {status.get('extra:langs')})")
        check(cp.stdout == b"", "doctor writes nothing to stdout")

        print("[tools/list]")
        expected = tool_names([sys.executable, "-m", "lucianke"], cwd=ws, env=env)
        got = tool_names([str(exe)], cwd=ws, env=env)
        check(got == expected, f"exe lists the same {len(expected)} tools as the source"
              + ("" if got == expected else f" (missing {sorted(expected - got)}, extra {sorted(got - expected)})"))  # fmt: skip

        print("[tools/call on tests/fixtures/poly]")
        s = Session([str(exe)], cwd=ws, env=env)
        s.initialize()
        err, text = s.call("configure_workspace", {"action": "set_roots", "root_paths": [str(ws)]})
        check(not err, "configure_workspace set_roots")
        status_text = wait_for_index(s)
        check("reindex done" in status_text, "index finished (reindex done)")
        err, text = s.call("search_workspace_symbols", {"query": "process_charge"})
        check(not err and "hits=1" in text and PROCESS_CHARGE in text, "search_workspace_symbols finds process_charge")
        err, text = s.call(
            "query_call_hierarchy",
            {"target": {"path": PAY, "symbol_name": PROCESS_CHARGE}, "direction": "both", "depth": 3},
        )
        check(not err and "create_order" in text and "_validate_limit" in text,
              "query_call_hierarchy returns callers and callees")  # fmt: skip
        err, text = s.call("get_file_skeleton", {"paths": [PAY]})
        check(not err and "PaymentProcessor" in text, "get_file_skeleton reads the file")
        code = s.close()
        check(code == 0, f"server exits 0 on stdin EOF (got {code})")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python tools/smoke_exe.py <path to lucianke.exe>", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    exe = Path(args[0]).resolve()
    if not exe.is_file():
        print(f"smoke: not found: {exe}", file=sys.stderr)
        return 2
    started = time.monotonic()
    try:
        smoke(exe)
    except SmokeError as exc:
        print(f"  FAIL  {exc}")
        print(f"smoke: FAILED ({time.monotonic() - started:.1f}s)")
        return 1
    print(f"smoke: PASSED ({time.monotonic() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
