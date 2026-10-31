"""BUG-0135 の残り: ``pyrightconfig.json`` の ``extends`` の先の設定ファイルの変更を、動いている basedpyright / pyright に届け、Lucianke の
見る解析の対象（``LspHandle.analysis_scope``）を読み直す。

* ``read_pyright_scope`` / ``changed_config_files``: 読んだ設定ファイル（``extends`` の先を含む。まだ無い先も）と刻み、変わったものの見つけ方。
  ``PyrightScope.with_prior``（読み直した直後は前の対象でも開かせる）。ドライブつきの絶対パスの ``include``。
* ``LspPool.notify_watched``（根の下の先）・``LspPool.sync_config_chain``（根の外の先。監視は根の下しか見ない）: 偽のサーバーが受けた通知で
  確かめる（``tests/unit/test_lsp_config_notify_bug0135.py`` の偽のサーバー）。
* 実機の basedpyright は ``tests/contract/test_lsp_extends_chain_bug0135.py``。
"""

# ruff: noqa: F811  (フィクスチャ recorded を引数で受ける)

from __future__ import annotations

import json
from pathlib import Path

from test_lsp_config_notify_bug0135 import (  # noqa: F401  (フィクスチャ registry・recorded)
    Recorded,
    _uris,
    recorded,
    registry,
)

from lucianke.core.model import RelPath
from lucianke.lsp.pool import FileChangeType
from lucianke.lsp.pyright_scope import changed_config_files, read_pyright_scope

# --- 読んだ設定ファイルと刻み ---------------------------------------------------------------------------------


