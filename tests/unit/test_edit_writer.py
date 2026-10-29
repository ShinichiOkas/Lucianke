"""edit/writer.py と edit/lock.py: 原子的書き込み・ジャーナル・ロールバック・書き込みロック。

設計書 §7.7.5、仕様書 §10.6、§10.10（EP-1・EP-2・EP-3・EP-4・EP-6・EP-9）、S-10（Windows の共有違反）。
時間に頼る検査は「起きないことの確認」だけに限る（遅くても落ちない）。再試行は ``time.sleep`` の呼び出し回数で同期する。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import EDIT_LOCK_WAIT_MS, JOURNAL_KEEP
from lucianke.edit import writer
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.writer import (
    TMP_PREFIX,
    FileWrite,
    prune_journal,
    rollback,
    write_all,
)

IS_WINDOWS = sys.platform == "win32"
EDIT_ID = "ed_0001"

CRLF_BOM = b"\xef\xbb\xbfline one\r\nline two\r\nno trailing newline"
REAL_REPLACE = os.replace


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


@pytest.fixture
def journal(tmp_path: Path) -> Path:
    return tmp_path / "journal"


def make_files(ws: Path, contents: dict[str, bytes]) -> dict[str, Path]:
    out = {}
    for name, data in contents.items():
        p = ws / name
        p.write_bytes(data)
        out[name] = p
    return out


def fw(path: Path, new: bytes, expected: bytes | None) -> FileWrite:
    return FileWrite(path, new, sha(expected) if expected is not None else None)


def tmp_leftovers(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.name.startswith(TMP_PREFIX)]


def read_manifest(journal: Path, edit_id: str = EDIT_ID) -> dict:
    return json.loads((journal / edit_id / "manifest.json").read_text(encoding="utf-8"))


def fail_replace_once(
    monkeypatch: pytest.MonkeyPatch, target: Path, exc: BaseException, *, nth: int = 1
) -> None:
    """``target`` への ``os.replace`` の ``nth`` 回目だけ ``exc`` で失敗させる。それ以外は本物に任せる。"""
    calls = {"n": 0}

    def fake(src, dst, *args, **kwargs):
        if Path(dst) == target:
            calls["n"] += 1
            if calls["n"] == nth:
                raise exc
        return REAL_REPLACE(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fake)


# --- 書き込みの基本（EP-1、EP-6） -----------------------------------------------------------


def test_write_one_file_and_journal_keeps_original(ws: Path, journal: Path):
    old, new = b"old\n", b"new content\n"
    f = make_files(ws, {"a.txt": old})["a.txt"]
    (result,) = write_all([fw(f, new, old)], journal_dir=journal, edit_id=EDIT_ID)

    assert f.read_bytes() == new
    assert result.abs_path == f
    assert result.sha256_before == sha(old)
    assert result.sha256_after == sha(f.read_bytes()) == sha(new)  # EP-1
    assert result.size_after == len(new)
    assert not result.created
    manifest = read_manifest(journal)
    assert manifest["state"] == "committed"
    (entry,) = manifest["files"]
    assert entry["sha256_before"] == sha(old) and entry["sha256_after"] == sha(new)
    assert entry["path"] == str(f) and entry["mode"] is not None
    assert (journal / EDIT_ID / entry["backup"]).read_bytes() == old
    assert tmp_leftovers(ws) == []


def test_write_three_files_returns_results_in_input_order(ws: Path, journal: Path):
    olds = {"a.txt": b"A\n", "b.txt": b"B\r\n", "c.txt": b"\xef\xbb\xbfC"}
    news = {"a.txt": b"A2\n", "b.txt": b"B2\r\n", "c.txt": b"\xef\xbb\xbfC2\n"}
    files = make_files(ws, olds)
    changes = [fw(files[n], news[n], olds[n]) for n in ("c.txt", "a.txt", "b.txt")]

    results = write_all(changes, journal_dir=journal, edit_id=EDIT_ID)

    assert [r.abs_path.name for r in results] == ["c.txt", "a.txt", "b.txt"]
    for r in results:
        assert r.abs_path.read_bytes() == news[r.abs_path.name]
        assert r.sha256_after == sha(news[r.abs_path.name])  # EP-1
        assert r.sha256_before == sha(olds[r.abs_path.name])
    manifest = read_manifest(journal)
    backups = {
        Path(e["path"]).name: (journal / EDIT_ID / e["backup"]).read_bytes() for e in manifest["files"]
    }
    assert backups == {n: olds[n] for n in olds}
    assert tmp_leftovers(ws) == []


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(CRLF_BOM, id="crlf-bom-no-trailing-newline"),
        pytest.param(b"lf\nonly\n", id="lf"),
        pytest.param(b"cr\ronly\r", id="cr"),
        pytest.param(b"mixed\r\nand\nand\r\xe6\x97\xa5\xe6\x9c\xac", id="mixed-japanese"),
        pytest.param(b"", id="empty"),
        pytest.param(b"nul\x00inside\n", id="nul"),
    ],
)
def test_identity_write_is_byte_exact(ws: Path, journal: Path, data: bytes):
    """EP-6 の下支え: 渡したバイト列がそのまま（改行・BOM・末尾改行を変えずに）ディスクに載る。"""
    f = make_files(ws, {"a.txt": data})["a.txt"]
    (result,) = write_all([fw(f, data, data)], journal_dir=journal, edit_id=EDIT_ID)
    assert f.read_bytes() == data
    assert result.sha256_after == sha(data)


def test_new_bytes_are_written_verbatim_not_translated(ws: Path, journal: Path):
    f = make_files(ws, {"a.txt": b"x"})["a.txt"]
    new = b"\xef\xbb\xbfa\nb\r\nc\rd"
    write_all([fw(f, new, b"x")], journal_dir=journal, edit_id=EDIT_ID)
    assert f.read_bytes() == new


def test_create_new_file_and_rollback_deletes_it(ws: Path, journal: Path):
    existing = make_files(ws, {"a.txt": b"A"})["a.txt"]
    created = ws / "new.txt"
    results = write_all(
        [fw(existing, b"A2", b"A"), FileWrite(created, b"fresh", None)], journal_dir=journal, edit_id=EDIT_ID
    )
    assert created.read_bytes() == b"fresh"
    assert [r.created for r in results] == [False, True]
    assert results[1].sha256_before is None

    restored = rollback(journal, EDIT_ID)

    assert existing.read_bytes() == b"A"
    assert not created.exists()
    assert set(restored) == {existing, created}


def test_new_file_that_already_exists_is_a_precondition_failure(ws: Path, journal: Path):
    f = make_files(ws, {"a.txt": b"other"})["a.txt"]
    with pytest.raises(LuciankeError) as ei:
        write_all([FileWrite(f, b"x", None)], journal_dir=journal, edit_id=EDIT_ID)
    assert ei.value.code is ErrorCode.E_PRECONDITION_FAILED
    assert f.read_bytes() == b"other"


@pytest.mark.skipif(IS_WINDOWS, reason="実行ビットは POSIX のみ保持する（設計書 §9.5）")
def test_posix_mode_is_preserved(ws: Path, journal: Path):
    f = make_files(ws, {"run.sh": b"#!/bin/sh\n"})["run.sh"]
    f.chmod(0o755)
    write_all([fw(f, b"#!/bin/sh\necho hi\n", b"#!/bin/sh\n")], journal_dir=journal, edit_id=EDIT_ID)
    assert stat.S_IMODE(f.stat().st_mode) == 0o755
    assert read_manifest(journal)["files"][0]["mode"] == 0o755


def test_empty_changes_is_a_no_op(journal: Path):
    assert write_all([], journal_dir=journal, edit_id=EDIT_ID) == []
    assert not journal.exists()


# --- 失敗では何も変わらない（EP-2、EP-3、EP-4） -----------------------------------------------


def test_expected_sha_mismatch_changes_nothing(ws: Path, journal: Path):
    files = make_files(ws, {"a.txt": b"A", "b.txt": b"B"})
    changes = [fw(files["a.txt"], b"A2", b"A"), fw(files["b.txt"], b"B2", b"not what is on disk")]

    with pytest.raises(LuciankeError) as ei:
        write_all(changes, journal_dir=journal, edit_id=EDIT_ID)

    err = ei.value
    assert err.code is ErrorCode.E_PRECONDITION_FAILED  # EP-3
    assert err.details["path"] == str(files["b.txt"])
    assert err.details["expected"] == sha(b"not what is on disk") and err.details["actual"] == sha(b"B")
    assert files["a.txt"].read_bytes() == b"A" and files["b.txt"].read_bytes() == b"B"  # EP-2
    assert tmp_leftovers(ws) == []
    assert not (journal / EDIT_ID).exists()


def test_missing_file_with_expected_sha_is_a_precondition_failure(ws: Path, journal: Path):
    with pytest.raises(LuciankeError) as ei:
        write_all([fw(ws / "gone.txt", b"x", b"was here")], journal_dir=journal, edit_id=EDIT_ID)
    assert ei.value.code is ErrorCode.E_PRECONDITION_FAILED
    assert ei.value.details["actual"] is None
    assert not (ws / "gone.txt").exists()


def test_precheck_exception_renames_nothing(ws: Path, journal: Path):
    olds = {"a.txt": b"A", "b.txt": b"B", "c.txt": b"C"}
    files = make_files(ws, olds)
    changes = [fw(files[n], olds[n] + b"2", olds[n]) for n in olds]
    seen: list[str] = []

    def precheck(path: Path) -> None:
        seen.append(path.name)
        if path.name == "c.txt":
            raise LuciankeError(ErrorCode.E_PRECONDITION_FAILED, "stale", details={"path": str(path)})

    with pytest.raises(LuciankeError) as ei:
        write_all(changes, journal_dir=journal, edit_id=EDIT_ID, precheck=precheck)

    assert ei.value.code is ErrorCode.E_PRECONDITION_FAILED and ei.value.message == "stale"
    assert seen == ["a.txt", "b.txt", "c.txt"]
    for n, data in olds.items():
        assert files[n].read_bytes() == data  # 先に検査した a・b も改名されていない
    assert tmp_leftovers(ws) == []
    assert not (journal / EDIT_ID).exists()


def test_precheck_runs_once_per_file_and_receives_abs_path(ws: Path, journal: Path):
    f = make_files(ws, {"a.txt": b"A"})["a.txt"]
    seen: list[Path] = []
    write_all([fw(f, b"B", b"A")], journal_dir=journal, edit_id=EDIT_ID, precheck=seen.append)
    assert seen == [f]


def test_external_change_between_journal_and_rename_is_kept(ws: Path, journal: Path):
    """EP-4: 解決のあとに外部が書き換えたら E_PRECONDITION_FAILED。外部の変更は残る。"""
    files = make_files(ws, {"a.txt": b"A", "b.txt": b"B"})
    changes = [fw(files["a.txt"], b"A2", b"A"), fw(files["b.txt"], b"B2", b"B")]

    def precheck(path: Path) -> None:
        if path.name == "b.txt":
            path.write_bytes(b"edited by someone else")

    with pytest.raises(LuciankeError) as ei:
        write_all(changes, journal_dir=journal, edit_id=EDIT_ID, precheck=precheck)

    assert ei.value.code is ErrorCode.E_PRECONDITION_FAILED
    assert files["a.txt"].read_bytes() == b"A"
    assert files["b.txt"].read_bytes() == b"edited by someone else"
    assert tmp_leftovers(ws) == []


def test_existing_journal_for_same_edit_id_is_left_alone(ws: Path, journal: Path):
    f = make_files(ws, {"a.txt": b"A"})["a.txt"]
    write_all([fw(f, b"B", b"A")], journal_dir=journal, edit_id=EDIT_ID)
    before = (journal / EDIT_ID / "manifest.json").read_bytes()

    with pytest.raises(LuciankeError) as ei:
        write_all([fw(f, b"C", b"B")], journal_dir=journal, edit_id=EDIT_ID)

    assert ei.value.code is ErrorCode.E_INTERNAL and ei.value.details["rolled_back"] is True
    assert f.read_bytes() == b"B"
    assert (journal / EDIT_ID / "manifest.json").read_bytes() == before


def test_caller_bugs_raise_value_error(ws: Path, journal: Path):
    f = make_files(ws, {"a.txt": b"A"})["a.txt"]
    with pytest.raises(ValueError, match="duplicate"):
        write_all([fw(f, b"1", b"A"), fw(f, b"2", b"A")], journal_dir=journal, edit_id=EDIT_ID)
    for bad in ("", "../x", "a/b", "a\\b", ".."):
        with pytest.raises(ValueError, match="edit_id"):
            write_all([fw(f, b"1", b"A")], journal_dir=journal, edit_id=bad)
    assert f.read_bytes() == b"A"


# --- 改名の失敗とロールバック（EP-9） ---------------------------------------------------------


def test_rename_failure_on_second_of_three_restores_everything(
    ws: Path, journal: Path, monkeypatch: pytest.MonkeyPatch
):
    olds = {"a.txt": b"A\r\n", "b.txt": b"\xef\xbb\xbfB", "c.txt": b"C\n"}
    files = make_files(ws, olds)
    changes = [fw(files[n], b"new-" + n.encode(), olds[n]) for n in olds]
    fail_replace_once(monkeypatch, files["b.txt"], OSError(5, "injected rename failure"))

    with pytest.raises(LuciankeError) as ei:
        write_all(changes, journal_dir=journal, edit_id=EDIT_ID)

    err = ei.value
    assert err.code is ErrorCode.E_INTERNAL
    assert err.details["rolled_back"] is True and err.details["log_ref"] == EDIT_ID
    for n, data in olds.items():
        assert files[n].read_bytes() == data  # EP-9
    assert tmp_leftovers(ws) == []
    assert read_manifest(journal)["state"] == "rolled_back"


def test_rename_failure_on_last_file_restores_the_renamed_ones(
    ws: Path, journal: Path, monkeypatch: pytest.MonkeyPatch
):
    olds = {"a.txt": b"A", "b.txt": b"B", "c.txt": b"C"}
    files = make_files(ws, olds)
    changes = [fw(files[n], b"new", olds[n]) for n in olds]
    fail_replace_once(monkeypatch, files["c.txt"], OSError(5, "injected"))

    with pytest.raises(LuciankeError) as ei:
        write_all(changes, journal_dir=journal, edit_id=EDIT_ID)

    assert ei.value.details["rolled_back"] is True
    assert {n: files[n].read_bytes() for n in olds} == olds


def test_rename_failure_restores_a_file_created_in_the_same_edit(
    ws: Path, journal: Path, monkeypatch: pytest.MonkeyPatch
):
    a = make_files(ws, {"a.txt": b"A"})["a.txt"]
    created = ws / "new.txt"
    fail_replace_once(monkeypatch, a, OSError(5, "injected"))
    with pytest.raises(LuciankeError) as ei:
        write_all(
            [FileWrite(created, b"fresh", None), fw(a, b"A2", b"A")], journal_dir=journal, edit_id=EDIT_ID
        )
    assert ei.value.details["rolled_back"] is True
    assert not created.exists() and a.read_bytes() == b"A"


def test_failed_rollback_is_reported_and_manual_rollback_recovers(
    ws: Path, journal: Path, monkeypatch: pytest.MonkeyPatch
):
    olds = {"a.txt": b"A", "b.txt": b"B"}
    files = make_files(ws, olds)
    changes = [fw(files[n], b"new-" + n.encode(), olds[n]) for n in olds]
    calls = {"a": 0}

    def fake(src, dst, *args, **kwargs):
        if Path(dst) == files["b.txt"]:
            raise OSError(5, "injected rename failure")
        if Path(dst) == files["a.txt"]:
            calls["a"] += 1
            if calls["a"] == 2:  # 2 回目 = ロールバックでの書き戻し
                raise OSError(5, "injected restore failure")
        return REAL_REPLACE(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fake)
    with pytest.raises(LuciankeError) as ei:
        write_all(changes, journal_dir=journal, edit_id=EDIT_ID)

    err = ei.value
    assert err.code is ErrorCode.E_INTERNAL
    assert err.details["rolled_back"] is False
    assert Path(err.details["journal"]) == journal / EDIT_ID / "manifest.json"
    assert files["a.txt"].read_bytes() == b"new-a.txt"  # 戻せなかった
    assert read_manifest(journal)["state"] == "rollback_failed"
    assert tmp_leftovers(ws) == []

    monkeypatch.setattr(os, "replace", REAL_REPLACE)
    assert rollback(journal, EDIT_ID) == [files["a.txt"]]
    assert {n: files[n].read_bytes() for n in olds} == olds
    assert read_manifest(journal)["state"] == "rolled_back"


def test_failure_while_writing_temp_files_changes_nothing(
    ws: Path, journal: Path, monkeypatch: pytest.MonkeyPatch
):
    olds = {"a.txt": b"A", "b.txt": b"B"}
    files = make_files(ws, olds)
    changes = [fw(files[n], b"new", olds[n]) for n in olds]
    real_write_tmp = writer._write_tmp
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_write_tmp(*args, **kwargs)

    monkeypatch.setattr(writer, "_write_tmp", flaky)
    with pytest.raises(LuciankeError) as ei:
        write_all(changes, journal_dir=journal, edit_id=EDIT_ID)

    assert ei.value.code is ErrorCode.E_INTERNAL and ei.value.details["rolled_back"] is True
    assert {n: files[n].read_bytes() for n in olds} == olds
    assert tmp_leftovers(ws) == []
    assert not (journal / EDIT_ID).exists()


# --- 手動の巻き戻し ------------------------------------------------------------------------


def test_manual_rollback_restores_a_committed_edit(ws: Path, journal: Path):
    olds = {"a.txt": b"A", "b.txt": b"B"}
    files = make_files(ws, olds)
    write_all([fw(files[n], b"new", olds[n]) for n in olds], journal_dir=journal, edit_id=EDIT_ID)

    restored = rollback(journal, EDIT_ID)

    assert {n: files[n].read_bytes() for n in olds} == olds
    assert sorted(p.name for p in restored) == ["a.txt", "b.txt"]
    assert read_manifest(journal)["state"] == "rolled_back"
    assert rollback(journal, EDIT_ID) == []  # 2 回目は何もしない


def test_manual_rollback_refuses_when_file_changed_afterwards(ws: Path, journal: Path):
    files = make_files(ws, {"a.txt": b"A", "b.txt": b"B"})
    write_all(
        [fw(files["a.txt"], b"A2", b"A"), fw(files["b.txt"], b"B2", b"B")],
        journal_dir=journal,
        edit_id=EDIT_ID,
    )
    files["b.txt"].write_bytes(b"someone edited after")

    with pytest.raises(LuciankeError) as ei:
        rollback(journal, EDIT_ID)

    assert ei.value.code is ErrorCode.E_PRECONDITION_FAILED
    assert files["a.txt"].read_bytes() == b"A2" and files["b.txt"].read_bytes() == b"someone edited after"

    assert len(rollback(journal, EDIT_ID, force=True)) == 2
    assert files["a.txt"].read_bytes() == b"A" and files["b.txt"].read_bytes() == b"B"


def test_manual_rollback_of_unknown_edit_id(journal: Path):
    journal.mkdir()
    with pytest.raises(LuciankeError) as ei:
        rollback(journal, "ed_9999")
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details["param"] == "edit_id"


# --- ジャーナルの保持 ----------------------------------------------------------------------


def test_prune_keeps_the_newest_fifty(journal: Path):
    assert JOURNAL_KEEP == 50
    for i in range(1, 52):
        (journal / f"ed_{i:04d}").mkdir(parents=True)
        (journal / f"ed_{i:04d}" / "manifest.json").write_text("{}", encoding="utf-8")

    removed = prune_journal(journal)

    assert removed == ["ed_0001"]
    left = sorted(p.name for p in journal.iterdir())
    assert len(left) == 50 and left[0] == "ed_0002" and left[-1] == "ed_0051"


def test_prune_orders_numerically_past_four_digits(journal: Path):
    for name in ("ed_9998", "ed_9999", "ed_10000", "ed_10001"):
        (journal / name).mkdir(parents=True)
    assert prune_journal(journal, keep=2) == ["ed_9998", "ed_9999"]
    assert sorted(p.name for p in journal.iterdir()) == ["ed_10000", "ed_10001"]


def test_prune_on_missing_dir_and_ignores_files(journal: Path):
    assert prune_journal(journal) == []
    journal.mkdir()
    (journal / "stray.txt").write_text("x", encoding="utf-8")
    (journal / "ed_0001").mkdir()
    assert prune_journal(journal, keep=0) == ["ed_0001"]
    assert (journal / "stray.txt").exists()


def test_write_all_prunes_old_journals(ws: Path, journal: Path):
    f = make_files(ws, {"a.txt": b"0"})["a.txt"]
    prev = b"0"
    for i in range(1, 53):
        new = str(i).encode()
        write_all([fw(f, new, prev)], journal_dir=journal, edit_id=f"ed_{i:04d}")
        prev = new
    names = sorted(p.name for p in journal.iterdir())
    assert len(names) == 50 and names[0] == "ed_0003" and names[-1] == "ed_0052"


# --- Windows の共有違反（S-10） ---------------------------------------------------------------


@contextmanager
def hold_open(path: Path) -> Iterator[Callable[[], None]]:
    """``CreateFileW`` で ``path`` を共有 READ|WRITE（DELETE なし）で開いたままにする。読めるが ``os.replace`` は失敗する（S-10 7b）。"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, share_rw, open_existing, normal = 0x80000000, 0x1 | 0x2, 3, 0x80
    h = k32.CreateFileW(str(path), generic_read, share_rw, None, open_existing, normal, None)
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    closed = False

    def close() -> None:
        nonlocal closed
        if not closed:
            closed = True
            k32.CloseHandle(h)

    try:
        yield close
    finally:
        close()


