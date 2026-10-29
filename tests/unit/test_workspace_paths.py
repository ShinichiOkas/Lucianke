"""workspace/paths.py: パスの封じ込め・除外 glob・file:// URI（設計書 §7.3.2、仕様書 §13.1、§13.4）。"""

from __future__ import annotations

import os
import string
from pathlib import Path
from urllib.parse import quote

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.workspace.paths import (
    _actual_case,
    confine,
    from_file_uri,
    is_excluded,
    to_abs,
    to_file_uri,
)
from lucianke.workspace.types import Workspace

IS_WINDOWS = os.name == "nt"


def _probe_case_insensitive(root: Path) -> bool:
    probe = root / "CaseProbe.tmp"
    probe.write_text("x", encoding="utf-8")
    try:
        return (root / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def make_ws(root: Path, **kwargs) -> Workspace:
    real = Path(os.path.realpath(root))
    kwargs.setdefault("case_insensitive", _probe_case_insensitive(real))
    return Workspace(workspace_id="w", root=real, **kwargs)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "conf").mkdir()
    (root / "conf" / ".env").write_text("K=V\n", encoding="utf-8")
    return make_ws(root, exclude_globs=("build/**", "*.min.js", "node_modules"))


def expect_rejected(
    ws: Workspace, user_path: str, reason: str, *, for_write: bool = False, param: str = "path"
):
    with pytest.raises(LuciankeError) as ei:
        confine(ws, user_path, for_write=for_write, param=param)
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details == {"param": param, "reason": reason}
    return ei


# --- 相対パス・絶対パスの正規化 -------------------------------------------------------------


def test_relative_path_is_normalized(ws: Workspace):
    assert confine(ws, "src/Mod.py") == "src/Mod.py"
    assert confine(ws, "./src//../src/Mod.py") == "src/Mod.py"
    assert confine(ws, "src/") == "src"


def test_root_itself_is_dot(ws: Workspace):
    assert confine(ws, ".") == "."
    assert confine(ws, str(ws.root)) == "."
    assert confine(ws, "src/..") == "."


def test_absolute_path_under_root_becomes_relative_with_slashes(ws: Workspace):
    result = confine(ws, str(ws.root / "src" / "Mod.py"))
    assert result == "src/Mod.py"
    assert "\\" not in result


def test_nonexistent_path_is_accepted_for_new_files(ws: Workspace):
    assert confine(ws, "src/new/deeper/file.py", for_write=True) == "src/new/deeper/file.py"


def test_dotdot_that_stays_inside_is_fine(ws: Workspace):
    assert confine(ws, "src/nope/../Mod.py") == "src/Mod.py"


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows の区切り・ドライブ文字")
class TestWindowsNormalization:
    def test_backslashes(self, ws: Workspace):
        assert confine(ws, "src\\Mod.py") == "src/Mod.py"

    def test_drive_letter_case_is_absorbed(self, ws: Workspace):
        absolute = str(ws.root / "src" / "Mod.py")
        assert absolute[1] == ":"
        flipped = absolute[0].swapcase() + absolute[1:]
        assert flipped != absolute
        assert confine(ws, flipped) == "src/Mod.py"
        assert confine(ws, flipped.replace("\\", "/")) == "src/Mod.py"

    def test_returns_actual_case_on_disk(self, ws: Workspace):
        assert confine(ws, "SRC/MOD.PY") == "src/Mod.py"
        assert confine(ws, str(ws.root).upper() + "\\src\\mod.py") == "src/Mod.py"

    def test_extended_length_prefix_for_a_path_under_root(self, ws: Workspace):
        assert confine(ws, "\\\\?\\" + str(ws.root / "src" / "Mod.py")) == "src/Mod.py"

    def test_other_drive_is_outside_root(self, ws: Workspace):
        own = ws.root.drive[:1].upper()
        other = next(c for c in "ZYXWVUTSRQPONM" if c != own)
        expect_rejected(ws, f"{other}:\\work\\file.py", "outside_root")
        expect_rejected(ws, f"{other}:/work/file.py", "outside_root")

    def test_drive_relative_is_rejected(self, ws: Workspace):
        expect_rejected(ws, ws.root.drive + "foo.py", "outside_root")

    @pytest.mark.parametrize(
        "bad", ["src/Mod.py:stream", "src/a:b", "src/a<b.py", "src/a|b", "src/a?.py", "src/*.py"]
    )
    def test_forbidden_characters(self, ws: Workspace, bad: str):
        expect_rejected(ws, bad, "invalid_chars")

    @pytest.mark.parametrize(
        "bad", ["src/nul", "src/CON.txt", "com1", "src/.git .", "src/name.", "src/name "]
    )
    def test_reserved_names_and_trailing_dots(self, ws: Workspace, bad: str):
        expect_rejected(ws, bad, "invalid_chars")

    def test_trailing_dot_cannot_hide_git_dir(self, ws: Workspace):
        expect_rejected(ws, ".git./config", "invalid_chars", for_write=True)

    def test_unc_path_is_outside_root(self, ws: Workspace):
        expect_rejected(ws, "\\\\server\\share\\x.py", "outside_root")
        expect_rejected(ws, "\\\\?\\UNC\\server\\share\\x.py", "outside_root")


