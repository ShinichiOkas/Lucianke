"""「LSP から取って ``relations`` に ``exact`` で書いた集合は、いまも完全」の目印の永続化（内部表 ``_exact_marks``。設計書 ADR-16）。

``tools/_lsp_exact.py`` の ``MEMO``（プロセス内）と同じ内容 ``(ファイルの指紋, exact 行の指紋)`` を DB にも置く。再起動して
``MEMO`` が空になっても、ファイルが変わっていなければ（``queries.files_fingerprint`` が同じ）、``relations`` の ``exact`` 行が
そのままなら（行の指紋が同じ）、LSP なしで ``exact`` に答えられる。目印は自己検証する: どちらかが違えば使われない。
古い目印を消す規則は要らない（同じ ``(workspace_id, kind, urn)`` は上書きされ、ファイルの指紋が変わった目印は
``store_marks`` が同じワークスペースの分をまとめて消す）。

SQL はこの層に閉じる。読みは読み取り接続、書きは ``IndexWriter`` のジョブ（``data_version`` は進めない）から呼ぶ。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import NamedTuple

__all__ = ["ExactMark", "load_mark", "store_marks"]


class ExactMark(NamedTuple):
    kind: str
    urn: str
    files_token: str
    rows_fp: str


def load_mark(conn: sqlite3.Connection, workspace_id: str, kind: str, urn: str) -> tuple[str, str] | None:
    """``(files_token, rows_fp)``。無い・表が無い（移行前の読み取り）なら ``None``。"""
    try:
        row = conn.execute(
            "SELECT files_token, rows_fp FROM _exact_marks WHERE workspace_id = ? AND kind = ? AND urn = ?",
            (workspace_id, kind, urn),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else (str(row[0]), str(row[1]))


def store_marks(conn: sqlite3.Connection, workspace_id: str, marks: Sequence[ExactMark]) -> int:
    """目印を入れる（同じ組は置き換え）。``marks`` のファイルの指紋と違う指紋の古い目印は、このワークスペースの分を消す
    （指紋が変わった目印は二度と一致しない）。書き込みジョブの中で呼ぶ。入れた件数を返す。"""
    if not marks:
        return 0
    tokens = {m.files_token for m in marks}
    marks_in = ",".join("?" for _ in tokens)
    conn.execute(
        f"DELETE FROM _exact_marks WHERE workspace_id = ? AND files_token NOT IN ({marks_in})",
        (workspace_id, *sorted(tokens)),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO _exact_marks (workspace_id, kind, urn, files_token, rows_fp) VALUES (?, ?, ?, ?, ?)",
        [(workspace_id, m.kind, m.urn, m.files_token, m.rows_fp) for m in marks],
    )
    return len(marks)