@pytest.mark.skipif(not IS_WINDOWS, reason="共有違反は Windows のみ（S-10）")
def test_windows_held_handle_fails_after_retries_and_rolls_back(ws: Path, journal: Path):
    olds = {"a.txt": b"A", "b.txt": b"B", "c.txt": b"C"}
    files = make_files(ws, olds)
    changes = [fw(files[n], b"new-" + n.encode(), olds[n]) for n in olds]

    with hold_open(files["b.txt"]), pytest.raises(LuciankeError) as ei:
        write_all(changes, journal_dir=journal, edit_id=EDIT_ID)

    err = ei.value
    assert err.code is ErrorCode.E_INTERNAL
    assert err.details["rolled_back"] is True and err.details["cause"] == "PermissionError"
    assert {n: files[n].read_bytes() for n in olds} == olds  # 1 本目も戻っている
    assert tmp_leftovers(ws) == []


@pytest.mark.skipif(not IS_WINDOWS, reason="共有違反は Windows のみ（S-10）")
def test_windows_retry_succeeds_once_the_handle_is_closed(
    ws: Path, journal: Path, monkeypatch: pytest.MonkeyPatch
):
    """相手が閉じるのを ``time.sleep`` の 3 回目（約 70 ms 後）に合わせる。時計に頼らないので揺れない。"""
    olds = {"a.txt": b"A", "b.txt": b"B"}
    files = make_files(ws, olds)
    changes = [fw(files[n], b"new-" + n.encode(), olds[n]) for n in olds]
    real_sleep = writer.time.sleep
    sleeps: list[float] = []

    with hold_open(files["b.txt"]) as close:

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 3:
                close()
            real_sleep(seconds)

        monkeypatch.setattr(writer.time, "sleep", fake_sleep)
        results = write_all(changes, journal_dir=journal, edit_id=EDIT_ID)

    assert sleeps == [0.01, 0.02, 0.04]  # S-10: 10, 20, 40 ms の間隔で 4 回目の試行が成功
    assert {n: files[n].read_bytes() for n in olds} == {n: b"new-" + n.encode() for n in olds}
    assert [r.sha256_after for r in results] == [sha(b"new-a.txt"), sha(b"new-b.txt")]
    assert tmp_leftovers(ws) == []