@pytest.mark.skipif(IS_WINDOWS, reason="大文字小文字を区別する FS では綴りをそのまま返す")
def test_case_sensitive_fs_keeps_spelling(tmp_path: Path):
    ws = make_ws(tmp_path, case_insensitive=False)
    (tmp_path / "Mod.py").write_text("", encoding="utf-8")
    assert confine(ws, "Mod.py") == "Mod.py"
    assert confine(ws, "mod.py") == "mod.py"


def test_actual_case_walks_the_directory(tmp_path: Path):
    """macOS など realpath が綴りを正さない環境用の補正。OS を問わず関数を直接確かめる。"""
    (tmp_path / "Src" / "Sub").mkdir(parents=True)
    (tmp_path / "Src" / "Sub" / "Mod.py").write_text("", encoding="utf-8")
    assert _actual_case(tmp_path, ("SRC", "sub", "MOD.PY")) == ("Src", "Sub", "Mod.py")
    # 存在しない要素から先は入力のまま
    assert _actual_case(tmp_path, ("src", "Missing", "File.PY")) == ("Src", "Missing", "File.PY")
    assert _actual_case(tmp_path, ()) == ()


def test_actual_case_is_applied_when_realpath_does_not_fix_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """realpath が綴りを正さない環境（macOS）を、realpath の差し替えで再現する。"""
    (tmp_path / "Src").mkdir()
    (tmp_path / "Src" / "Mod.py").write_text("", encoding="utf-8")
    ws = make_ws(tmp_path, case_insensitive=True)
    monkeypatch.setattr("lucianke.workspace.paths._IS_WINDOWS", False)
    monkeypatch.setattr("os.path.realpath", lambda p: os.path.normpath(p))
    assert confine(ws, "src/mod.py") == "Src/Mod.py"
    assert confine(ws, str(ws.root).swapcase() + "/SRC/MOD.PY") == "Src/Mod.py"


# --- ルート外 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["../../etc/passwd", "..", "../outside.txt", "src/../../outside.txt", "./../x"]
)
def test_dotdot_escape(ws: Workspace, raw: str):
    expect_rejected(ws, raw, "outside_root")


def test_absolute_path_outside_root(ws: Workspace, tmp_path: Path):
    outside = tmp_path / "other" / "x.py"
    expect_rejected(ws, str(outside), "outside_root")
    expect_rejected(ws, str(ws.root.parent), "outside_root")


def test_sibling_directory_with_common_prefix_is_outside(ws: Workspace):
    sibling = ws.root.parent / (ws.root.name + "-evil")
    sibling.mkdir()
    expect_rejected(ws, str(sibling / "x.py"), "outside_root")


def test_param_name_is_reported(ws: Workspace):
    expect_rejected(ws, "../x", "outside_root", param="scope_paths")


@pytest.mark.parametrize(
    "raw", ["a\x00b.py", "\x00", "src/Mod.py\x00.txt", "a\nb.py", "a\tb.py", "\x7f", "a\x1fb"]
)
def test_nul_and_control_characters(ws: Workspace, raw: str):
    expect_rejected(ws, raw, "invalid_chars")


def test_empty_and_non_string_paths(ws: Workspace):
    expect_rejected(ws, "", "invalid_chars")
    with pytest.raises(LuciankeError):
        confine(ws, None)  # type: ignore[arg-type]


# --- シンボリックリンク -------------------------------------------------------------------


def _symlink(link: Path, target: Path, *, directory: bool) -> None:
    """リンクを作る。Windows で権限が無ければ、ディレクトリはジャンクション（権限不要）で代用し、ファイルは skip する。"""
    try:
        os.symlink(target, link, target_is_directory=directory)
        return
    except (OSError, NotImplementedError) as exc:
        failure = exc
    if IS_WINDOWS and directory and target.exists():
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip(f"cannot create symlinks here: {failure}")


