"""``lucianke doctor``（設計書 §11.2、R-10）。PATH を差し替えて有無を切り替え、``stdout`` に 1 バイトも出ないことを確かめる。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lucianke.cli import doctor

_WIN = sys.platform == "win32"


def make_exe(directory: Path, name: str) -> Path:
    """``directory`` に ``name`` という実行ファイル（中身は空でよい。起動はテスト側の ``run`` が肩代わりする）を作る。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".exe" if _WIN else name)
    path.write_bytes(b"")
    if not _WIN:
        path.chmod(0o755)
    return path


def fake_run(table: dict[str, str]):
    """``--version`` の出力を実行ファイルの basename（拡張子なし）で返す差し替え。"""

    def run(argv):
        return table.get(Path(argv[0]).stem)

    return run


def by_name(checks, name):
    (found,) = [c for c in checks if c.name == name]
    return found


@pytest.fixture
def empty_env(tmp_path: Path) -> dict[str, str]:
    """PATH が空ディレクトリだけ。LSP・Node・ctags・git が見つからない環境。"""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    return {"PATH": str(empty)}


def test_find_executable_follows_path(tmp_path: Path):
    bindir = tmp_path / "bin"
    exe = make_exe(bindir, "node")
    assert doctor.find_executable("node", {"PATH": str(bindir)}) == str(exe)
    assert doctor.find_executable("node", {"PATH": str(tmp_path / "other")}) is None
    assert doctor.find_executable("node", {"PATH": ""}) is None
    assert doctor.find_executable("node", {}) is None


def test_find_executable_ignores_relative_path_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    make_exe(tmp_path / "rel", "node")
    monkeypatch.chdir(tmp_path)
    assert doctor.find_executable("node", {"PATH": "rel"}) is None


def test_tools_missing_are_not_failures(empty_env):
    checks = doctor.check_tools(empty_env, fake_run({}))
    assert {c.name: c.status for c in checks} == {"node": "info", "ctags": "info", "git": "info"}
    assert doctor.exit_code(checks) == 0


def test_tools_found_report_version(tmp_path: Path):
    bindir = tmp_path / "bin"
    for n in ("node", "ctags", "git"):
        make_exe(bindir, n)
    run = fake_run(
        {"node": "v22.1.0", "ctags": "Universal Ctags 6.1.0, Copyright", "git": "git version 2.45.0"}
    )
    checks = doctor.check_tools({"PATH": str(bindir)}, run)
    assert by_name(checks, "node").status == "ok"
    assert by_name(checks, "node").version == "v22.1.0"
    assert by_name(checks, "ctags").status == "ok"
    assert "Universal Ctags" in by_name(checks, "ctags").detail
    assert by_name(checks, "git").status == "ok"
    assert by_name(checks, "git").path == str(bindir / ("git.exe" if _WIN else "git"))


def test_ctags_that_is_not_universal_is_warned(tmp_path: Path):
    bindir = tmp_path / "bin"
    make_exe(bindir, "ctags")
    checks = doctor.check_tools({"PATH": str(bindir)}, fake_run({"ctags": "Exuberant Ctags 5.8"}))
    assert by_name(checks, "ctags").status == "warn"


def test_node_missing_warns_only_when_a_typescript_server_needs_it(empty_env):
    assert by_name(doctor.check_tools(empty_env, fake_run({}), ts_server_found=True), "node").status == "warn"
    assert (
        by_name(doctor.check_tools(empty_env, fake_run({}), ts_server_found=False), "node").status == "info"
    )


def test_lsp_presence_toggles_with_path(tmp_path: Path, empty_env):
    root = tmp_path / "ws"
    root.mkdir()
    missing, ts_found = doctor.check_lsp(root, empty_env)
    assert not ts_found
    assert by_name(missing, "basedpyright (python)").status == "warn"
    assert by_name(missing, "typescript-language-server (typescript, tsx, javascript)").status == "warn"
    # 代替は warn を重ねない
    assert by_name(missing, "vtsls (typescript, tsx, javascript)").status == "info"
    assert by_name(missing, "gopls (go)").status == "info"  # best effort

    bindir = tmp_path / "bin"
    make_exe(bindir, "basedpyright-langserver")
    make_exe(bindir, "vtsls")
    found, ts_found = doctor.check_lsp(root, {"PATH": str(bindir)})
    assert ts_found
    assert by_name(found, "basedpyright (python)").status == "ok"
    # 基準の basedpyright が見つかれば pyright は要らない
    assert by_name(found, "pyright (python)").status == "info"
    assert by_name(found, "vtsls (typescript, tsx, javascript)").status == "ok"
    # 既定が無くても代替が見つかっていれば warn にしない
    assert by_name(found, "typescript-language-server (typescript, tsx, javascript)").status == "info"


