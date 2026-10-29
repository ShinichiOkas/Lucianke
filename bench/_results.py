"""ベンチマーク台本の共通部品: 結果の書き出し（仕様書 §14.8）、機材・コミット・フィクスチャ SHA の記録、百分位。

標準ライブラリだけ。``bench/`` の台本（run_latency / run_reaction / 18a のトークン台本）が
``sys.path`` に ``bench/`` を足して ``import _results`` で使う。

結果ファイルの置き場は ``bench/results/<日時>-<OS>-<kind>.json``（``.gitignore`` 済み。要約は ``.pair-agent/acceptance/``）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO / "bench" / "results"

SCHEMA_VERSION = 1

# 木の走査で捨てるディレクトリ（フィクスチャの内容ハッシュ・Tier 1 ファイルの列挙で共通）
SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
     "dist", "build", ".cache", "results", ".idea", ".vscode"}
)  # fmt: skip


def os_name() -> str:
    """結果ファイル名と記録に使う OS 名: ``windows`` / ``macos`` / ``linux``。"""
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def iso_now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# --- 統計 -----------------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float | None:
    """最近順位法（nearest-rank）。``p`` は 0〜100。200 件なら p95 は昇順の 190 番目。空は None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def summarize(values: Iterable[float]) -> dict[str, Any]:
    """件数・最小・p50・p95・p99・最大・平均。空なら件数 0 だけ。"""
    vals = [float(v) for v in values]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": round(min(vals), 3),
        "p50": round(percentile(vals, 50) or 0.0, 3),
        "p95": round(percentile(vals, 95) or 0.0, 3),
        "p99": round(percentile(vals, 99) or 0.0, 3),
        "max": round(max(vals), 3),
        "mean": round(sum(vals) / len(vals), 3),
    }


# --- 機材 -----------------------------------------------------------------------------------


def _run(cmd: list[str], timeout: float = 10.0, cwd: Path | None = None) -> str | None:
    try:
        cp = subprocess.run(  # noqa: S603  固定の引数列（シェルを通さない）
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, encoding="utf-8", errors="replace",
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout.strip() if cp.returncode == 0 else None


def _cpu_model() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    elif sys.platform == "darwin":
        out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out:
            return out
    else:
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or "unknown"


def _memory_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [  # noqa: RUF012  ctypes の慣例
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]  # fmt: skip

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
            return round(st.ullTotalPhys / 2**30, 1)
        if sys.platform == "darwin":
            out = _run(["sysctl", "-n", "hw.memsize"])
            return round(int(out) / 2**30, 1) if out else None
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 2**20, 1)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _storage_type(path: Path) -> str:
    """``path`` を載せているストレージの種別（SSD / NVMe / HDD / unknown）。取れなければ ``unknown``。"""
    try:
        if sys.platform == "win32":
            drive = path.resolve().drive.rstrip(":")
            if not drive:
                return "unknown"
            ps = (
                f"$d = Get-Partition -DriveLetter {drive} | Get-Disk | Get-PhysicalDisk; "
                "'{0}/{1}' -f $d.MediaType, $d.BusType"
            )
            out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=20.0)
            return out or "unknown"
        if sys.platform == "darwin":
            out = _run(["diskutil", "info", "/"])
            if out:
                for line in out.splitlines():
                    if "Solid State" in line:
                        return "SSD" if line.split(":", 1)[1].strip().lower() == "yes" else "HDD"
            return "unknown"
        dev = _run(["findmnt", "-no", "SOURCE", "--target", str(path)])
        if dev:
            base = Path(dev).name.rstrip("0123456789") or Path(dev).name
            rot = Path("/sys/block") / base / "queue" / "rotational"
            if rot.is_file():
                return "HDD" if rot.read_text(encoding="utf-8").strip() == "1" else "SSD"
    except OSError:
        pass
    return "unknown"


def machine_info(storage_probe: Path | None = None) -> dict[str, Any]:
    """機材の記録（仕様書 §14.2）: OS・CPU・メモリ・ストレージ種別。"""
    return {
        "os": os_name(),
        "os_detail": platform.platform(),
        "python": platform.python_version(),
        "cpu": _cpu_model(),
        "cpu_logical": os.cpu_count(),
        "memory_gb": _memory_gb(),
        "storage": _storage_type(storage_probe or REPO),
    }


# --- コミット・フィクスチャ -------------------------------------------------------------------


