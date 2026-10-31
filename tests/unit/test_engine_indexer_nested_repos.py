"""入れ子の Git の作業ツリー・リポジトリを索引しない（BUG-0118）。

``.claude/worktrees/*``（並行の役の作業ツリー。``.git`` はファイル）や、中に置いた別のリポジトリ（``.git`` はディレクトリ）の
ファイルは、走査でも 1 ファイルの取り込み（監視・先取り）でも取り込まない。根の ``.gitmodules`` が挙げるサブモジュールは今までどおり。
"""

from __future__ import annotations

from pathlib import Path

from lucianke.engine.indexer import Indexer
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


async def test_nested_worktrees_and_repos_are_not_indexed(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root, "src/app.py", "def main() -> int:\n    return 1\n")
    _write(root, ".claude/worktrees/agent-1/.git", "gitdir: ../../../.git/worktrees/agent-1\n")  # 作業ツリー（.git はファイル）
    _write(root, ".claude/worktrees/agent-1/src/app.py", "def main() -> int:\n    return 2\n")
    (root / "vendor/other/.git").mkdir(parents=True)  # 中に置いた別のリポジトリ（.git はディレクトリ）
    _write(root, "vendor/other/lib.py", "def lib() -> None:\n    pass\n")
    _write(root, ".gitmodules", '[submodule "libs/sub"]\n\tpath = libs/sub\n\turl = https://example.invalid/sub.git\n')
    _write(root, "libs/sub/.git", "gitdir: ../../.git/modules/sub\n")  # サブモジュール: 今までどおり取り込む
    _write(root, "libs/sub/mod.py", "def sub() -> None:\n    pass\n")

    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id="ws", root=root.resolve()), writer, readers, TreeCache())
    try:
        report = await indexer.scan()
        with readers.reader() as conn:
            paths = {str(r[0]) for r in conn.execute("SELECT path FROM files")}
        assert paths == {"src/app.py", ".gitmodules", "libs/sub/mod.py"}, paths
        assert report.files_seen == 3

        # 監視・先取りの 1 ファイルの取り込みも、入れ子の作業ツリーのファイルは取り込まない
        assert await indexer.index_file(".claude/worktrees/agent-1/src/app.py") is None
        assert await indexer.ingest_ahead("vendor/other/lib.py") == "skipped"
        with readers.reader() as conn:
            assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 3
    finally:
        await indexer.aclose()
        readers.close()
        writer.close()
