"""圧縮済み・束ねた生成物の JS / TS は ``files`` 行だけにする（BUG-0119）。

``*.min.js`` の名前、または 32 KB 以上で 1 行の平均が 300 バイト以上の JS / TS は構文木を作らない（シンボル・関係を作らない）。
手で書いた JS・同じ大きさでも行の普通な JS・Python は今までどおり。
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


async def test_minified_bundles_get_a_files_row_only(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root, "vendor/lib.min.js", "function small(a){return a}\n")  # 名前で分かる（小さくても）
    one_line = "".join(f"function f{i}(t){{return f{i - 1}(t)+1}}" for i in range(1, 2000))  # 約 60 KB の 1 行
    _write(root, "dist/bundle.js", one_line + "\n")
    _write(root, "dist/plain.js", one_line.replace("}function", "}\nfunction") + "\n")  # 同じ中身で行が普通
    _write(root, "src/app.js", "export function main() {\n  return helper();\n}\nfunction helper() {\n  return 1;\n}\n")
    _write(root, "src/app.py", "def main() -> int:\n    return 1\n")

    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    indexer = Indexer(Workspace(workspace_id="ws", root=root.resolve()), writer, readers, TreeCache())
    try:
        report = await indexer.scan()
        assert report.errors == [] and report.degraded == []
        with readers.reader() as conn:
            rows = {
                str(path): (str(conf), int(n), int(lines))
                for path, conf, n, lines in conn.execute(
                    "SELECT f.path, f.confidence_level, (SELECT count(*) FROM symbols s WHERE s.file_id = f.file_id),"
                    " f.line_count FROM files f"
                )
            }
        assert rows["vendor/lib.min.js"] == ("lexical_fallback", 0, 1)
        assert rows["dist/bundle.js"] == ("lexical_fallback", 0, 1)
        assert rows["dist/plain.js"][0] == "structural" and rows["dist/plain.js"][1] == 1999
        assert rows["src/app.js"][0] == "structural" and rows["src/app.js"][1] == 2
        assert rows["src/app.py"][0] == "structural" and rows["src/app.py"][1] == 1
    finally:
        await indexer.aclose()
        readers.close()
        writer.close()
