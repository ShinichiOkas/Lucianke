"""後から作られた・消えたファイルを import している側の ``relations`` を解き直す（BUG-0025。設計書 §7.6.2）。

import の解決は、定義元のファイルの有無を **取り込んだ時点のファイルシステム** で決める（``Indexer._exists``）。そのため、
import 元 ``a.py`` を取り込んだあとで ``b.py`` が作られても、``a.py`` の行は ``lk://_ext/pkg#b.helper``（外部）のまま、
あるいは（相対 import なら）行そのものが無いまま残る。逆に ``b.py`` が消えると、``b.py`` を指していた行が宙に浮く。

* 取り込むとき、``resolve_module`` が調べたパス（見つかった・見つからなかった両方）の **語幹** を ``_import_probes``
  （``file_id``, ``stem``）に書く（``ProbeRecorder`` → ``FileFacts.import_stems`` → ``ingest_file``）。語幹はパスから拡張子と
  ``__init__`` / ``index`` を除いたもの（``pkg/b.py`` も ``pkg/b/__init__.py`` も ``pkg/b``）。
* ファイル ``P`` が作られた・消えたら、``stem_of(P)`` を持つ ``_import_probes`` の行から import している側を引き、
  **その範囲だけ** 解析し直して取り込み直す。全体の再走査はしない。``index_file`` / ``remove`` では ``P`` の取り込み・削除と同じ
  トランザクションに入れる（``prepare_importers`` で書き込みの前に事実を作り、``ingest_importers`` で書く。``data_version`` は 1 回だけ進む）。
  ``scan`` は最後に、この走査で取り込まなかった側を別のトランザクション（``data_version`` は進めない。``_settle_relations`` と同じ扱い）で解き直す。
  取り込み直した側は、同じ内容でも ``exact`` の行を失い ``structural`` に戻る（ADR-8。次に LSP が答えたとき戻る）。
* 語幹の一致は ``P`` を調べたパスと同じ語幹を持つという意味で、実際に ``P`` に解決されるとは限らない（大目に引く）。
  取り込み直した結果が前と同じなら行は変わらない。
* 既存ファイルに **シンボルが増えた** だけの場合（``b.py`` に ``helper`` を足した）は解き直さない（BUG-0030）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Final

from lucianke.core.model import RelPath
from lucianke.index.ingest import FileFacts, ingest_file

if TYPE_CHECKING:
    import sqlite3

    from lucianke.engine.indexer import Indexer

__all__ = [
    "IMPORTER_BATCH_FILES",
    "ProbeRecorder",
    "after_scan",
    "find_importers",
    "ingest_importers",
    "prepare_importers",
    "stem_of",
]

_log = logging.getLogger(__name__)

IMPORTER_BATCH_FILES: Final = 50
"""取り込み直す側を 1 トランザクションにまとめる最大のファイル数。"""

_SQL_CHUNK: Final = 500
_COLLAPSED_NAMES: Final = frozenset({"__init__", "index"})


def stem_of(path: str) -> str:
    """パスの語幹。拡張子を除き、``__init__`` と ``index`` はその親ディレクトリにする（``pkg/b.py`` と ``pkg/b/__init__.py`` は ``pkg/b``）。"""
    head, _, name = str(path).rpartition("/")
    base = name.rpartition(".")[0] or name
    if base in _COLLAPSED_NAMES:
        return head
    return f"{head}/{base}" if head else base


class ProbeRecorder:
    """``exists`` を包み、調べたパスの語幹を集める（``make_resolver(exists=...)`` に渡す。見つかった・見つからなかった両方）。"""

    __slots__ = ("_exists", "stems")

    def __init__(self, exists: Callable[[RelPath], bool], sink: set[str]) -> None:
        self._exists = exists
        self.stems = sink

    def __call__(self, rel: RelPath) -> bool:
        self.stems.add(stem_of(str(rel)))
        return self._exists(rel)


def find_importers(
    conn: sqlite3.Connection, workspace_id: str, stems: Collection[str], skip: Collection[int] = ()
) -> list[tuple[int, str]]:
    """``stems`` のどれかを調べて取り込まれたファイルの ``(file_id, path)``。パス順。"""
    found: dict[int, str] = {}
    ordered = sorted(stems)
    for i in range(0, len(ordered), _SQL_CHUNK):
        chunk = ordered[i : i + _SQL_CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT DISTINCT p.file_id, f.path FROM _import_probes p JOIN files f ON f.file_id = p.file_id"
            f" WHERE f.workspace_id = ? AND p.stem IN ({marks})",
            (workspace_id, *chunk),
        )
        for file_id, path in rows:
            if file_id not in skip:
                found[int(file_id)] = str(path)
    return sorted(found.items(), key=lambda kv: kv[1])


async def prepare_importers(
    ix: Indexer, paths: Iterable[str], *, skip_file_ids: Collection[int] = ()
) -> list[FileFacts]:
    """``paths`` の語幹を調べて取り込まれたファイルを今のファイルシステムで解析し直した事実（DB には書かない）。

    読み込めない・消えた・解析に失敗した（``files`` 行だけに落ちる）ものは入れない（いまある行を壊さない。消えた分は監視の ``remove`` が来る）。
    """
    stems = {stem_of(p) for p in paths}
    if not stems:
        return []
    ws_id = ix.workspace.workspace_id

    def lookup() -> list[tuple[int, str]]:
        with ix._readers.reader() as conn:  # noqa: SLF001
            return find_importers(conn, ws_id, stems, skip_file_ids)

    own = {str(p) for p in paths}
    out: list[FileFacts] = []
    for _file_id, path in await asyncio.to_thread(lookup):
        if path in own:
            continue  # 自分自身は対象外（除外に変わっただけで実体が残るファイルを、消した直後に取り込み直さない）
        try:
            prepared = await ix._prepare(RelPath(path), None)  # noqa: SLF001
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("reresolve: could not prepare %s", path, exc_info=True)
            continue
        if prepared is None or prepared == "gone" or prepared.degraded:
            continue
        out.append(prepared.facts)
    return out


def ingest_importers(conn: sqlite3.Connection, facts: Iterable[FileFacts]) -> list[int]:
    """``prepare_importers`` の事実を取り込む（書き手スレッドのトランザクションの中で呼ぶ）。取り込んだ ``file_id`` を返す（呼び手が ``_settle_relations`` に渡す）。"""
    return [ingest_file(conn, f).file_id for f in facts]


async def after_scan(
    ix: Indexer, *, created: Sequence[RelPath], removed: Sequence[RelPath], skip_file_ids: Collection[int]
) -> None:
    """``scan`` の最後。この走査で新しく見つかった・消えたファイルを import している側のうち、この走査で取り込まなかったものを解き直す。

    失敗は握る（走査の成否に影響させない）。``data_version`` は進めない（この走査の取り込み・削除がすでに進めている）。
    """
    from lucianke.engine.indexer import _settle_relations  # 循環 import を避けるため遅延

    try:
        facts = await prepare_importers(
            ix, (*map(str, created), *map(str, removed)), skip_file_ids=skip_file_ids
        )
        for start in range(0, len(facts), IMPORTER_BATCH_FILES):
            chunk = facts[start : start + IMPORTER_BATCH_FILES]

            def job(conn: object, chunk: list = chunk) -> int:
                ids = ingest_importers(conn, chunk)  # type: ignore[arg-type]
                _settle_relations(conn, ids)
                return len(ids)

            await ix._writer.asubmit(  # noqa: SLF001
                job, bumps_version=False, label=f"reresolve:{chunk[0].path}+{len(chunk) - 1}"
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("reresolve after scan failed", exc_info=True)