@pytest.mark.skipif(not IS_WINDOWS, reason="読み取り専用属性は Windows のみ（S-10 反映 3）")
def test_windows_read_only_target_is_blocked_without_touching_anything(ws: Path, journal: Path):
    files = make_files(ws, {"a.txt": b"A", "ro.txt": b"R"})
    os.chmod(files["ro.txt"], stat.S_IREAD)
    try:
        with pytest.raises(LuciankeError) as ei:
            write_all(
                [fw(files["a.txt"], b"A2", b"A"), fw(files["ro.txt"], b"R2", b"R")],
                journal_dir=journal,
                edit_id=EDIT_ID,
            )
    finally:
        os.chmod(files["ro.txt"], stat.S_IWRITE | stat.S_IREAD)

    assert ei.value.code is ErrorCode.E_MUTATION_BLOCKED
    assert ei.value.details["reason"] == "read_only_file"
    assert files["a.txt"].read_bytes() == b"A" and files["ro.txt"].read_bytes() == b"R"
    assert tmp_leftovers(ws) == []


# --- ワークスペースの書き込みロック -----------------------------------------------------------


async def test_two_acquires_on_one_workspace_are_serialised():
    locks = WorkspaceLocks()
    order: list[str] = []
    a_in, release_a = asyncio.Event(), asyncio.Event()

    async def first() -> None:
        async with locks.acquire("w"):
            order.append("a:in")
            a_in.set()
            await release_a.wait()
            order.append("a:out")

    async def second() -> None:
        async with locks.acquire("w"):
            order.append("b:in")
            order.append("b:out")

    ta = asyncio.create_task(first())
    await a_in.wait()
    tb = asyncio.create_task(second())
    for _ in range(10):
        await asyncio.sleep(0)
    assert order == ["a:in"]  # b は a の解放を待っている
    assert locks.is_locked("w")

    release_a.set()
    await asyncio.gather(ta, tb)
    assert order == ["a:in", "a:out", "b:in", "b:out"]
    assert not locks.is_locked("w")


