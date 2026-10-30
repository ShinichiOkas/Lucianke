"""テスト全体の見張り（``tests/conftest.py``。BUG-0076）の試験。

見張りを入れた小さなテスト一式を別の pytest で流し、次を確かめる:

* セットアップが途中で落ちて書き手のスレッドを残した試験は、その試験自身の teardown のエラーになる（理由に「leaked threads」）。
* 後に続く無関係な試験（書き手のスレッドが無いことを全体で数える）は巻き添えで落ちない（見張りが残りを止める）。
* module スコープのフィクスチャが持つ書き手は、そのフィクスチャが片づくまで「残した」とみなさない。片づけずに残したときは
  フィクスチャの名前でエラーになる。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "tests" / "conftest.py"

LEAKY = """
import threading

import pytest

from lucianke.index.writer import IndexWriter


def writer_threads():
    return [t for t in threading.enumerate() if t.name == "lucianke-index-writer" and t.is_alive()]


@pytest.fixture
def leaky(tmp_path):
    w = IndexWriter(tmp_path / "paad.sqlite")
    raise TimeoutError("the LSP did not become ready")  # 後始末の前に落ちる（w を閉じない）
    yield w


@pytest.fixture
def tidy(tmp_path):
    w = IndexWriter(tmp_path / "paad.sqlite")
    try:
        yield w
    finally:
        w.close()


def test_a_offender(leaky):
    pass


def test_b_bystander():
    assert writer_threads() == []


def test_c_tidy(tidy):
    assert len(writer_threads()) == 1


def test_d_body_leaks(tmp_path):
    IndexWriter(tmp_path / "paad.sqlite")


def test_e_bystander():
    assert writer_threads() == []
"""

SCOPED = """
import pytest

from lucianke.index.writer import IndexWriter


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    w = IndexWriter(tmp_path_factory.mktemp("m") / "paad.sqlite")
    yield w
    w.close()


def test_m1(shared):
    assert shared.data_version() == 0


def test_m2(shared):
    assert shared.data_version() == 0
"""

SCOPED_LEAK = """
import pytest

from lucianke.index.writer import IndexWriter


@pytest.fixture(scope="module")
def forgotten(tmp_path_factory):
    return IndexWriter(tmp_path_factory.mktemp("f") / "paad.sqlite")  # 閉じない


def test_f1(forgotten):
    pass


def test_f2(forgotten):
    pass
"""


def run(tmp_path: Path) -> dict[str, list[str]]:
    (tmp_path / "pytest.ini").write_bytes(b"[pytest]\n")
    (tmp_path / "conftest.py").write_bytes(GUARD.read_bytes())
    (tmp_path / "test_leaky.py").write_bytes(LEAKY.encode("utf-8"))
    (tmp_path / "test_scoped.py").write_bytes(SCOPED.encode("utf-8"))
    (tmp_path / "test_scoped_leak.py").write_bytes(SCOPED_LEAK.encode("utf-8"))
    temp = (
        tmp_path / "temp"
    )  # 入れ子の pytest の一時ディレクトリ（赤い報告の記録〈BUG-0088〉で本物の記録を汚さない）
    temp.mkdir()
    proc = subprocess.run(  # noqa: S603  固定の引数列
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA", "-q", str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(
            os.environ, PYTHONUTF8="1", COLUMNS="2000", TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp)
        ),  # 要約の行を切らせない
        timeout=300,
        check=False,
    )
    out: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        word, _, rest = line.partition(" ")
        if word in {"PASSED", "FAILED", "ERROR"} and "::" in rest:
            test = rest.split("::", 1)[1].split(" ", 1)[0]
            out.setdefault(test, []).append(f"{word} {rest}")
    assert out, proc.stdout + proc.stderr
    return out


def test_the_guard_blames_the_test_that_leaked_and_spares_the_others(tmp_path: Path) -> None:
    out = run(tmp_path)
    offender = " | ".join(out["test_a_offender"])
    assert "ERROR" in offender and "leaked threads" in offender and "this test left 1" in offender, out
    assert "stopped by the guard" in offender, out
    body = " | ".join(out["test_d_body_leaks"])
    assert body.startswith("PASSED") and "ERROR" in body and "leaked threads" in body, out
    for clean in ("test_b_bystander", "test_c_tidy", "test_e_bystander", "test_m1", "test_m2", "test_f1"):
        assert out[clean] == [out[clean][0]] and out[clean][0].startswith("PASSED"), (clean, out)
    forgotten = " | ".join(out["test_f2"])
    assert forgotten.startswith("PASSED") and "ERROR" in forgotten, out
    assert "fixture 'forgotten' (scope=module" in forgotten and "stopped by the guard" in forgotten, out
