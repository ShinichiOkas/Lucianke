"""git/repo.py・git/vtree.py: リビジョン解決・過去断面・ステージ・ignore・HEAD 情報・仮想ツリー（設計書 §7.9.1、仕様書 §8.5）。

リポジトリは ``tmp_path`` に dulwich だけで作る（``git`` コマンドと利用者の gitconfig に依存しない）。
コミット時刻は固定して SHA を決定的にする（短縮 SHA の衝突で揺れないように）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.objects import Blob, Commit, Tag, Tree
from dulwich.repo import Repo

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.git.repo import GitRepo, HeadInfo, ResolvedRevision
from lucianke.git.vtree import VirtualTree, WorkingTreeSource, open_file_source
from lucianke.workspace.types import Workspace

WHO = b"Tester <tester@example.invalid>"
T0 = 1_700_000_000


# --- フィクスチャ -------------------------------------------------------------------------


@dataclass(frozen=True)
class Fx:
    root: Path
    c1: str
    c2: str
    c3: str
    ann_tag_obj: str  # 注釈付きタグ v1-ann のタグオブジェクト（コミットとは別の SHA）

    @property
    def git(self) -> GitRepo:
        g = GitRepo.open(self.root)
        assert g is not None
        return g


def _w(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return str(p)


def _commit(repo: Repo, root: Path, files: dict[str, str], msg: str, n: int) -> str:
    paths = [_w(root, rel, text) for rel, text in files.items()]
    porcelain.add(repo, paths=paths)
    sha = porcelain.commit(
        repo,
        message=msg.encode(),
        author=WHO,
        committer=WHO,
        author_timestamp=T0 + n * 60,
        commit_timestamp=T0 + n * 60,
        author_timezone=0,
        commit_timezone=0,
    )
    return sha.decode("ascii")


def build_fixture(root: Path) -> Fx:
    """3 コミット + タグ + ブランチ + ステージ/未ステージ/未追跡/無視の混在する作業ツリー。"""
    root.mkdir(parents=True, exist_ok=True)
    repo = porcelain.init(str(root))
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    c1 = _commit(
        repo,
        root,
        {
            ".gitignore": ".venv/\n__pycache__/\n*.log\n",
            "README.md": "v1\n",
            "src/app.py": "A1\n",
            "docs/日本語.md": "jp-v1\n",
            "sub/.gitignore": "*.gen\n!keep.gen\n",
            "sub/code.py": "sub\n",
        },
        "c1",
        1,
    )
    porcelain.tag_create(repo, "v1")  # 軽量タグ（c1）
    porcelain.tag_create(
        repo, "v1-ann", author=WHO, message=b"annotated", annotated=True, tag_time=T0 + 90, tag_timezone=0
    )
    ann_obj = repo.refs[b"refs/tags/v1-ann"].decode("ascii")
    c2 = _commit(repo, root, {"src/app.py": "A2\n"}, "c2", 2)
    porcelain.branch_create(repo, "feature")  # c2
    repo.refs[b"refs/remotes/origin/main"] = c2.encode("ascii")
    c3 = _commit(repo, root, {"src/app.py": "A3\n", "README.md": "v3\n"}, "c3", 3)

    # ステージだけの変更・ステージ後に作業ツリーでさらに変更・ステージだけの新規ファイル
    porcelain.add(repo, paths=[_w(root, "README.md", "staged\n")])
    porcelain.add(repo, paths=[_w(root, "new_staged.py", "NS\n")])
    _w(root, "README.md", "worktree\n")
    _w(root, "scratch.txt", "untracked\n")  # 未追跡
    _w(root, ".venv/lib/x.py", "ignored\n")  # 無視
    _w(root, "src/__pycache__/m.pyc", "ignored\n")
    _w(root, "debug.log", "ignored\n")
    repo.close()
    return Fx(root, c1, c2, c3, ann_obj)


@pytest.fixture(scope="module", autouse=True)
def _isolated_env(tmp_path_factory: pytest.TempPathFactory):
    """利用者のグローバル除外（``core.excludesFile`` / ``~/.config/git/ignore``）と gitconfig を読ませない。"""
    home = tmp_path_factory.mktemp("home")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        mp.setenv("USERPROFILE", str(home))
        mp.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory, _isolated_env) -> Fx:
    return build_fixture(tmp_path_factory.mktemp("repo") / "w")


@pytest.fixture
def fx_copy(fx: Fx, tmp_path: Path) -> Fx:
    """変更してよいコピー（``.git`` ごと。SHA は同じ）。"""
    dst = tmp_path / "w"
    shutil.copytree(fx.root, dst)
    return Fx(dst, fx.c1, fx.c2, fx.c3, fx.ann_tag_obj)


def make_ws(root: Path) -> Workspace:
    return Workspace(
        workspace_id="w",
        root=Path(os.path.realpath(root)),
        exclude_globs=(".venv/", "__pycache__/"),
    )


def snapshot(root: Path) -> dict[str, str]:
    """``.git`` を含む全ファイルの SHA-256。"""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# --- open --------------------------------------------------------------------------------


def test_open_non_git_directory_is_none(tmp_path: Path):
    assert GitRepo.open(tmp_path) is None


def test_open_missing_directory_is_none(tmp_path: Path):
    assert GitRepo.open(tmp_path / "nope") is None


def test_open_git_repository(fx: Fx):
    g = GitRepo.open(fx.root)
    assert g is not None
    assert g.root == fx.root


def test_open_subdirectory_is_not_discovered(fx: Fx):
    # 親の .git を拾わない（作業ルートがモノレポのサブディレクトリの場合は M2）
    assert GitRepo.open(fx.root / "src") is None


# --- resolve_revision: 許可する形 ----------------------------------------------------------


def test_resolve_working_tree_and_staged(fx: Fx):
    assert fx.git.resolve_revision("WORKING_TREE") == ResolvedRevision("working_tree", None, "WORKING_TREE")
    assert fx.git.resolve_revision("STAGED") == ResolvedRevision("staged", None, "STAGED")


@pytest.mark.parametrize(
    ("rev", "which"),
    [
        ("HEAD", "c3"),
        ("HEAD~0", "c3"),
        ("HEAD~", "c2"),
        ("HEAD~1", "c2"),
        ("HEAD^", "c2"),
        ("HEAD^1", "c2"),
        ("HEAD~2", "c1"),
        ("HEAD~1~1", "c1"),
        ("HEAD^^", "c1"),
        ("main", "c3"),
        ("refs/heads/main", "c3"),
        ("feature", "c2"),
        ("refs/heads/feature", "c2"),
        ("feature~1", "c1"),
        ("origin/main", "c2"),
        ("refs/remotes/origin/main", "c2"),
        ("origin/main^", "c1"),
        ("v1", "c1"),
        ("refs/tags/v1", "c1"),
        ("v1-ann", "c1"),  # 注釈付きタグは剥がしてコミットにする
    ],
)
def test_resolve_ref_forms(fx: Fx, rev: str, which: str):
    got = fx.git.resolve_revision(rev)
    assert got.kind == "commit"
    assert got.sha == getattr(fx, which)
    assert got.display == rev


@pytest.mark.parametrize("n", [40, 12, 7, 4])
def test_resolve_full_and_short_sha(fx: Fx, n: int):
    got = fx.git.resolve_revision(fx.c2[:n])
    assert got == ResolvedRevision("commit", fx.c2, fx.c2[:n])


def test_resolve_short_sha_with_suffix(fx: Fx):
    # 短縮 SHA を基点に ~N を付けられる（dulwich の parse_object 単体では不可。S-08）
    assert fx.git.resolve_revision(fx.c3[:7] + "~2").sha == fx.c1


def test_resolve_sha_is_always_40_hex(fx: Fx):
    for rev in ("HEAD", "v1-ann", fx.c1[:5]):
        sha = fx.git.resolve_revision(rev).sha
        assert sha is not None and len(sha) == 40 and all(ch in "0123456789abcdef" for ch in sha)


# --- resolve_revision: 拒否 ---------------------------------------------------------------


def _assert_not_found(g: GitRepo, rev: str, reason: str | None = None) -> LuciankeError:
    with pytest.raises(LuciankeError) as ei:
        g.resolve_revision(rev)
    err = ei.value
    assert err.code is ErrorCode.E_REVISION_NOT_FOUND
    assert err.details["param"] == "revision"
    assert "WORKING_TREE" in err.details["hint"]
    if reason is not None:
        assert err.details["reason"] == reason
    return err


@pytest.mark.parametrize(
    "rev",
    [
        "HEAD@{1}",
        "@{1}",
        "main@{yesterday}",
        "stash@{0}",
        "HEAD:README.md",
        ":README.md",
        "main:src/app.py",
        "HEAD..HEAD~1",
        "HEAD...main",
        "a..b",
        "HEAD^{}",
        "HEAD^{commit}",
        "v1-ann^{tag}",
        "HEAD^{/fix}",
        "../x",
        "refs/heads/../x",
        "/etc/passwd",
        "main/",
        "origin//main",
        "a b",
        "HEAD\n",
        "HEAD\x00",
        " HEAD",
        "HEAD ",
        "main*",
        "ma?n",
        "ma[i]n",
        "back\\slash",
        "",
        "@",
        "HEAD~99999999999",
    ],
)
def test_resolve_rejects_unsupported_or_missing(fx: Fx, rev: str):
    _assert_not_found(fx.git, rev)


@pytest.mark.parametrize(
    "rev",
    ["HEAD@{1}", "HEAD:README.md", "a..b", "HEAD^{}", "../x", "a b", "", "@"],
)
def test_resolve_rejected_syntax_reason(fx: Fx, rev: str):
    _assert_not_found(fx.git, rev, "unsupported_syntax")


@pytest.mark.parametrize(
    "rev",
    [
        "nonexistent",
        "abc",
        "0000",
        "ffffffff",
        "HEAD~3",
        "HEAD^2",
        "HEAD~~~",
        "working_tree",
        "staged",
        "head",
    ],
)
def test_resolve_missing_reason(fx: Fx, rev: str):
    _assert_not_found(fx.git, rev, "not_found")


def test_resolve_rejects_non_commit_objects(fx: Fx):
    repo = Repo(str(fx.root))
    try:
        tree_sha = repo[fx.c3.encode()].tree.decode("ascii")
        blob_sha = repo[repo[fx.c3.encode()].tree].lookup_path(repo.object_store.__getitem__, b"src/app.py")[
            1
        ]
    finally:
        repo.close()
    _assert_not_found(fx.git, tree_sha, "not_found")
    _assert_not_found(fx.git, blob_sha.decode("ascii"), "not_found")
    assert (
        fx.git.resolve_revision(fx.ann_tag_obj).sha == fx.c1
    )  # 注釈付きタグオブジェクトの SHA は剥がしてコミットにする


def test_resolve_is_case_sensitive_for_keywords(fx: Fx):
    _assert_not_found(fx.git, "Working_Tree")


def test_resolve_ambiguous_short_sha(tmp_path: Path):
    """4 桁の短縮 SHA が 2 つ以上のコミットに当たる -> ambiguous（独立したリポジトリで作る）。

    dulwich の短縮 SHA の走査は要求した型（コミット）のオブジェクトだけを数えるので、コミットを直接作る。
    メッセージだけ違うコミットを、4 桁が衝突するまで足す（誕生日のパラドックスで 300 前後。内容が決定的なので結果も決定的）。
    """
    root = tmp_path / "amb"
    root.mkdir()
    repo = porcelain.init(str(root))
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    tree = Tree()
    repo.object_store.add_object(tree)
    seen: dict[str, str] = {}
    prefix = None
    for i in range(20000):
        c = Commit()
        c.tree = tree.id
        c.author = c.committer = WHO
        c.author_time = c.commit_time = T0 + i
        c.author_timezone = c.commit_timezone = 0
        c.message = f"commit {i}".encode()
        repo.object_store.add_object(c)
        sha = c.id.decode("ascii")
        if sha[:4] in seen:
            prefix = sha[:4]
            break
        seen[sha[:4]] = sha
    repo.close()
    assert prefix is not None
    g = GitRepo.open(root)
    assert g is not None
    err = _assert_not_found(g, prefix, "ambiguous")
    assert "ambiguous" in err.message


def test_resolve_in_repository_without_commits(tmp_path: Path):
    root = tmp_path / "empty"
    porcelain.init(str(root)).close()
    g = GitRepo.open(root)
    assert g is not None
    _assert_not_found(g, "HEAD", "not_found")
    assert g.resolve_revision("WORKING_TREE").kind == "working_tree"
    assert g.resolve_revision("STAGED").kind == "staged"


# --- read_blob / read_staged_blob / list_tree ----------------------------------------------


def test_read_blob_past_revisions(fx: Fx):
    g = fx.git
    assert g.read_blob(fx.c1, "src/app.py") == b"A1\n"
    assert g.read_blob(fx.c2, "src/app.py") == b"A2\n"
    assert g.read_blob(fx.c3, "src/app.py") == b"A3\n"
    # 「2 コミット前」: HEAD~2 は c1
    head2 = g.resolve_revision("HEAD~2").sha
    assert head2 is not None
    assert g.read_blob(head2, "README.md") == b"v1\n"
    assert g.read_blob(fx.c3, "README.md") == b"v3\n"  # ステージ・作業ツリーの内容ではなくコミットの内容


def test_read_blob_non_ascii_path_and_exact_bytes(fx: Fx):
    assert fx.git.read_blob(fx.c1, "docs/日本語.md") == b"jp-v1\n"


def test_read_blob_missing_directory_and_through_file(fx: Fx):
    g = fx.git
    assert g.read_blob(fx.c3, "no/such.py") is None
    assert g.read_blob(fx.c3, "src") is None  # ディレクトリ
    assert g.read_blob(fx.c3, "src/") is None
    assert g.read_blob(fx.c3, "README.md/x") is None  # 途中がファイル
    assert g.read_blob(fx.c1, "new_staged.py") is None  # c1 には無い
    assert g.read_blob(fx.c3, "scratch.txt") is None  # 未追跡


@pytest.mark.parametrize(
    "rel",
    [
        "",
        ".",
        "..",
        "../README.md",
        "/README.md",
        "src/../README.md",
        "src//app.py",
        ".git/config",
        "a\\b",
        "C:/x",
    ],
)
def test_read_blob_unsafe_paths_are_none(fx: Fx, rel: str):
    assert fx.git.read_blob(fx.c3, rel) is None


@pytest.mark.parametrize("sha", ["", "HEAD", "abc", "z" * 40, "0" * 40, "1" * 39])
def test_read_blob_bad_commit_sha(fx: Fx, sha: str):
    with pytest.raises(LuciankeError) as ei:
        fx.git.read_blob(sha, "README.md")
    assert ei.value.code is ErrorCode.E_REVISION_NOT_FOUND


def test_read_blob_tree_sha_is_not_a_commit(fx: Fx):
    repo = Repo(str(fx.root))
    try:
        tree_sha = repo[fx.c3.encode()].tree.decode("ascii")
    finally:
        repo.close()
    with pytest.raises(LuciankeError) as ei:
        fx.git.read_blob(tree_sha, "README.md")
    assert ei.value.code is ErrorCode.E_REVISION_NOT_FOUND


def test_read_staged_blob(fx: Fx):
    g = fx.git
    assert g.read_staged_blob("README.md") == b"staged\n"  # ステージ。HEAD は v3、作業ツリーは worktree
    assert g.read_blob(fx.c3, "README.md") == b"v3\n"
    assert (fx.root / "README.md").read_bytes() == b"worktree\n"
    assert g.read_staged_blob("new_staged.py") == b"NS\n"  # ステージだけに入っている新規ファイル
    assert g.read_staged_blob("src/app.py") == b"A3\n"  # 変更なし = HEAD と同じ


def test_read_staged_blob_missing_untracked_directory_unsafe(fx: Fx):
    g = fx.git
    assert g.read_staged_blob("scratch.txt") is None  # 未追跡
    assert g.read_staged_blob("nope/x.py") is None
    assert g.read_staged_blob("src") is None  # ディレクトリ
    assert g.read_staged_blob("../README.md") is None
    assert g.read_staged_blob("") is None
    assert g.read_staged_blob(".git/HEAD") is None


def test_list_tree(fx: Fx):
    g = fx.git
    assert g.list_tree(fx.c1) == [
        ".gitignore",
        "README.md",
        "docs/日本語.md",
        "src/app.py",
        "sub/.gitignore",
        "sub/code.py",
    ]
    assert "new_staged.py" not in g.list_tree(fx.c3)
    assert g.list_tree(fx.c3, "src") == ["src/app.py"]
    assert g.list_tree(fx.c3, "src/") == ["src/app.py"]
    assert g.list_tree(fx.c3, "docs") == ["docs/日本語.md"]
    assert g.list_tree(fx.c3, "no/such") == []
    assert g.list_tree(fx.c3, "README.md") == []  # ファイルはディレクトリではない
    assert g.list_tree(fx.c3, "..") == []
    assert g.list_tree(fx.c3, "") == g.list_tree(fx.c3, ".")


def test_list_staged(fx: Fx):
    g = fx.git
    staged = g.list_staged()
    assert "new_staged.py" in staged and "README.md" in staged
    assert "scratch.txt" not in staged
    assert staged == sorted(staged)
    assert g.list_staged("src") == ["src/app.py"]
    assert g.list_staged("no") == []
    assert g.list_staged("..") == []


def test_hostile_tree_entries_are_skipped(tmp_path: Path):
    """Git のツリー項目に ``..``・``.git``・``\\``・非 UTF-8・シンボリックリンク・サブモジュールがあっても拒否/除外する。"""
    root = tmp_path / "hostile"
    root.mkdir()
    repo = Repo.init(str(root))
    store = repo.object_store
    blob = Blob.from_string(b"x\n")
    store.add_object(blob)
    inner = Tree()
    inner.add(b"fine.py", 0o100644, blob.id)
    inner.add(b"..", 0o100644, blob.id)
    store.add_object(inner)
    dotgit = Tree()
    dotgit.add(b"config", 0o100644, blob.id)
    store.add_object(dotgit)
    top = Tree()
    top.add(b"ok.py", 0o100644, blob.id)
    top.add(b"exec.sh", 0o100755, blob.id)
    top.add(b"link", 0o120000, blob.id)
    top.add(b"submod", 0o160000, b"1" * 40)
    top.add(b"a\\b.py", 0o100644, blob.id)
    top.add(b"\xff.py", 0o100644, blob.id)
    top.add(b"..", 0o100644, blob.id)
    top.add(b".GIT", 0o040000, dotgit.id)
    top.add(b"d", 0o040000, inner.id)
    store.add_object(top)
    c = Commit()
    c.tree = top.id
    c.author = c.committer = WHO
    c.author_time = c.commit_time = T0
    c.author_timezone = c.commit_timezone = 0
    c.message = b"hostile"
    store.add_object(c)
    repo.refs[b"refs/heads/main"] = c.id
    repo.close()

    g = GitRepo.open(root)
    assert g is not None
    sha = c.id.decode("ascii")
    assert g.list_tree(sha) == ["d/fine.py", "exec.sh", "ok.py"]
    assert g.read_blob(sha, "ok.py") == b"x\n"
    assert g.read_blob(sha, "exec.sh") == b"x\n"
    assert g.read_blob(sha, "d/fine.py") == b"x\n"
    for rel in ("link", "submod", "submod/x", "a\\b.py", "..", "d/..", ".GIT/config", ".git/config"):
        assert g.read_blob(sha, rel) is None, rel
    tree = VirtualTree(g, g.resolve_revision("main"))
    assert tree.list() == ["d/fine.py", "exec.sh", "ok.py"]
    assert not tree.exists("link")


# --- is_ignored ----------------------------------------------------------------------------


def test_is_ignored_directories_need_trailing_slash_semantics(fx: Fx):
    g = fx.git
    assert g.is_ignored(".venv", is_dir=True) is True
    # ディレクトリ専用のパターン（`.venv/`）は、実在しない仮想パスを is_dir=False で渡すとファイル扱いになり一致しない
    assert g.is_ignored("ghost/.venv", is_dir=False) is False
    assert g.is_ignored("ghost/.venv", is_dir=True) is True
    assert g.is_ignored("__pycache__", is_dir=True) is True
    assert g.is_ignored("src/__pycache__", is_dir=True) is True
    assert g.is_ignored("src", is_dir=True) is False


def test_is_ignored_files_and_children_of_ignored_dirs(fx: Fx):
    g = fx.git
    assert g.is_ignored(".venv/lib/x.py") is True  # 親が無視されていれば配下も
    assert g.is_ignored("src/__pycache__/m.pyc") is True
    assert g.is_ignored("debug.log") is True
    assert g.is_ignored("deep/er/trace.log") is True
    assert g.is_ignored("README.md") is False
    assert g.is_ignored("src/app.py") is False
    assert g.is_ignored("scratch.txt") is False  # 未追跡でも無視ではない


def test_is_ignored_virtual_paths_need_no_file_on_disk(fx: Fx):
    assert fx.git.is_ignored("ghost/__pycache__/x.pyc") is True
    assert fx.git.is_ignored("ghost/code.py") is False


def test_is_ignored_nested_gitignore(fx: Fx):
    g = fx.git
    assert g.is_ignored("sub/a.gen") is True
    assert g.is_ignored("sub/deep/b.gen") is True
    assert g.is_ignored("other/a.gen") is False  # sub/.gitignore は sub 配下だけ
    assert g.is_ignored("sub/keep.gen") is False  # `!keep.gen` で除外解除
    assert g.is_ignored("sub/code.py") is False


def test_is_ignored_normalizes_input(fx: Fx):
    g = fx.git
    assert g.is_ignored(".venv/", is_dir=True) is True
    assert g.is_ignored("sub\\a.gen") is True  # `\` 区切りも受ける
    for rel in ("", ".", "..", "../x.log", "/abs.log", ".git/config"):
        assert g.is_ignored(rel) is False


# --- head_info -----------------------------------------------------------------------------


def test_head_info(fx: Fx):
    info = fx.git.head_info()
    assert isinstance(info, HeadInfo)
    branch, head_sha, dirty = info
    assert branch == "main"
    assert head_sha == fx.c3
    # README.md（ステージ + 作業ツリーの二重変更は 1 件）・new_staged.py・scratch.txt。無視ファイルは数えない。
    assert dirty == 3


def test_head_info_detached_and_clean(fx_copy: Fx):
    repo = Repo(str(fx_copy.root))
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/feature")
    repo.close()
    assert fx_copy.git.head_info().branch == "feature"
    assert fx_copy.git.head_info().head_sha == fx_copy.c2

    (fx_copy.root / ".git" / "HEAD").write_bytes(f"{fx_copy.c1}\n".encode("ascii"))  # detached HEAD
    info = fx_copy.git.head_info()
    assert info.branch is None
    assert info.head_sha == fx_copy.c1


def test_head_info_without_commits(tmp_path: Path):
    root = tmp_path / "empty"
    repo = porcelain.init(str(root))
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    repo.close()
    _w(root, "new.txt", "x\n")
    g = GitRepo.open(root)
    assert g is not None
    assert g.head_info() == HeadInfo("main", None, 1)


# --- 不変性 --------------------------------------------------------------------------------


def _exercise_everything(g: GitRepo, fx: Fx) -> None:
    for rev in (
        "WORKING_TREE",
        "STAGED",
        "HEAD",
        "HEAD~2",
        "HEAD^",
        "main",
        "feature",
        "origin/main",
        "v1",
        "v1-ann",
        fx.c2[:7],
    ):
        resolved = g.resolve_revision(rev)
        if resolved.sha is not None:
            for rel in ("README.md", "src/app.py", "docs/日本語.md", "missing.py", "src"):
                g.read_blob(resolved.sha, rel)
            g.list_tree(resolved.sha)
            g.list_tree(resolved.sha, "src")
    for bad in ("HEAD@{1}", "HEAD:README.md", "a..b", "nonexistent"):
        with pytest.raises(LuciankeError):
            g.resolve_revision(bad)
    for rel in ("README.md", "new_staged.py", "scratch.txt", "src/app.py"):
        g.read_staged_blob(rel)
    g.list_staged()
    g.list_staged("src")
    for rel, is_dir in ((".venv", True), ("debug.log", False), ("sub/a.gen", False), ("README.md", False)):
        g.is_ignored(rel, is_dir)
    g.head_info()
    ws = make_ws(fx.root)
    for resolved in (
        g.resolve_revision("HEAD~1"),
        g.resolve_revision("STAGED"),
        g.resolve_revision("WORKING_TREE"),
    ):
        src = open_file_source(ws, g, resolved)
        src.list()
        src.list("src")
        src.exists("README.md")
        src.read("src/app.py")


def test_all_operations_leave_index_and_worktree_unchanged(fx_copy: Fx):
    before = snapshot(fx_copy.root)
    index_before = before[".git/index"]
    _exercise_everything(fx_copy.git, fx_copy)
    after = snapshot(fx_copy.root)
    assert after[".git/index"] == index_before
    assert after == before  # .git 全体（HEAD・refs・config・オブジェクト）と作業ツリーの全ファイル


def test_head_info_does_not_rewrite_index_when_stat_is_stale(fx_copy: Fx):
    """内容は同じで mtime だけ変わったファイルがあっても ``status`` は ``index`` を書き換えない（optional_locks=False）。"""
    for rel in ("src/app.py", "sub/code.py", ".gitignore"):
        p = fx_copy.root / rel
        os.utime(p, (T0 + 5000, T0 + 5000))
    before = snapshot(fx_copy.root)
    fx_copy.git.head_info()
    fx_copy.git.head_info()
    assert snapshot(fx_copy.root) == before


def test_virtual_tree_does_not_need_the_working_tree(fx_copy: Fx):
    """過去断面の読み取りは作業ツリーのファイルが消えていても、変わっていても同じ内容を返す。"""
    g = fx_copy.git
    (fx_copy.root / "docs" / "日本語.md").unlink()
    (fx_copy.root / "src" / "app.py").write_bytes(b"CHANGED ON DISK\n")
    tree = VirtualTree(g, g.resolve_revision("v1"))
    assert tree.read("docs/日本語.md") == b"jp-v1\n"
    assert tree.read("src/app.py") == b"A1\n"
    staged = VirtualTree(g, g.resolve_revision("STAGED"))
    assert staged.read("src/app.py") == b"A3\n"


# --- VirtualTree / WorkingTreeSource / open_file_source ---------------------------------------


def test_virtual_tree_commit(fx: Fx):
    g = fx.git
    tree = VirtualTree(g, g.resolve_revision("HEAD~2"))
    assert tree.revision.sha == fx.c1
    assert tree.read("README.md") == b"v1\n"
    assert tree.exists("README.md")
    assert not tree.exists("new_staged.py")
    assert tree.list("src") == ["src/app.py"]
    assert "docs/日本語.md" in tree.list()


def test_virtual_tree_staged(fx: Fx):
    tree = VirtualTree(fx.git, fx.git.resolve_revision("STAGED"))
    assert tree.read("README.md") == b"staged\n"
    assert tree.read("new_staged.py") == b"NS\n"
    assert tree.exists("new_staged.py")
    assert not tree.exists("scratch.txt")
    assert tree.list("src") == ["src/app.py"]


@pytest.mark.parametrize("rev", ["HEAD", "STAGED"])
def test_virtual_tree_missing_file_is_target_not_found(fx: Fx, rev: str):
    tree = VirtualTree(fx.git, fx.git.resolve_revision(rev))
    with pytest.raises(LuciankeError) as ei:
        tree.read("no/such.py")
    err = ei.value
    assert err.code is ErrorCode.E_TARGET_NOT_FOUND
    assert err.details["path"] == "no/such.py"
    assert err.details["param"] == "path"
    assert err.details["revision"] == rev
    assert not tree.exists("no/such.py")
    for unsafe in ("../README.md", "/README.md", ".git/config", ""):
        with pytest.raises(LuciankeError) as ei2:
            tree.read(unsafe)
        assert ei2.value.code is ErrorCode.E_TARGET_NOT_FOUND


def test_virtual_tree_caches_reads_within_a_request(fx: Fx, monkeypatch: pytest.MonkeyPatch):
    g = fx.git
    tree = VirtualTree(g, g.resolve_revision("HEAD"))
    calls: list[str] = []
    original = GitRepo.read_blob

    def spy(self: GitRepo, commit_sha: str, rel: str) -> bytes | None:
        calls.append(rel)
        return original(self, commit_sha, rel)

    monkeypatch.setattr(GitRepo, "read_blob", spy)
    assert tree.exists("README.md")
    assert tree.read("README.md") == b"v3\n"
    assert not tree.exists("zzz")
    assert not tree.exists("zzz")
    assert calls == ["README.md", "zzz"]


def test_virtual_tree_rejects_working_tree_and_shaless_commit(fx: Fx):
    g = fx.git
    with pytest.raises(ValueError):
        VirtualTree(g, g.resolve_revision("WORKING_TREE"))
    with pytest.raises(ValueError):
        VirtualTree(g, ResolvedRevision("commit", None, "x"))


def test_working_tree_source_reads_disk(fx: Fx):
    src = WorkingTreeSource(make_ws(fx.root))
    assert src.read("README.md") == b"worktree\n"  # ディスクの内容（HEAD でもステージでもない）
    assert src.read("docs/日本語.md") == b"jp-v1\n"
    assert src.exists("scratch.txt")
    assert not src.exists("nope.py")
    assert not src.exists("src")  # ディレクトリはファイルではない
    files = src.list()
    assert "scratch.txt" in files and "src/app.py" in files
    assert not any(
        f.startswith((".git/", ".venv/")) or "__pycache__" in f for f in files
    )  # `.git` と exclude_globs
    assert src.list("src") == ["src/app.py"]
    assert files == sorted(files)


def test_working_tree_source_errors(fx: Fx):
    src = WorkingTreeSource(make_ws(fx.root))
    with pytest.raises(LuciankeError) as ei:
        src.read("nope.py")
    assert ei.value.code is ErrorCode.E_TARGET_NOT_FOUND
    assert ei.value.details["path"] == "nope.py"
    with pytest.raises(LuciankeError) as ei:
        src.read("src")  # ディレクトリ
    assert ei.value.code is ErrorCode.E_TARGET_NOT_FOUND
    with pytest.raises(LuciankeError) as ei:
        src.read("../outside.txt")
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT  # confine による封じ込め


def test_open_file_source_picks_by_kind(fx: Fx):
    ws = make_ws(fx.root)
    g = fx.git
    assert isinstance(open_file_source(ws, g, g.resolve_revision("WORKING_TREE")), WorkingTreeSource)
    assert isinstance(open_file_source(ws, g, g.resolve_revision("STAGED")), VirtualTree)
    commit_src = open_file_source(ws, g, g.resolve_revision("v1"))
    assert isinstance(commit_src, VirtualTree)
    assert commit_src.read("README.md") == b"v1\n"
    # 作業ツリーは Git が無くても読める
    assert isinstance(
        open_file_source(ws, None, ResolvedRevision("working_tree", None, "WORKING_TREE")), WorkingTreeSource
    )


def test_open_file_source_without_git_for_past_revision(tmp_path: Path):
    ws = make_ws(tmp_path)
    for resolved in (
        ResolvedRevision("commit", "a" * 40, "HEAD"),
        ResolvedRevision("staged", None, "STAGED"),
    ):
        with pytest.raises(LuciankeError) as ei:
            open_file_source(ws, None, resolved)
        assert ei.value.code is ErrorCode.E_REVISION_NOT_FOUND
        assert ei.value.details["reason"] == "no_git_repository"


# --- 後始末（Windows のハンドル）--------------------------------------------------------------


def test_repo_handles_are_closed_so_files_can_be_renamed(fx_copy: Fx):
    """要求ごとに ``Repo`` を閉じる: 操作の直後に pack/オブジェクトのファイルを改名・削除できる（winerror=32 回避）。"""
    g = fx_copy.git
    _exercise_everything(g, fx_copy)
    pack_dir = fx_copy.root / ".git" / "objects"
    target = pack_dir.parent / "objects_renamed"
    pack_dir.rename(target)  # 開いたままのハンドルがあれば Windows では PermissionError
    target.rename(pack_dir)
    shutil.rmtree(fx_copy.root / ".git" / "refs" / "remotes")  # 同様に refs も掴んでいない


def test_tag_object_is_distinct_from_commit(fx: Fx):
    # v1-ann がタグオブジェクトを指していること（剥がしの検査が意味を持つ前提）
    repo = Repo(str(fx.root))
    try:
        obj = repo[fx.ann_tag_obj.encode()]
        assert isinstance(obj, Tag)
        assert obj.object[1] == fx.c1.encode()
    finally:
        repo.close()
    assert fx.ann_tag_obj != fx.c1