async def test_wait_beyond_limit_raises_timeout_and_does_not_leak_the_lock():
    locks = WorkspaceLocks()
    a_in, release_a = asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with locks.acquire("w"):
            a_in.set()
            await release_a.wait()

    ta = asyncio.create_task(holder())
    await a_in.wait()

    with pytest.raises(LuciankeError) as ei:
        async with locks.acquire("w", timeout_ms=30):
            pytest.fail("must not acquire while another holder is active")
    assert ei.value.code is ErrorCode.E_TIMEOUT
    assert ei.value.details["workspace_id"] == "w" and ei.value.details["waited_ms"] == 30

    release_a.set()
    await ta
    async with locks.acquire("w", timeout_ms=1000):  # 期限切れの待ち人がロックを握ったままにならない
        assert locks.is_locked("w")


async def test_zero_timeout_fails_at_once_when_held_and_succeeds_when_free():
    locks = WorkspaceLocks()
    async with locks.acquire("w", timeout_ms=0):
        with pytest.raises(LuciankeError) as ei:
            async with locks.acquire("w", timeout_ms=0):
                pass
        assert ei.value.code is ErrorCode.E_TIMEOUT


async def test_default_wait_limit_is_the_spec_constant():
    import inspect

    assert (
        inspect.signature(WorkspaceLocks.acquire).parameters["timeout_ms"].default
        == EDIT_LOCK_WAIT_MS
        == 5000
    )