def test_symlinked_directory_pointing_outside(ws: Workspace, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    _symlink(ws.root / "link", outside, directory=True)
    expect_rejected(ws, "link/secret.txt", "symlink_escape")
    expect_rejected(ws, "link", "symlink_escape")
    expect_rejected(ws, "link/new.txt", "symlink_escape", for_write=True)


def test_symlinked_file_pointing_outside(ws: Workspace, tmp_path: Path):
    target = tmp_path / "outside.txt"
    target.write_text("s", encoding="utf-8")
    _symlink(ws.root / "file_link", target, directory=False)
    expect_rejected(ws, "file_link", "symlink_escape")


def test_dangling_symlink_pointing_outside(ws: Workspace, tmp_path: Path):
    _symlink(ws.root / "dangling", tmp_path / "does-not-exist", directory=False)
    expect_rejected(ws, "dangling", "symlink_escape")


@pytest.mark.skipif(IS_WINDOWS, reason="Windows は OS 自身が ``..`` を字句で畳んでからリンクをたどる")
def test_dotdot_after_symlink_is_resolved_through_the_link(ws: Workspace, tmp_path: Path):
    """字句では ``link/..`` は消えてルート内に見えるが、実体は link の親（ルート外）。"""
    outside = tmp_path / "outside" / "deep"
    outside.mkdir(parents=True)
    _symlink(ws.root / "link", outside, directory=True)
    expect_rejected(ws, "link/../x.txt", "symlink_escape")


def test_symlink_inside_root_resolves_to_its_target(ws: Workspace):
    _symlink(ws.root / "alias", ws.root / "src", directory=True)
    assert confine(ws, "alias/Mod.py") == "src/Mod.py"


def test_absolute_path_through_symlinked_root_spelling(tmp_path: Path):
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    _symlink(tmp_path / "spelled", real, directory=True)
    ws = make_ws(real)
    assert confine(ws, str(tmp_path / "spelled" / "src" / "a.py")) == "src/a.py"


# --- 書き込みの安全装置 -------------------------------------------------------------------


def test_git_dir_is_not_writable_but_readable(ws: Workspace):
    expect_rejected(ws, ".git/config", "git_dir", for_write=True)
    expect_rejected(ws, ".git/hooks/pre-commit", "git_dir", for_write=True)
    expect_rejected(ws, ".git", "git_dir", for_write=True)
    expect_rejected(ws, "sub/.git/config", "git_dir", for_write=True)
    assert confine(ws, ".git/config") == ".git/config"
    assert confine(ws, ".gitignore", for_write=True) == ".gitignore"
    assert confine(ws, "src/.github/x.yml", for_write=True) == "src/.github/x.yml"


def test_git_dir_check_ignores_case_on_case_insensitive_fs(tmp_path: Path):
    ws = make_ws(tmp_path, case_insensitive=True)
    expect_rejected(ws, ".GIT/config", "git_dir", for_write=True)


def test_excluded_globs_are_not_writable_but_readable(ws: Workspace):
    expect_rejected(ws, "build/out/x.py", "excluded", for_write=True)
    expect_rejected(ws, "web/app.min.js", "excluded", for_write=True)
    expect_rejected(ws, "a/node_modules/pkg/index.js", "excluded", for_write=True)
    assert confine(ws, "build/out/x.py") == "build/out/x.py"
    assert confine(ws, "web/app.js", for_write=True) == "web/app.js"


@pytest.mark.parametrize(
    "raw",
    [
        "conf/.env",
        ".env.local",
        "keys/server.pem",
        "id_rsa",
        "a/id_rsa.pub",
        "x.p12",
        "gcp-credentials.json",
        "k.KEY",
    ],
)
def test_secret_files_are_not_writable(ws: Workspace, raw: str):
    expect_rejected(ws, raw, "secret_file", for_write=True)
    assert confine(ws, raw)  # 読むための封じ込めは通る（本文を読まない判断は languages / tools 側）


def test_for_write_does_not_reject_ordinary_files(ws: Workspace):
    assert confine(ws, "src/Mod.py", for_write=True) == "src/Mod.py"
    assert confine(ws, "environment.py", for_write=True) == "environment.py"


def test_outside_root_wins_over_write_checks(ws: Workspace):
    expect_rejected(ws, "../.git/config", "outside_root", for_write=True)


# --- to_abs ------------------------------------------------------------------------------


def test_to_abs_roundtrip(ws: Workspace):
    rel = confine(ws, "src/Mod.py")
    assert to_abs(ws, rel) == ws.root / "src" / "Mod.py"
    assert to_abs(ws, confine(ws, ".")) == ws.root


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd"])
def test_to_abs_rejects_unconfined_input(ws: Workspace, bad: str):
    with pytest.raises(ValueError):
        to_abs(ws, bad)  # type: ignore[arg-type]


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows のドライブ付きパス")
def test_to_abs_rejects_drive_path(ws: Workspace):
    with pytest.raises(ValueError):
        to_abs(ws, "C:/Windows")  # type: ignore[arg-type]


# --- is_excluded -------------------------------------------------------------------------


def _ex(*globs: str, root: Path | None = None, ci: bool = False) -> Workspace:
    return Workspace(
        workspace_id="w", root=root or Path(os.path.realpath(".")), exclude_globs=globs, case_insensitive=ci
    )


@pytest.mark.parametrize(
    ("globs", "rel", "expected"),
    [
        # 要素名だけのパターンは、どの深さのファイル・ディレクトリにも当たり、配下も除外する
        (("node_modules",), "node_modules/x/y.js", True),
        (("node_modules",), "a/b/node_modules/y.js", True),
        (("node_modules",), "a/node_modules_x/y.js", False),
        (("*.pyc",), "a/b/c.pyc", True),
        (("*.pyc",), "c.pyc", True),
        (("*.pyc",), "a/c.py", False),
        # / を含むパターンはルートに固定。* は / を越えない
        (("docs/*.md",), "docs/a.md", True),
        (("docs/*.md",), "docs/sub/a.md", False),
        (("docs/*.md",), "x/docs/a.md", False),
        (("/build",), "build/a.py", True),
        (("/build",), "src/build/a.py", False),
        # ** は / を越える
        (("build/**",), "build/a/b/c.py", True),
        (("build/**",), "build/a.py", True),
        (("build/**",), "src/build/a.py", False),
        (("**/gen/**",), "a/b/gen/x.py", True),
        (("**/gen/**",), "gen/x.py", True),
        (("**/*.min.js",), "web/static/app.min.js", True),
        (("**/*.min.js",), "app.min.js", True),
        (("**/*.min.js",), "app.js", False),
        (("src/**/test_*.py",), "src/a/b/test_x.py", True),
        (("src/**/test_*.py",), "src/test_x.py", True),
        (("src/**/test_*.py",), "lib/test_x.py", False),
        (("**",), "anything/at/all.txt", True),
        # ? と文字クラス
        (("data?.csv",), "data1.csv", True),
        (("data?.csv",), "data12.csv", False),
        (("log[0-9].txt",), "log7.txt", True),
        (("log[!0-9].txt",), "log7.txt", False),
        (("log[!0-9].txt",), "logx.txt", True),
        # 末尾 / はディレクトリだけ
        (("cache/",), "cache/x.bin", True),
        (("cache/",), "a/cache/x.bin", True),
        (("cache/",), "cache", False),
        # 否定・空は除外に使わない
        (("!keep.txt",), "keep.txt", False),
        (("",), "a.txt", False),
        # 複数パターン
        (("*.log", "tmp/**"), "tmp/a.txt", True),
        (("*.log", "tmp/**"), "a.log", True),
        (("*.log", "tmp/**"), "a.txt", False),
        # ルートそのものは除外しない
        (("**",), ".", False),
        ((), "a.txt", False),
    ],
)
def test_is_excluded(globs: tuple[str, ...], rel: str, expected: bool):
    assert is_excluded(_ex(*globs), rel) is expected  # type: ignore[arg-type]


def test_is_excluded_case_sensitivity_follows_the_filesystem():
    assert is_excluded(_ex("Build/**", ci=True), "build/a.py") is True  # type: ignore[arg-type]
    if not IS_WINDOWS:
        assert is_excluded(_ex("Build/**", ci=False), "build/a.py") is False  # type: ignore[arg-type]


# --- file:// URI -------------------------------------------------------------------------


def test_uri_roundtrip_with_spaces_and_japanese(ws: Workspace):
    rel = confine(ws, "src/my dir/日本語 ファイル.py")
    (ws.root / "src" / "my dir").mkdir()
    (ws.root / "src" / "my dir" / "日本語 ファイル.py").write_text("", encoding="utf-8")
    uri = to_file_uri(ws, rel)
    assert uri.startswith("file:///")
    assert " " not in uri
    assert "%20" in uri
    assert "%E6%97%A5%E6%9C%AC%E8%AA%9E" in uri
    assert from_file_uri(ws, uri) == rel
    assert to_abs(ws, from_file_uri(ws, uri)) == ws.root / "src" / "my dir" / "日本語 ファイル.py"  # type: ignore[arg-type]


def test_uri_special_characters_roundtrip(ws: Workspace):
    rel = confine(ws, "src/a#b%c d&e.py")
    uri = to_file_uri(ws, rel)
    assert "#" not in uri.split("file://", 1)[1]
    assert from_file_uri(ws, uri) == rel


def test_uri_for_root_file(ws: Workspace):
    assert from_file_uri(ws, to_file_uri(ws, confine(ws, "README.md"))) == "README.md"
    assert from_file_uri(ws, to_file_uri(ws, confine(ws, "."))) == "."


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows のドライブ文字つき URI")
class TestWindowsUri:
    def _literal(self, ws: Workspace, rel: str, *, drive: str) -> str:
        posix = (ws.root / rel).as_posix()  # "S:/work/..."
        return "file:///" + drive + quote(posix[2:], safe="/")

    def test_uri_form(self, ws: Workspace):
        uri = to_file_uri(ws, confine(ws, "src/Mod.py"))
        assert uri == "file:///" + (ws.root / "src" / "Mod.py").as_posix().replace(" ", "%20")
        assert uri[8].isalpha()
        assert uri[9] == ":"

    def test_lowercase_drive(self, ws: Workspace):
        drive = ws.root.drive[0].lower() + ":"
        assert from_file_uri(ws, self._literal(ws, "src/Mod.py", drive=drive)) == "src/Mod.py"

    def test_percent_encoded_colon_as_vscode_sends(self, ws: Workspace):
        """vtsls などは ``file:///c%3A/...`` を返す。"""
        drive = ws.root.drive[0].lower() + "%3A"
        assert from_file_uri(ws, self._literal(ws, "src/Mod.py", drive=drive)) == "src/Mod.py"

    def test_other_drive_is_none(self, ws: Workspace):
        own = ws.root.drive[0].upper()
        other = next(c for c in string.ascii_uppercase[::-1] if c != own)
        assert from_file_uri(ws, f"file:///{other}:/x/y.py") is None

    def test_unc_uri_is_none(self, ws: Workspace):
        assert from_file_uri(ws, "file://server/share/x.py") is None


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX の file:///home/... 形式")
def test_posix_literal_uri(tmp_path: Path):
    ws = Workspace(workspace_id="w", root=Path(os.path.realpath(tmp_path)))
    root = ws.root.as_posix()
    assert from_file_uri(ws, f"file://{quote(root)}/src/a%20b.py") == "src/a b.py"
    assert from_file_uri(ws, f"file://localhost{quote(root)}/src/a.py") == "src/a.py"
    assert from_file_uri(ws, f"file://{quote(root)}/%E6%97%A5.py") == "日.py"
    assert to_file_uri(ws, confine(ws, "src/a b.py")) == f"file://{quote(root)}/src/a%20b.py"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX の他ホスト URI")
def test_posix_remote_host_uri_is_none(tmp_path: Path):
    ws = make_ws(tmp_path)
    assert from_file_uri(ws, "file://otherhost/home/u/x.py") is None


def test_uri_outside_root_or_foreign_is_none(ws: Workspace, tmp_path: Path):
    outside = (tmp_path / "other" / "x.py").as_uri()
    assert from_file_uri(ws, outside) is None
    assert from_file_uri(ws, ws.root.parent.as_uri()) is None
    assert from_file_uri(ws, "https://example.com/x.py") is None
    assert from_file_uri(ws, "untitled:Untitled-1") is None
    assert from_file_uri(ws, "jar:file:///x.jar!/A.class") is None
    assert from_file_uri(ws, "not a uri") is None
    assert from_file_uri(ws, "") is None


def test_uri_with_encoded_nul_or_dotdot_is_none(ws: Workspace):
    assert from_file_uri(ws, ws.root.as_uri() + "/src/a%00.py") is None
    assert from_file_uri(ws, ws.root.as_uri() + "/src/%2E%2E/%2E%2E/x.py") is None


def test_uri_pointing_through_escaping_symlink_is_none(ws: Workspace, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink(ws.root / "link", outside, directory=True)
    assert from_file_uri(ws, (ws.root / "link" / "x.py").as_uri()) is None


def test_uri_to_git_and_secret_is_still_a_valid_read(ws: Workspace):
    """URI は読み取り用の封じ込め。書き込みの拒否（.git・秘密）は ``for_write`` のときだけ。"""
    assert from_file_uri(ws, (ws.root / ".git" / "config").as_uri()) == ".git/config"
    assert from_file_uri(ws, (ws.root / "conf" / ".env").as_uri()) == "conf/.env"