def repo_info(repo: Path = REPO) -> dict[str, Any]:
    """Lucianke のコミット SHA と、作業ツリーが汚れているか（他の役が並行して編集中なら true になる）。"""
    sha = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    porcelain = _run(["git", "status", "--porcelain"], cwd=repo)
    dirty_paths = [ln for ln in (porcelain or "").splitlines() if ln.strip()]
    return {"commit": sha, "dirty": bool(dirty_paths), "dirty_paths": len(dirty_paths)}


def tree_digest(root: Path) -> str:
    """木の内容ハッシュ（相対パスと内容の SHA-256 を、パス昇順に畳んだもの）。``SKIP_DIRS`` と ``.pyc`` は除く。"""
    h = hashlib.sha256()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        files.extend(Path(dirpath) / f for f in sorted(filenames) if not f.endswith((".pyc", ".pyo")))
    for f in files:
        h.update(f.relative_to(root).as_posix().encode("utf-8") + b"\0")
        try:
            h.update(hashlib.sha256(f.read_bytes()).digest())
        except OSError:
            h.update(b"unreadable")
    return h.hexdigest()


def fixture_info(name: str, root: Path, *, sha: str | None = None) -> dict[str, Any]:
    """フィクスチャの記録。``sha`` はコミット SHA（``bench/fixtures.toml`` の値）。無ければ内容ハッシュを入れる。

    リポジトリ内のフィクスチャ（F-POLY）は、含むリポジトリの ``HEAD:<相対パス>`` の tree SHA も添える。
    """
    info: dict[str, Any] = {"name": name, "root": str(root)}
    if sha:
        info["sha"] = sha
    info["content_sha256"] = tree_digest(root)
    try:
        rel = root.resolve().relative_to(REPO).as_posix()
    except ValueError:
        rel = None
    if rel:
        info["git_tree"] = _run(["git", "rev-parse", f"HEAD:{rel}" if rel != "." else "HEAD^{tree}"], cwd=REPO)
    return info


def tier1_files(root: Path, exts: Iterable[str] = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs")) -> list[str]:
    """``root`` 配下の Tier 1 ファイルの相対パス（``/`` 区切り・昇順）。``SKIP_DIRS`` と 256 KB 超は除く。"""
    ext_set = tuple(exts)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            if not f.endswith(ext_set) or f.endswith(".d.ts"):
                continue
            p = Path(dirpath) / f
            try:
                if p.stat().st_size > 256 * 1024:
                    continue
            except OSError:
                continue
            out.append(p.relative_to(root).as_posix())
    return out


# --- 書き出し -------------------------------------------------------------------------------


def write_result(kind: str, payload: dict[str, Any], *, stamp: str | None = None) -> Path:
    """``bench/results/<日時>-<OS>-<kind>.json`` に書く。共通の見出し（種別・日時・機材・Lucianke のコミット）を前置する。"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = stamp or now_stamp()
    doc: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "finished_at": iso_now(),
        "lucianke": repo_info(),
        "machine": machine_info(),
        **payload,
    }
    path = RESULTS_DIR / f"{stamp}-{os_name()}-{kind}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return path


# --- 負荷の記録 ------------------------------------------------------------------------------


def _cpu_ticks() -> tuple[int, int] | None:
    """システム全体の ``(アイドル, 合計)`` の累積 tick。取れなければ None。"""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None

            def val(ft: Any) -> int:
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            return val(idle), val(kernel) + val(user)  # kernel にアイドルが含まれる
        first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        nums = [int(x) for x in first[1:]]
        return nums[3] + (nums[4] if len(nums) > 4 else 0), sum(nums)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


class CpuLoadMeter:
    """測定区間のシステム全体の CPU 使用率（他の役のテストが並行しているかの目安）。``start()`` ～ ``stop()``。"""

    def __init__(self) -> None:
        self._t0 = _cpu_ticks()

    def stop(self) -> dict[str, Any]:
        t1 = _cpu_ticks()
        out: dict[str, Any] = {"cpu_busy_pct": None}
        if self._t0 and t1 and t1[1] > self._t0[1]:
            out["cpu_busy_pct"] = round(100.0 * (1.0 - (t1[0] - self._t0[0]) / (t1[1] - self._t0[1])), 1)
        try:
            out["loadavg_1m"] = round(os.getloadavg()[0], 2)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            out["loadavg_1m"] = None
        return out
