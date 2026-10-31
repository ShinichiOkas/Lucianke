"""後から作られた・消えた・シンボルの集合が変わったファイルを import している側の ``relations`` を解き直す（BUG-0025・BUG-0034。設計書 §7.6.2）。

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
* 既存ファイルの編集でも、**シンボルの集合が変わった**（URN が足された・消えた。``b.py`` に ``helper`` を足した、``helper`` を消した、
  オーバーロードの宣言が増減した）ときは同じ扱いにする（BUG-0034）。import 元の行は、定義元に目標のシンボルがまだ無いと
  ``_settle_relations`` に捨てられ（``_ext`` にもならない）、あとから足されても元に戻らないため。``index_file`` は書き込みの前に
  ``symbols`` の URN の集合と取り込む事実のそれを比べ（``symbols_changed``）、違うときだけ ``prepare_importers`` で import 元の事実を作る。
  **本文だけの編集（集合が同じ）では import 元に触れない**（編集のたびに解析し直す費用と ``exact`` の行を失う副作用を避ける）。
  ``scan`` は取り込んだ結果（``IngestStats.symbols_added`` / ``symbols_removed``）から同じ判定をして ``after_scan`` に渡す。
* **pytest の設定**（BUG-0082）: Python の絶対 import の根は、プロジェクト（ワークスペースのルート・テストフィクスチャのルート）に
  置かれた pytest の設定（``pythonpath``・``--import-mode``。``workspace.pyenv.detect_pytest_imports``）でも変わる。取り込むとき、
  使った設定の指紋を語幹 ``config_stem(project, config)``（``?pytest:<project>#<指紋>``。パスの語幹とは重ならない）として同じ表に書く。
  設定ファイル（``PYTEST_CONFIG_NAMES``）が取り込まれた・作られた・消えたら、覚えた設定を捨てて読み直し、**いまと違う指紋で解いた
  ファイルだけ** を解き直す（``stale_config_importers``）。設定の中身が同じ（pytest に関係ない鍵だけの編集）なら何もしない。
  サーバーが止まっている間の編集も、次の差分走査が設定ファイルを取り込み直したときに同じ判定で拾う（指紋は索引に残っている）。
* **名前空間パッケージのディレクトリ**（BUG-0101）: import の解決が「``__init__.py`` の無いディレクトリか」を調べたら、語幹
  ``?dir:<ディレクトリ>``（``DirProbeRecorder``）を書く。ファイルが作られた・消えたら、その祖先のディレクトリの ``?dir:`` の語幹でも
  引く（ディレクトリの最初のファイル・最後のファイルで、名前空間パッケージができる・無くなる）。
* **再び公開した名前**（BUG-0100）: ファイルの取り込み・削除で、そのファイルの「公開する名前」（最上位のシンボル・束ねた名前の先・
  ``*`` の先・``__all__``。``index/reexports.export_changes``）が変わったら、その名前を連鎖の途中でたどって整えたファイル
  （``_reexport_deps``。``index/reexports.dependents``）も解き直す。取り込み直した側の「公開する名前」も変われば、その依存も続けて
  引く（``reexports.MAX_HOPS`` 段まで。同じトランザクションに入れる）。``__init__.py`` の ``from .helpers import flush`` を
  ``from .impl import flush`` に変えると、``from pkg import flush`` の側は ``pkg/impl.py#flush`` に結び直る。
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Final

from lucianke.core.model import RelPath
from lucianke.index import reexports
from lucianke.index.ingest import FileFacts, ingest_file
from lucianke.workspace.pyenv import PYTEST_CONFIG_NAMES, PytestImports

if TYPE_CHECKING:
    import sqlite3

    from lucianke.engine.indexer import Indexer

__all__ = [
    "IMPORTER_BATCH_FILES",
    "DirProbeRecorder",
    "ProbeRecorder",
    "after_scan",
    "config_stem",
    "dir_stem",
    "export_changes_of",
    "find_importers",
    "importers_to_redo",
    "ingest_importers",
    "is_pytest_config",
    "prepare_importers",
    "stale_config_importers",
    "stem_of",
    "symbols_changed",
]

_log = logging.getLogger(__name__)

IMPORTER_BATCH_FILES: Final = 50
"""取り込み直す側を 1 トランザクションにまとめる最大のファイル数。"""

_SQL_CHUNK: Final = 500
_COLLAPSED_NAMES: Final = frozenset({"__init__", "index"})
_CONFIG_STEM: Final = "?pytest:"
_DIR_STEM: Final = "?dir:"
_CONFIG_NAMES: Final = frozenset(PYTEST_CONFIG_NAMES)


def config_stem(project: str, config: PytestImports) -> str:
    """プロジェクト ``project`` の pytest の設定 ``config`` で import を解いたことを表す語幹（``?pytest:<project>#<指紋>``）。"""
    return f"{_CONFIG_STEM}{project}#{config.fingerprint()}"


def is_pytest_config(path: str) -> bool:
    """``path`` が pytest の設定ファイルの名前か（``pyproject.toml``・``pytest.ini``・``setup.cfg``・``tox.ini`` など）。"""
    return posixpath.basename(str(path)) in _CONFIG_NAMES