def test_lsp_found_in_workspace_venv(tmp_path: Path, empty_env):
    root = tmp_path / "ws"
    make_exe(root / ".venv" / ("Scripts" if _WIN else "bin"), "ruff")
    checks, _ = doctor.check_lsp(root, empty_env)
    assert by_name(checks, "ruff (python)").status == "ok"


def test_lsp_env_override_to_missing_command_warns(tmp_path: Path, empty_env):
    env = {**empty_env, "LUCIANKE_LSP_RUFF_CMD": str(tmp_path / "nope" / "ruff-x")}
    checks, _ = doctor.check_lsp(tmp_path, env)
    assert by_name(checks, "ruff (python)").status == "warn"
    assert "override" in by_name(checks, "ruff (python)").detail


def test_cache_writable_and_usage(tmp_path: Path):
    cache = tmp_path / "cache"
    (cache / "logs").mkdir(parents=True)
    (cache / "logs" / "a.log").write_bytes(b"x" * 2048)
    (cache / "abc123").mkdir()
    (cache / "abc123" / "paad.sqlite").write_bytes(b"y" * 5000)
    checks = doctor.check_cache(cache)
    assert by_name(checks, "writable").status == "ok"
    usage = by_name(checks, "usage")
    assert usage.status == "ok"
    assert "2 files" in usage.detail
    assert "abc123" in usage.detail and "logs" in usage.detail
    assert list(cache.glob(".doctor-*")) == []  # 書き込みの試験片を残さない


def test_cache_over_limit_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "big").write_bytes(b"z" * 4096)
    monkeypatch.setattr(doctor, "CACHE_WARN_BYTES", 1024)
    assert by_name(doctor.check_cache(cache), "usage").status == "warn"