async def test_different_workspaces_do_not_block_each_other():
    locks = WorkspaceLocks()
    async with locks.acquire("w1"):
        async with locks.acquire("w2", timeout_ms=1000):
            assert locks.is_locked("w1") and locks.is_locked("w2")
        await locks.wait_idle("w2")  # w1 が握られていても w2 の読み取りは待たない


async def test_lock_is_released_when_the_body_raises():
    locks = WorkspaceLocks()
    with pytest.raises(RuntimeError, match="boom"):
        async with locks.acquire("w"):
            raise RuntimeError("boom")
    assert not locks.is_locked("w")
    async with locks.acquire("w", timeout_ms=1000):
        pass


async def test_cancelled_waiter_does_not_leak_the_lock():
    locks = WorkspaceLocks()
    a_in, release_a, b_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with locks.acquire("w"):
            a_in.set()
            await release_a.wait()

    async def waiter() -> None:
        b_waiting.set()
        async with locks.acquire("w"):
            pytest.fail("cancelled waiter must not acquire")

    ta = asyncio.create_task(holder())
    await a_in.wait()
    tb = asyncio.create_task(waiter())
    await b_waiting.wait()
    await asyncio.sleep(0)
    tb.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tb
    release_a.set()
    await ta
    assert not locks.is_locked("w")
    async with locks.acquire("w", timeout_ms=1000):
        pass


async def test_wait_idle_waits_for_release_and_does_not_hold_the_lock():
    locks = WorkspaceLocks()
    await locks.wait_idle("never-seen")  # 未知のワークスペースは即座に返る
    a_in, release_a = asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with locks.acquire("w"):
            a_in.set()
            await release_a.wait()

    ta = asyncio.create_task(holder())
    await a_in.wait()
    reader = asyncio.create_task(locks.wait_idle("w"))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not reader.done()  # 書き込み中は待つ

    release_a.set()
    await asyncio.wait_for(reader, timeout=5)
    await ta
    assert not locks.is_locked("w")
    await locks.wait_idle("w")  # 空いていれば即座に返る
