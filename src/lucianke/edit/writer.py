"""編集の原子的書き込みとジャーナル（設計書 §7.7.5、仕様書 §10.6、§10.10 EP-1・EP-2・EP-6・EP-9、S-10）。

``write_all`` は複数ファイルを「全部書く or 何も書かない」で書く。手順:

1. **ジャーナル**: 各ファイルの書き込み前の内容を ``<journal_dir>/<edit_id>/files/<連番>`` に退避し、``manifest.json``
   （元パス・SHA-256・モード）とともに ``fsync`` する。このとき ``expected_sha256`` と照合する（不一致なら何も書かない）。
2. **一時ファイル**: 全ファイルの新内容を **対象と同じディレクトリ** の一時ファイルに書き、``fsync`` し、閉じる。
3. **直前照合**: どれかを改名する前に、全ファイルについて ``precheck(abs_path)`` を呼び、現在の SHA-256 を
   ``expected_sha256`` と照合する。ここで失敗したら何も改名しない。
4. **改名**: ``os.replace`` を順に行う。Windows の共有違反（S-10: ``winerror`` 5・32）は 10〜160 ms の間隔で再試行する。
   改名全体の上限は 1 s。
5. **失敗時**: 改名済みのファイルをジャーナルから戻し、``E_INTERNAL``（``details.rolled_back: true``）を送出する。

新内容のバイト列はそのまま書く（改行・BOM・末尾改行は呼び手が作る）。一時ファイルは必ずバイナリで書く
（テキストモードだと Windows で ``\\n`` が ``\\r\\n`` に変わる。S-10 項目 9）。POSIX では実行ビットを含むモードを保つ。
Windows では読み取り専用属性を **付けも外しもしない**（改名で対象に移るため。読み取り専用の対象は書かずに拒否する）。

このモジュールの関数は同期でブロックする（``fsync`` と再試行の ``sleep``）。asyncio のループからは
``asyncio.to_thread`` で呼ぶこと。呼び出しはワークスペースの書き込みロック（``edit/lock.py``）の下で行う。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import JOURNAL_KEEP

__all__ = [
    "RENAME_BUDGET_S",
    "RETRY_DELAYS",
    "TMP_PREFIX",
    "FileWrite",
    "WrittenFile",
    "prune_journal",
    "rollback",
    "write_all",
]

_log = logging.getLogger("lucianke.edit.writer")

TMP_PREFIX = ".lucianke-tmp-"  # 一時ファイル名の接頭辞。ファイル監視はこの名前を無視してよい
RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)  # S-10: 初回＋再試行 5 回、合計 310 ms
RETRYABLE_WINERRORS = frozenset({5, 32})  # S-10: 5=アクセス拒否（ハンドル保持）、32=共有違反（tmp 側の保持）
RENAME_BUDGET_S = 1.0  # S-10: 複数ファイルの改名全体の上限。超えたらロールバックする

_IS_WINDOWS = sys.platform == "win32"
_EDIT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_MANIFEST = "manifest.json"


@dataclass(frozen=True)
class FileWrite:
    """1 ファイルの書き込み要求。

    ``expected_sha256`` は書き込み前の内容の SHA-256（小文字 16 進 64 桁）。``None`` は「ファイルがまだ存在しない
    （新規作成）」を意味し、存在すれば ``E_PRECONDITION_FAILED``。親ディレクトリは呼び手が作っておく。
    """

    abs_path: Path
    new_bytes: bytes
    expected_sha256: str | None


@dataclass(frozen=True)
class WrittenFile:
    """書き込み結果。``sha256_after`` は改名後にディスクから再計算した値（EP-1）。"""

    abs_path: Path
    sha256_before: str | None  # 新規作成なら None
    sha256_after: str
    size_after: int
    created: bool


@dataclass
class _Entry:
    index: int
    path: Path
    new_bytes: bytes
    existed: bool
    sha_before: str | None
    sha_new: str
    mode: int | None  # stat.S_IMODE。新規作成なら None
    tmp: Path | None = None


# --- 小さな部品 ----------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_if_exists(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _fsync_dir(directory: Path) -> None:
    """POSIX のみ（S-10 反映 6）。Windows では Python からディレクトリを開けないので行わない。"""
    if _IS_WINDOWS:
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _with_retry(op: Callable[[], None], *, deadline: float | None = None) -> None:
    """S-10 の ``atomic_replace`` の型。Windows の ``PermissionError``（winerror 5・32）だけを再試行する。"""
    for delay in (*RETRY_DELAYS, None):
        try:
            op()
            return
        except PermissionError as exc:
            if not _IS_WINDOWS or getattr(exc, "winerror", None) not in RETRYABLE_WINERRORS or delay is None:
                raise
            if deadline is not None and time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)


def _replace(src: Path, dst: Path, *, deadline: float | None = None) -> None:
    # ``os.replace`` は呼ぶたびにモジュール属性として引く（テストが差し替える）
    _with_retry(lambda: os.replace(src, dst), deadline=deadline)


def _unlink_quiet(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("could not remove temp file %s: %s", path.name, exc)


def _write_tmp(directory: Path, edit_id: str, data: bytes, mode: int | None) -> Path:
    """``directory`` に一時ファイルを作って書き、``fsync`` して **閉じてから** 返す（S-10 反映 5）。"""
    for _ in range(20):
        tmp = directory / f"{TMP_PREFIX}{edit_id}-{secrets.randbelow(10**6):06d}"
        try:
            f = open(tmp, "xb")  # noqa: SIM115  バイナリ固定。"w" だと \n が \r\n に変わる
        except FileExistsError:
            continue
        try:
            with f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if mode is not None and not _IS_WINDOWS:
                os.chmod(tmp, mode)
        except BaseException:
            _unlink_quiet(tmp)
            raise
        return tmp
    raise FileExistsError(f"could not create a unique temp file in {directory}")


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    """``manifest.json`` を原子的に書く（一時ファイル → ``fsync`` → 改名）。"""
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    with open(tmp, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    try:
        _replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise


def _check_edit_id(edit_id: str) -> None:
    if not _EDIT_ID_RE.fullmatch(edit_id):
        raise ValueError(f"invalid edit_id: {edit_id!r}")


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _internal(
    message: str, exc: BaseException, *, edit_id: str, rolled_back: bool, journal: Path | None = None
) -> LuciankeError:
    details: dict[str, Any] = {"rolled_back": rolled_back, "log_ref": edit_id, "cause": type(exc).__name__}
    if journal is not None and not rolled_back:
        details["journal"] = str(journal)
    return LuciankeError(ErrorCode.E_INTERNAL, message, details=details)


def _precondition_failed(path: Path, expected: str | None, actual: str | None) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_PRECONDITION_FAILED,
        f"file changed since it was read: {path.name}",
        details={"path": str(path), "expected": expected, "actual": actual},
    )


def _check_sha(path: Path, expected: str | None, actual: str | None) -> None:
    if actual != expected:
        raise _precondition_failed(path, expected, actual)


def _is_read_only(path: Path) -> bool:
    """S-10 反映 3: 読み取り専用は再試行しても直らない（開かれている場合と区別できない）ので書かずに拒否する。"""
    if sys.platform != "win32":
        return False
    return bool(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)


# --- 書き込み -----------------------------------------------------------------------------


def write_all(
    changes: list[FileWrite],
    *,
    journal_dir: Path,
    edit_id: str,
    precheck: Callable[[Path], None] | None = None,
) -> list[WrittenFile]:
    """全ファイルを書く。成功すれば入力と同じ順の ``WrittenFile`` を返す。

    失敗は 3 通り: ``E_PRECONDITION_FAILED``（``expected_sha256`` 不一致。何も変わらない）、``precheck`` が送出した
    例外（そのまま。何も改名されない）、``E_INTERNAL``（改名中などの失敗。``details.rolled_back`` が戻せたかを示す）。
    ``E_MUTATION_BLOCKED``（``details.reason: "read_only_file"``）は Windows で対象が読み取り専用のとき。
    """
    if not changes:
        return []
    _check_edit_id(edit_id)
    seen: set[str] = set()
    for change in changes:
        key = _norm(change.abs_path)
        if key in seen:
            raise ValueError(f"duplicate path in changes: {change.abs_path}")
        seen.add(key)

    edit_dir = journal_dir / edit_id
    try:
        edit_dir.mkdir(parents=True, exist_ok=False)  # 同じ edit_id のジャーナルを上書き・削除しない
    except OSError as exc:
        raise _internal(
            "Could not create the journal; nothing was changed.", exc, edit_id=edit_id, rolled_back=True
        ) from exc
    entries: list[_Entry] = []
    renamed_any = False
    try:
        try:
            _prepare(changes, edit_dir, edit_id, entries)
        except OSError as exc:
            raise _internal(
                "Could not prepare the write; nothing was changed.", exc, edit_id=edit_id, rolled_back=True
            ) from exc
        _verify_before_rename(entries, precheck)
        renamed_any = True  # ここから先は失敗しても改名済みが有りうる
        results = _rename_all(entries, edit_dir, edit_id)
    except BaseException:
        if not renamed_any:
            shutil.rmtree(edit_dir, ignore_errors=True)  # 何も変えていない編集のジャーナルは残さない
        raise
    finally:
        for e in entries:
            _unlink_quiet(e.tmp)
    try:
        prune_journal(journal_dir)
    except OSError as exc:  # 保持の整理は書き込みの成否に関係しない
        _log.warning("could not prune journal: %s", exc)
    return results


def _prepare(changes: list[FileWrite], edit_dir: Path, edit_id: str, entries: list[_Entry]) -> None:
    """ジャーナルへの退避（手順 1）と一時ファイルの作成（手順 2）。ここまでは対象ファイルを変えない。"""
    files_dir = edit_dir / "files"
    files_dir.mkdir()
    for i, change in enumerate(changes):
        old = _read_if_exists(change.abs_path)
        sha_before = _sha256(old) if old is not None else None
        _check_sha(change.abs_path, change.expected_sha256, sha_before)
        mode: int | None = None
        if old is not None:
            if _is_read_only(change.abs_path):
                raise LuciankeError(
                    ErrorCode.E_MUTATION_BLOCKED,
                    f"read-only file: {change.abs_path.name}",
                    details={"reason": "read_only_file", "path": str(change.abs_path)},
                )
            mode = stat.S_IMODE(change.abs_path.stat().st_mode)
            with open(files_dir / f"{i:04d}", "xb") as f:
                f.write(old)
                f.flush()
                os.fsync(f.fileno())
        entries.append(
            _Entry(
                i,
                change.abs_path,
                change.new_bytes,
                old is not None,
                sha_before,
                _sha256(change.new_bytes),
                mode,
            )
        )
    manifest = {
        "edit_id": edit_id,
        "state": "journaled",
        "started_at": datetime.now(UTC).isoformat(),
        "files": [
            {
                "index": e.index,
                "path": str(e.path),
                "existed": e.existed,
                "sha256_before": e.sha_before,
                "sha256_new": e.sha_new,
                "sha256_after": None,
                "mode": e.mode,
                "backup": f"files/{e.index:04d}" if e.existed else None,
            }
            for e in entries
        ],
    }
    _write_json(edit_dir / _MANIFEST, manifest)
    _fsync_dir(files_dir)
    _fsync_dir(edit_dir)
    for e in entries:
        e.tmp = _write_tmp(e.path.parent, edit_id, e.new_bytes, e.mode)


def _verify_before_rename(entries: list[_Entry], precheck: Callable[[Path], None] | None) -> None:
    """手順 3: 改名の直前の照合。全ファイルを確かめ終えるまで、どれも改名しない。"""
    for e in entries:
        if precheck is not None:
            precheck(e.path)
        current = _read_if_exists(e.path)
        _check_sha(e.path, e.sha_before, _sha256(current) if current is not None else None)


def _rename_all(entries: list[_Entry], edit_dir: Path, edit_id: str) -> list[WrittenFile]:
    manifest_path = edit_dir / _MANIFEST
    renamed: list[_Entry] = []
    try:
        deadline = time.monotonic() + RENAME_BUDGET_S
        for e in entries:
            assert e.tmp is not None
            _replace(e.tmp, e.path, deadline=deadline)
            renamed.append(e)
            e.tmp = None
        results: list[WrittenFile] = []
        for e in entries:
            data = e.path.read_bytes()
            results.append(WrittenFile(e.path, e.sha_before, _sha256(data), len(data), created=not e.existed))
    except Exception as exc:
        failed = _restore(edit_dir, [e for e in reversed(renamed)], edit_id)
        _set_state(edit_dir, "rollback_failed" if failed else "rolled_back")
        if failed:
            _log.error(
                "rollback failed for %d file(s); journal kept at %s",
                len(failed),
                manifest_path,
                extra={"event": "edit_rollback_failed", "edit_id": edit_id, "log_ref": edit_id},
            )
        raise _internal(
            "Writing failed while renaming files; "
            + (
                "all changes were rolled back."
                if not failed
                else "rollback was incomplete, see details.journal."
            ),
            exc,
            edit_id=edit_id,
            rolled_back=not failed,
            journal=manifest_path,
        ) from exc
    _set_state(edit_dir, "committed", sha_after={r.abs_path: r.sha256_after for r in results})
    _fsync_dirs({e.path.parent for e in entries})
    return results


def _fsync_dirs(directories: set[Path]) -> None:
    for d in directories:
        _fsync_dir(d)


def _set_state(edit_dir: Path, state: str, *, sha_after: dict[Path, str] | None = None) -> None:
    """``manifest.json`` の状態を更新する。失敗してもデータは守られているのでログだけ（手動の ``rollback`` は動く）。"""
    manifest_path = edit_dir / _MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["state"] = state
        if sha_after is not None:
            by_path = {_norm(p): s for p, s in sha_after.items()}
            for entry in manifest["files"]:
                entry["sha256_after"] = by_path.get(_norm(Path(entry["path"])))
        _write_json(manifest_path, manifest)
    except (OSError, ValueError, KeyError) as exc:
        _log.warning("could not update journal state to %s: %s", state, exc)


# --- ロールバック --------------------------------------------------------------------------


def _restore_one(edit_dir: Path, entry: dict[str, Any], edit_id: str) -> None:
    """ジャーナルの 1 件を元のパスへ戻す。元が無かったファイルは消す。"""
    target = Path(entry["path"])
    if not entry["existed"]:
        try:
            _with_retry(target.unlink)
        except FileNotFoundError:
            pass
        return
    data = (edit_dir / entry["backup"]).read_bytes()
    if _sha256(data) != entry["sha256_before"]:
        raise OSError(f"journal backup is corrupt: {entry['backup']}")
    tmp = _write_tmp(target.parent, edit_id, data, entry["mode"])
    try:
        _replace(tmp, target)
    finally:
        _unlink_quiet(tmp)


def _restore(edit_dir: Path, done: list[_Entry], edit_id: str) -> list[Path]:
    """改名済みの ``done`` を戻す。戻せなかったパスを返す（全部戻れば空）。1 件の失敗で止めない。"""
    try:
        manifest = json.loads((edit_dir / _MANIFEST).read_text(encoding="utf-8"))
        by_index = {f["index"]: f for f in manifest["files"]}
    except (OSError, ValueError, KeyError) as exc:
        _log.error("journal manifest unreadable: %s", exc, extra={"edit_id": edit_id})
        return [e.path for e in done]
    failed: list[Path] = []
    for e in done:
        try:
            _restore_one(edit_dir, by_index[e.index], edit_id)
        except Exception as exc:  # noqa: BLE001  1 件の失敗で残りの復元を止めない
            _log.error("could not restore %s: %s", e.path.name, exc, extra={"edit_id": edit_id})
            failed.append(e.path)
    return failed


def rollback(journal_dir: Path, edit_id: str, *, force: bool = False) -> list[Path]:
    """ジャーナルの ``edit_id`` を手動で巻き戻し、戻したパスを返す。

    各ファイルの現在の SHA-256 が「書き込み前」「新内容」「改名後」のどれでもない（その後に別の変更が入った）ときは、
    何も変えずに ``E_PRECONDITION_FAILED``。``force=True`` で無視する。すでに元の内容のファイルは触らない。
    """
    _check_edit_id(edit_id)
    edit_dir = journal_dir / edit_id
    try:
        manifest = json.loads((edit_dir / _MANIFEST).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as exc:
        raise LuciankeError(
            ErrorCode.E_INVALID_ARGUMENT,
            f"no journal for edit_id {edit_id!r}",
            details={"param": "edit_id"},
        ) from exc
    todo: list[_Entry] = []
    for f in manifest["files"]:
        path = Path(f["path"])
        current = _read_if_exists(path)
        sha_now = _sha256(current) if current is not None else None
        if sha_now == f["sha256_before"]:
            continue
        known = {f["sha256_before"], f["sha256_new"], f["sha256_after"]}
        if not force and sha_now not in known:
            raise _precondition_failed(path, f["sha256_new"], sha_now)
        todo.append(
            _Entry(f["index"], path, b"", f["existed"], f["sha256_before"], f["sha256_new"], f["mode"])
        )
    failed = _restore(edit_dir, list(reversed(todo)), edit_id)
    if failed:
        _set_state(edit_dir, "rollback_failed")
        raise _internal(
            "Rollback was incomplete, see details.journal.",
            OSError("restore failed"),
            edit_id=edit_id,
            rolled_back=False,
            journal=edit_dir / _MANIFEST,
        )
    _set_state(edit_dir, "rolled_back")
    return [e.path for e in todo]


# --- 保持 ---------------------------------------------------------------------------------


def _natural_key(name: str) -> list[tuple[int, int, str]]:
    # "ed_9999" < "ed_10000" になるよう、数字の連なりは数値で比べる
    return [(1, int(p), "") if p.isdigit() else (0, 0, p) for p in re.split(r"(\d+)", name) if p]


def prune_journal(journal_dir: Path, keep: int = JOURNAL_KEEP) -> list[str]:
    """``edit_id`` の古いものから消し、直近 ``keep`` 件を残す。消した ``edit_id`` を返す。"""
    try:
        dirs = [p for p in journal_dir.iterdir() if p.is_dir()]
    except FileNotFoundError:
        return []
    dirs.sort(key=lambda p: _natural_key(p.name))
    removed: list[str] = []
    for p in dirs[: max(len(dirs) - max(keep, 0), 0)]:
        shutil.rmtree(p, ignore_errors=True)
        removed.append(p.name)
    return removed