def test_cache_unwritable_is_a_failure(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a regular file where a directory is needed")
    checks = doctor.check_cache(blocker / "cache")  # ファイルの下にはディレクトリを作れない（OS を問わず）
    assert by_name(checks, "writable").status == "fail"
    assert doctor.exit_code(checks) == 1


def test_required_missing_exits_1_and_optional_missing_exits_0(tmp_path: Path, empty_env):
    ok_checks = doctor.collect(
        cache_root=tmp_path / "cache", env=empty_env, root=tmp_path, run=fake_run({}), selftest=False
    )
    assert doctor.summarize(ok_checks)["fail"] == 0
    assert doctor.summarize(ok_checks)["warn"] >= 1  # LSP が無い
    assert doctor.exit_code(ok_checks) == 0
    assert doctor.exit_code(ok_checks, strict=True) == 1  # --strict は warn も落とす

    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad = doctor.collect(
        cache_root=blocker / "cache", env=empty_env, root=tmp_path, run=fake_run({}), selftest=False
    )
    assert doctor.exit_code(bad) == 1


def test_missing_required_dependency_fails(monkeypatch: pytest.MonkeyPatch):
    real = doctor._dist_version

    def fake(name: str):
        return None if name == "watchfiles" else real(name)

    monkeypatch.setattr(doctor, "_dist_version", fake)
    checks = doctor.check_dependencies()
    required = by_name(checks, "required")
    assert required.status == "fail"
    assert "watchfiles" in required.detail
    assert doctor.exit_code(checks) == 1


def test_missing_extras_in_an_exe_are_info_without_a_pip_hint(monkeypatch: pytest.MonkeyPatch):
    # exe には extras を後から pip で足せない。欠けた extras は「同梱していない」の info にする（warn・pip の案内を出さない）
    real = doctor._dist_version

    def fake(name: str):
        return None if name in ("z3-solver", "sqlglot") else real(name)

    monkeypatch.setattr(doctor, "_dist_version", fake)
    frozen = by_name(doctor.check_dependencies(frozen=True), "extra:verify")
    assert frozen.status == "info" and "not bundled in this exe" in frozen.detail and "pip" not in frozen.detail
    source = by_name(doctor.check_dependencies(frozen=False), "extra:verify")
    assert source.status == "warn" and "lucianke[verify]" in source.detail


def test_stdout_isolation_self_test_passes():
    (check,) = doctor.check_stdout_isolation()
    assert check.status == "ok", check.detail


def test_selftest_argv_in_an_exe_starts_the_exe_itself_with_the_hidden_subcommand():
    # exe（PyInstaller）には -c を受ける Python が無い。exe 自身を隠しサブコマンドで起動する
    assert doctor.selftest_argv(frozen=True) == [sys.executable, "_doctor-selftest", doctor.SELFTEST_MARKER]
    assert doctor.selftest_argv(frozen=False)[1:3] == ["-I", "-c"]


def test_hidden_selftest_subcommand_keeps_stdout_pure():
    # exe が通る経路（``lucianke _doctor-selftest <marker>``）を、ソースの入口で同じように通す
    cp = subprocess.run(
        [sys.executable, "-m", "lucianke", "_doctor-selftest", doctor.SELFTEST_MARKER],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=60, check=False,
    )  # fmt: skip
    assert cp.returncode == 0, cp.stderr[-500:]
    assert cp.stdout == (doctor.SELFTEST_MARKER + "\n").encode()
    assert b"doctor-noise-print" in cp.stderr and b"doctor-noise-fd1" in cp.stderr


def test_main_writes_only_to_the_given_stream_and_json_parses(tmp_path: Path, empty_env, capfd):
    buf = io.StringIO()
    code = doctor.main(
        ["--json", "--root", str(tmp_path)],
        cache_root=tmp_path / "cache", env=empty_env, run=fake_run({}), selftest=False, stream=buf,
    )  # fmt: skip
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True
    assert payload["summary"]["fail"] == 0
    assert {c["group"] for c in payload["checks"]} >= {"runtime", "dependencies", "cache", "tools", "lsp"}
    captured = capfd.readouterr()
    assert captured.out == "" and captured.err == ""


def test_text_table_lists_every_group(tmp_path: Path, empty_env):
    buf = io.StringIO()
    doctor.main(
        [], cache_root=tmp_path / "cache", env=empty_env, run=fake_run({}), selftest=False, stream=buf
    )
    text = buf.getvalue()
    for group in ("[runtime]", "[dependencies]", "[cache]", "[tools]", "[lsp]"):
        assert group in text
    assert "result:" in text


def test_help_goes_to_stderr_not_stdout(tmp_path: Path, capfd):
    with pytest.raises(SystemExit) as e:
        doctor.main(["--help"], cache_root=tmp_path)
    assert e.value.code == 0
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "--json" in captured.err


def test_bad_option_exits_2(tmp_path: Path):
    with pytest.raises(SystemExit) as e:
        doctor.main(["--nope"], cache_root=tmp_path)
    assert e.value.code == 2


def _run_cli(
    args: list[str], tmp_path: Path, *, cache: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env["LUCIANKE_CACHE_DIR"] = str(cache if cache is not None else tmp_path / "cache")
    env["PYTHONUTF8"] = "1"
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "lucianke", "doctor", *args],
        capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=180, check=False,
    )  # fmt: skip


def test_cli_doctor_json_full_run_keeps_stdout_empty(tmp_path: Path):
    """実物の経路（子プロセスの自己診断つき）。stdout は 0 バイト、JSON は stderr、終了コードは ``ok`` と一致。"""
    cp = _run_cli(["--json"], tmp_path)
    assert cp.stdout == b""
    payload = json.loads(cp.stderr.decode("ascii"))  # JSON は ASCII のみ（stderr のエンコードに依らない）
    assert cp.returncode == (0 if payload["ok"] else 1)
    isolation = [c for c in payload["checks"] if c["name"] == "stdout isolation"]
    assert isolation and isolation[0]["status"] == "ok", isolation


def test_cli_doctor_table_goes_to_stderr(tmp_path: Path):
    cp = _run_cli(["--no-selftest"], tmp_path)
    assert cp.stdout == b""
    err = cp.stderr.decode("utf-8", "replace")
    assert "[runtime]" in err and "[lsp]" in err and "result:" in err
    assert "[stdout]" not in err  # --no-selftest
    assert cp.returncode == (1 if "FAIL" in err else 0)


def test_cli_doctor_cache_failure_exits_1_with_clean_stdout(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    cp = _run_cli(["--json", "--no-selftest"], tmp_path, cache=blocker / "cache")
    assert cp.stdout == b""
    assert cp.returncode == 1
    assert json.loads(cp.stderr.decode("ascii"))["ok"] is False


def test_unknown_subcommands_still_exit_2(tmp_path: Path):
    cp = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "lucianke", "frobnicate"], capture_output=True, stdin=subprocess.DEVNULL, timeout=60, check=False
    )  # fmt: skip
    assert cp.stdout == b""
    assert cp.returncode == 2