def stale_config_importers(
    conn: sqlite3.Connection, workspace_id: str, project: str, current: str, skip: Collection[int] = ()
) -> list[tuple[int, str]]:
    """プロジェクト ``project`` の pytest の設定を、いまの語幹 ``current`` と違う指紋で使って取り込まれたファイルの ``(file_id, path)``。"""
    prefix = f"{_CONFIG_STEM}{project}#"
    upper = prefix[:-1] + chr(ord("#") + 1)  # ``<prefix>`` で始まる語幹の範囲（索引 ``_import_probes_stem`` を使う）
    rows = conn.execute(
        "SELECT DISTINCT p.file_id, f.path FROM _import_probes p JOIN files f ON f.file_id = p.file_id"
        " WHERE f.workspace_id = ? AND p.stem >= ? AND p.stem < ? AND p.stem != ?",
        (workspace_id, prefix, upper, current),
    )
    return sorted(((int(i), str(p)) for i, p in rows if int(i) not in skip), key=lambda kv: kv[1])


def stem_of(path: str) -> str:
    """パスの語幹。拡張子を除き、``__init__`` と ``index`` はその親ディレクトリにする（``pkg/b.py`` と ``pkg/b/__init__.py`` は ``pkg/b``）。"""
    head, _, name = str(path).rpartition("/")
    base = name.rpartition(".")[0] or name
    if base in _COLLAPSED_NAMES:
        return head
    return f"{head}/{base}" if head else base


def dir_stem(path: str) -> str:
    """ディレクトリ ``path`` を「名前空間パッケージか」と調べたことを表す語幹（``?dir:<path>``。パスの語幹とは重ならない）。"""
    return f"{_DIR_STEM}{path}"


def _ancestor_dir_stems(path: str) -> set[str]:
    out: set[str] = set()
    head = posixpath.dirname(str(path))
    while head:
        out.add(dir_stem(head))
        head = posixpath.dirname(head)
    return out


class DirProbeRecorder:
    """``is_dir`` を包み、調べたディレクトリの語幹 ``?dir:<path>`` を集める（``make_resolver(is_dir=...)`` に渡す。BUG-0101）。"""

    __slots__ = ("_is_dir", "stems")

    def __init__(self, is_dir: Callable[[RelPath], bool], sink: set[str]) -> None:
        self._is_dir = is_dir
        self.stems = sink

    def __call__(self, rel: RelPath) -> bool:
        self.stems.add(dir_stem(str(rel)))
        return self._is_dir(rel)


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
    ix: Indexer,
    paths: Iterable[str],
    *,
    skip_file_ids: Collection[int] = (),
    export_changes: Mapping[str, Collection[str]] | None = None,
) -> list[FileFacts]:
    """``paths`` の語幹（と祖先のディレクトリの ``?dir:`` の語幹）を調べて取り込まれたファイル、``export_changes``（パス → 変わった
    公開する名前）をたどって整えたファイル（BUG-0100）を、今のファイルシステムで解析し直した事実（DB には書かない）。

    取り込み直す側の「公開する名前」も変わるなら、その名前に依るファイルも続けて加える（``reexports.MAX_HOPS`` 段まで）。
    読み込めない・消えた・解析に失敗した（``files`` 行だけに落ちる）ものは入れない（いまある行を壊さない。消えた分は監視の ``remove`` が来る）。
    """
    paths = [str(p) for p in paths]
    stems = {stem_of(p) for p in paths}
    for p in paths:
        stems |= _ancestor_dir_stems(p)
    changes = {str(k): set(v) for k, v in (export_changes or {}).items() if v}
    if not paths and not changes:
        return []
    ws_id = ix.workspace.workspace_id
    # pytest の設定ファイルが変わった・できた・消えた: 覚えた設定を捨てて読み直し、違う設定で解いた側も解き直す（BUG-0082）
    projects = sorted({posixpath.dirname(p) for p in paths if is_pytest_config(p)})
    ix.forget_pytest_imports(projects)
    current = {project: config_stem(project, await asyncio.to_thread(ix.pytest_imports, project)) for project in projects}

    def lookup() -> list[tuple[int, str]]:
        with ix._readers.reader() as conn:  # noqa: SLF001
            found = dict(find_importers(conn, ws_id, stems, skip_file_ids)) if stems else {}
            for project, stem in current.items():
                found.update(stale_config_importers(conn, ws_id, project, stem, skip_file_ids))
            found.update(reexports.dependents(conn, ws_id, changes, skip_file_ids))
            return sorted(found.items(), key=lambda kv: kv[1])

    own = set(paths) | set(changes)
    done: set[str] = set(own)
    out: list[FileFacts] = []
    todo = await asyncio.to_thread(lookup)
    for _hop in range(reexports.MAX_HOPS + 1):
        cascade: dict[str, set[str]] = {}
        for _file_id, path in todo:
            if path in done:
                continue  # 自分自身は対象外（除外に変わっただけで実体が残るファイルを、消した直後に取り込み直さない）
            done.add(path)
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
            names = await export_changes_of(ix, path, prepared.facts)
            if names:
                cascade[path] = set(names)
        if not cascade:
            break

        def more(cascade: dict[str, set[str]] = cascade) -> list[tuple[int, str]]:
            with ix._readers.reader() as conn:  # noqa: SLF001
                return reexports.dependents(conn, ws_id, cascade, skip_file_ids)

        todo = await asyncio.to_thread(more)
    return out