def _write(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_the_scope_records_every_config_file_it_read_including_a_missing_extends_target(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(root / "pyrightconfig.json", {"extends": "../shared/base.json"})
    read = read_pyright_scope(root, refresh=True)
    assert read.scope is not None
    assert [p.name for p, stamp in read.files] == ["pyrightconfig.json", "base.json"]
    assert read.files[1][1] is None  # まだ無い先: 作られたら見つける
    assert changed_config_files(read.files) == []
    _write(tmp_path / "shared" / "base.json", {"include": ["src"]})
    changed = changed_config_files(read.files)
    assert [(p.name, kind) for p, kind in changed] == [("base.json", 1)]
    assert read_pyright_scope(root).scope is not None and read_pyright_scope(root).scope.narrowed  # 覚えは先の変化で捨てる


def test_changed_config_files_reports_modified_and_deleted_files(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    base = tmp_path / "shared" / "base.json"
    _write(base, {"include": ["src"]})
    _write(root / "pyrightconfig.json", {"extends": str(base)})
    files = read_pyright_scope(root, refresh=True).files
    _write(base, {"include": ["src", "tests"], "exclude": ["x"]})
    assert [(p.name, k) for p, k in changed_config_files(files)] == [("base.json", 2)]
    base.unlink()
    assert [(p.name, k) for p, k in changed_config_files(files)] == [("base.json", 3)]


def test_an_unreadable_extends_target_keeps_the_files_read_so_far(tmp_path: Path) -> None:
    """先の構文が壊れていて対象は判断しない（``None``）。それでも先は鎖に残り、直したら見つける。"""
    root = tmp_path / "proj"
    base = tmp_path / "shared" / "base.json"
    base.parent.mkdir()
    base.write_text("{ not json", encoding="utf-8")
    _write(root / "pyrightconfig.json", {"extends": str(base)})
    read = read_pyright_scope(root, refresh=True)
    assert read.scope is None
    assert [p.name for p, _ in read.files] == ["pyrightconfig.json", "base.json"]
    _write(base, {"include": ["src"]})
    assert [(p.name, k) for p, k in changed_config_files(read.files)] == [("base.json", 2)]


def test_an_absolute_include_with_a_drive_letter_is_not_joined_to_the_config_directory(tmp_path: Path) -> None:
    """``include`` に絶対パス（Windows の ``C:/…``）を書いた先（``extends`` の先は別のディレクトリなので絶対パスで書くことが多い）。"""
    root = tmp_path / "proj"
    base = tmp_path / "shared" / "base.json"
    _write(base, {"include": [(root / "src").as_posix()]})
    _write(root / "pyrightconfig.json", {"extends": str(base)})
    scope = read_pyright_scope(root, refresh=True).scope
    assert scope is not None and scope.narrowed
    assert scope.contains(root / "src" / "a.py") and not scope.contains(root / "tests" / "b.py")


def test_with_prior_counts_only_files_inside_both_scopes(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(root / "pyrightconfig.json", {"include": ["src"]})
    narrow = read_pyright_scope(root, refresh=True).scope
    _write(root / "pyrightconfig.json", {"include": ["src", "tests"]})
    wide = read_pyright_scope(root, refresh=True).scope
    assert narrow is not None and wide is not None
    assert wide.contains(root / "tests" / "t.py")
    both = wide.with_prior(narrow)
    assert both.narrowed and not both.contains(root / "tests" / "t.py") and both.contains(root / "src" / "a.py")


# --- 偽のサーバー ---------------------------------------------------------------------------------------------


async def test_a_change_to_an_extended_config_under_the_root_reloads_the_scope(recorded: Recorded) -> None:
    root = recorded.root
    h = recorded.handle()
    _write(root / "config" / "base.json", {"include": ["../src"]})
    _write(root / "pyrightconfig.json", {"extends": "./config/base.json"})
    await recorded.change(("pyrightconfig.json", FileChangeType.CREATED))
    assert h.analysis_scope is not None and h.analysis_scope.narrowed
    assert recorded.pool.is_config_chain_file(recorded.ws, RelPath("config/base.json"))
    assert not recorded.pool.is_config_chain_file(recorded.ws, RelPath("config/other.json"))
    _write(root / "config" / "base.json", {"include": ["../src", "../tests"]})
    got = await recorded.change(("config/base.json", FileChangeType.CHANGED))
    assert [_uris(x) for x in got] == [["base.json"]]  # 根の設定ではないので、そのまま届ける（明示の判断は変わらない）
    assert h.analysis_scope.contains(root / "tests" / "t.py")  # type: ignore[union-attr]
    assert h.priming_scope is not None and not h.priming_scope.contains(root / "tests" / "t.py")  # 読み直した直後は前の対象でも


async def test_a_change_to_an_extended_config_outside_the_root_is_synced_before_a_query(recorded: Recorded) -> None:
    root = recorded.root
    h = recorded.handle()
    base = root.parent / "shared" / "base.json"
    _write(base, {"include": [(root / "src").as_posix()]})
    _write(root / "pyrightconfig.json", {"extends": str(base)})
    await recorded.change(("pyrightconfig.json", FileChangeType.CREATED))
    assert await recorded.pool.sync_config_chain(h) is False  # 変わっていない: 何も送らない
    _write(base, {"include": [(root / "src").as_posix(), (root / "tests").as_posix()]})
    before = len(recorded.lines())
    assert await recorded.pool.sync_config_chain(h) is True
    assert await recorded.pool.sync_config_chain(h) is False  # 届けたので刻みは新しい
    lines = await recorded.wait_lines(lambda ls: any(_uris(x) == ["base.json"] for x in ls[before:]))
    sent = [x for x in lines[before:] if _uris(x) == ["base.json"]]
    assert len(sent) == 1 and sent[0]["params"]["changes"][0]["type"] == int(FileChangeType.CHANGED)
    assert h.analysis_scope is not None and h.analysis_scope.contains(root / "tests" / "t.py")
    base.unlink()
    assert await recorded.pool.sync_config_chain(h) is True
    lines = await recorded.wait_lines(lambda ls: any(_uris(x) == ["base.json"] and x["params"]["changes"][0]["type"] == 3 for x in ls))
    assert lines  # 先が消えた（削除）も届ける