async def export_changes_of(ix: Indexer, path: str, facts: FileFacts) -> frozenset[str]:
    """``path`` の取り込み済みの状態と、これから取り込む ``facts`` で「公開する名前」が変わった名前（BUG-0100。``reexports.export_changes``）。

    まだ取り込まれていないファイルは ``ALL_NAMES``。書き込みの前に読み取り接続で調べる。
    """
    ws_id = ix.workspace.workspace_id

    def lookup() -> frozenset[str]:
        with ix._readers.reader() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT file_id FROM files WHERE workspace_id = ? AND path = ?", (ws_id, str(path))
            ).fetchone()
            return reexports.export_changes(
                conn,
                None if row is None else int(row[0]),
                (rec.qualified_name for rec in facts.symbols),
                facts.reexports,
            )

    return await asyncio.to_thread(lookup)


async def symbols_changed(ix: Indexer, path: str, facts: FileFacts) -> bool:
    """``path`` の取り込み済みのシンボルの URN の集合と、これから取り込む ``facts`` のそれが違うか（足された・消えた。中身だけの編集は違わない）。

    まだ取り込まれていない・シンボルが無かったファイルに初めてシンボルができた場合も ``True``。書き込みの前に読み取り接続で調べる。
    """
    ws_id = ix.workspace.workspace_id

    def lookup() -> set[str]:
        with ix._readers.reader() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT s.urn FROM symbols s JOIN files f ON f.file_id = s.file_id WHERE f.workspace_id = ? AND f.path = ?",
                (ws_id, str(path)),
            )
            return {str(urn) for (urn,) in rows}

    return await asyncio.to_thread(lookup) != {str(rec.urn) for rec in facts.symbols}


async def importers_to_redo(ix: Indexer, path: str, facts: FileFacts, *, is_new: bool) -> list[FileFacts]:
    """``index_file`` が ``path`` と同じトランザクションで取り込み直す import 元の事実。

    ファイルが新しい（``is_new``）か、シンボルの集合が変わるか、pytest の設定ファイル（中身が変わった。設定が同じなら解き直す側は
    無い）のときは import 元を、「公開する名前」が変わる（BUG-0100）ときはその名前をたどって整えた側を作る（``[]`` なら触れない）。
    """
    names = frozenset({reexports.ALL_NAMES}) if is_new else await export_changes_of(ix, path, facts)
    redo = is_new or is_pytest_config(path) or await symbols_changed(ix, path, facts)
    if not redo and not names:
        return []
    return await prepare_importers(ix, [path] if redo else [], export_changes={path: names} if names else None)


def ingest_importers(conn: sqlite3.Connection, facts: Iterable[FileFacts]) -> list[int]:
    """``prepare_importers`` の事実を取り込む（書き手スレッドのトランザクションの中で呼ぶ）。取り込んだ ``file_id`` を返す（呼び手が ``_settle_relations`` に渡す）。"""
    return [ingest_file(conn, f).file_id for f in facts]


async def after_scan(
    ix: Indexer,
    *,
    created: Sequence[RelPath],
    removed: Sequence[RelPath],
    skip_file_ids: Collection[int],
    changed: Sequence[RelPath | str] = (),
    configs: Sequence[RelPath | str] = (),
    export_changes: Mapping[str, Collection[str]] | None = None,
) -> None:
    """``scan`` の最後。この走査で新しく見つかった・消えた・シンボルの集合が変わった（``changed``。BUG-0034）ファイルを
    import している側と、取り込み直した pytest の設定ファイル（``configs``）と違う設定で import を解いた側（BUG-0082）と、
    「公開する名前」が変わった（``export_changes``。消えたファイルはすべての名前）名前をたどって整えた側（BUG-0100）のうち、
    この走査で取り込まなかったものを解き直す。

    失敗は握る（走査の成否に影響させない）。``data_version`` は進めない（この走査の取り込み・削除がすでに進めている）。
    """
    from lucianke.engine.indexer import _settle_relations  # 循環 import を避けるため遅延

    try:
        changes: dict[str, set[str]] = {str(k): set(v) for k, v in (export_changes or {}).items() if v}
        for path in removed:
            changes[str(path)] = {reexports.ALL_NAMES}
        facts = await prepare_importers(
            ix,
            dict.fromkeys((*map(str, created), *map(str, removed), *map(str, changed), *map(str, configs))),
            skip_file_ids=skip_file_ids,
            export_changes=changes,
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
            ix.reingested_importers += len(chunk)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("reresolve after scan failed", exc_info=True)
