"""T-06 ``find_references_and_implementations`` と T-07 ``query_call_hierarchy`` が共有する LSP ``exact`` 経路の部品。

LSP は PaaD を更新する供給者（設計書 ADR-6・R-11）。ツールは次の順で答える。

1. **PaaD から（2 回目以降）**: 以前 LSP で取って ``relations`` に ``exact`` で書いた集合が **まだ新しい** なら、LSP を呼ばず
   その ``exact`` 行だけで答える（LSP が止まっていても ``exact``）。新しさの目印は ``MEMO``（プロセス内）:
   ``(files_fingerprint, exact 行の指紋)``。ファイルが 1 つでも変わる（索引の ``sha256`` が変わる）と、または別の書き手が同じ
   行を書き換えると目印は外れる。ファイルの再取り込みはそのファイルの ``exact`` 行を消して ``structural`` を入れ直すので、
   「LSP でしか見つからない参照」が編集後に黙って落ちるのを防ぐための条件（行の有無だけを見ると、その穴が開く）。
2. **LSP から（1 回目）**: ``ready`` の LSP に聞き、結果を ``features.to_exact_relations`` で ``RelationFact`` にして
   ``IndexWriter`` へ **1 回のジョブで** 書き戻す（``upsert_exact_relations`` は ``(元, 先, 種別)`` ごとの置換なので、
   同じ組を別ジョブで書くと互いに消す）。書けて読み戻した指紋が一致したときだけ ``MEMO`` に目印を置く。
3. **降格**: LSP が ``ready`` でない・時間切れ・``E_LSP_UNAVAILABLE`` なら ``structural``（今までどおり ``relations`` の全行）。

使用種別・囲みのシンボル・ガード条件は LSP からは来ないので、参照箇所のファイルを構文木にかけて（``syntax.relations`` の
``extract_relations``。索引器と同じ分類）位置から引く。引けない位置は ``REFERENCES`` / ``r`` で囲みはファイルの URN。

``ctx.services`` に求めるもの（duck typing。どれも無ければ構文木だけの ``structural`` で今までどおり動く）:

* ``lsp_pool``: ``LspPool``（``WiredServices`` が持つ）。
* ``runtime(workspace_id)`` が返す束の ``writer``（``IndexWriter``。``WiredServices.runtime()`` が持つ）、または直接 ``writer``。
  無ければ書き戻さず（目印も置かず）、毎回 LSP に聞く。
* ``trees``（``TreeCache``。``ToolServices`` が持つ）: 位置の分類に使う。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import (
    Capability,
    Confidence,
    EngineChoice,
    FileInfo,
    LspStatus,
    Position,
    Range,
    RelPath,
    Tier,
)
from lucianke.core.urn import Urn
from lucianke.engine.selector import is_lsp_degradation, on_lsp_failure, select, starts_lsp
from lucianke.index import exact_marks, queries
from lucianke.index.exact_marks import ExactMark
from lucianke.index.ingest import RelationFact, upsert_exact_relations
from lucianke.lsp import features
from lucianke.lsp.pool import LspHandle
from lucianke.lsp.servers import servers_for
from lucianke.syntax.relations import extract_relations
from lucianke.syntax.support import get_support
from lucianke.tools._services import analyze_file
from lucianke.workspace.paths import to_abs
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.tools.lsp_exact")

__all__ = [
    "EXACT_BUDGET_S",
    "FIRST_DIAGNOSTIC_LANGUAGES",
    "FIRST_DIAGNOSTIC_WAIT_S",
    "MEMO",
    "CallEdgeSource",
    "PRIME_LANGUAGES",
    "PRIME_MAX",
    "LspRoute",
    "PrimeSet",
    "SiteIndex",
    "SiteInfo",
    "cached_rows",
    "confirm_marker",
    "needs_first_diagnostic",
    "needs_priming",
    "await_first_diagnostic",
    "persist_marks",
    "prime_candidates",
    "prime_documents",
    "degraded_choice",
    "is_lsp_failure",
    "lsp_status_of",
    "reference_fact",
    "route_for",
    "row_of",
    "rows_fingerprint",
    "write_facts",
]

LSP_FAILURES = (TimeoutError, LuciankeError)
"""LSP の機能呼び出しが ``exact`` を返せなかったときの例外の型。ツールは握って ``is_lsp_failure`` で確かめ、``structural`` に降格する。
切断（``ConnectionError``）は ``lsp.features`` が ``E_LSP_UNAVAILABLE`` / ``disconnected`` にして上げるので、ここには出てこない。
位置が範囲外の ``E_INVALID_ARGUMENT``（索引がディスクより古い）は ``LuciankeError`` として握り、``is_lsp_failure`` が降格と認める。"""


def is_lsp_failure(exc: BaseException) -> bool:
    """降格すべき失敗か。``engine.selector.is_lsp_degradation`` に従う（降格の規則はセレクターの 1 か所）。

    時間切れ、``E_LSP_UNAVAILABLE`` の ``indexing`` / ``standby`` / ``unavailable`` / ``disconnected`` / ``position_clamped`` /
    ``lsp_error``。索引の位置がディスクの範囲外（``E_INVALID_ARGUMENT`` / ``param=position``）は ``position_clamped`` と同じ扱い。
    それ以外は呼び手の誤りで、握らずに上げる。
    """
    return is_lsp_degradation(_normalized(exc))


def _is_stale_position(exc: BaseException) -> bool:
    """索引の位置がディスクのファイルの範囲を越えている（索引がディスクより古い）。``lsp.features`` が ``E_INVALID_ARGUMENT`` で上げる。"""
    return (
        isinstance(exc, LuciankeError)
        and exc.code is ErrorCode.E_INVALID_ARGUMENT
        and exc.details.get("param") == "position"
    )


def _normalized(exc: BaseException) -> BaseException:
    """位置が範囲外の誤りは、位置を LSP に送れなかった降格（``position_clamped``）として扱う。索引が古いだけで、利用者の誤りではない。"""
    if _is_stale_position(exc):
        return LuciankeError(
            ErrorCode.E_LSP_UNAVAILABLE,
            "the indexed position is outside the file on disk",
            details={"reason": "position_clamped"},
        )
    return exc


def _down_reason(lsp_status: str) -> str:
    """LSP に聞けない経路の ``E_LSP_UNAVAILABLE`` の ``details.reason``（``selector.is_lsp_degradation`` が降格と認める語彙）。"""
    return lsp_status if lsp_status in ("indexing", "standby", "unavailable") else "unavailable"


# --- ctx.services の duck typing ---------------------------------------------------------------


def writer_of(services: Any) -> Any | None:
    """``IndexWriter``。``writer`` 属性、無ければ ``runtime()`` の束の ``writer``。無ければ ``None``。"""
    writer = getattr(services, "writer", None)
    if writer is not None:
        return writer
    runtime = getattr(services, "runtime", None)
    if callable(runtime):
        try:
            rt = runtime()
        except Exception:  # noqa: BLE001  束が無い・開けなかったときは書き戻さない
            return None
        return getattr(rt, "writer", None)
    return None


# --- 経路 ------------------------------------------------------------------------------------


@dataclass(slots=True)
class LspRoute:
    """ファイル 1 つ・能力 1 つの経路。``handle`` があって ``choice.confidence`` が ``exact`` のときだけ LSP に聞ける。"""

    choice: EngineChoice
    handle: LspHandle | None
    file: FileInfo
    capability: Capability

    @property
    def exact(self) -> bool:
        return self.handle is not None and self.choice.confidence is Confidence.EXACT


async def route_for(services: Any, ws: Workspace, file: FileInfo, cap: Capability) -> LspRoute | None:
    """``file`` に ``cap`` を適用する経路。LSP の定義が無い言語・Tier 3 以上・プールが無いときは ``None``（構文木だけ）。

    LSP が ``standby`` なら起動を投げる（待たない。設計書 §7.8.1）。
    """
    pool = getattr(services, "lsp_pool", None)
    language = file.language
    if pool is None or file.tier not in (Tier.T1, Tier.T2) or not servers_for(language, "types"):
        return None
    if starts_lsp(cap):
        await pool.ensure_started(language, ws)
    st = pool.status(language, ws, "types")

    def pick(status: LspStatus, progress: int | None, name: str | None) -> EngineChoice:
        return select(
            file,
            cap,
            lsp_status=status,
            lsp_progress=progress,
            has_grammar=True,
            has_ctags=False,
            lsp_name=name,
        )

    choice = pick(st.status, st.progress, st.server_name)
    handle = None
    if choice.confidence is Confidence.EXACT:
        handle = pool.get(language, ws, role="types", start_if_needed=False)
        if handle is None:  # ready の合図の直後に落ちた
            choice = pick(LspStatus.UNAVAILABLE, None, st.server_name)
    return LspRoute(choice, handle, file, cap)


def lsp_status_of(services: Any, ws: Workspace, file: FileInfo | None) -> str:
    """LSP の今の状態（``_resolution_meta.lsp_status``）。起動はしない。PaaD の ``exact`` 行で答えるときの表示用。"""
    pool = getattr(services, "lsp_pool", None)
    if file is None or not servers_for(file.language, "types"):
        return LspStatus.NOT_APPLICABLE.value
    if pool is None:
        return LspStatus.UNAVAILABLE.value
    return str(pool.status(file.language, ws, "types").status.value)


def degraded_choice(route: LspRoute, exc: BaseException) -> EngineChoice:
    """LSP の呼び出しが ``exact`` を返せなかったときの ``structural`` の選択（その要求に限る）。``selector.on_lsp_failure`` に任せる。

    ``indexing``（LSP が 0 件と答えたが索引に参照がある、を含む）は ``lsp_status=indexing`` と ``lsp_indexing`` の警告、時間切れは
    ``ready`` のまま ``lsp_timeout``、``standby`` / ``unavailable`` / ``disconnected`` は ``unavailable``。
    """
    return on_lsp_failure(route.choice, route.capability, _normalized(exc))


# --- 参照元になりうるファイルを LSP に開かせる（BUG-0047） ------------------------------------------

PRIME_LANGUAGES = frozenset({"typescript", "tsx", "javascript"})
"""tsserver 系の言語 ID。``tsconfig.json`` / ``jsconfig.json`` の ``include`` に入らないファイル（``test/`` など）は、開くまで
どのプロジェクトにも入らず ``references`` / ``callHierarchy`` に出ない。Python（basedpyright）は未オープンの文書も探すので要らない。"""

PRIME_MAX = 100
"""1 回の問い合わせで新しく開く上限（``didOpen`` の LRU 上限 200 の半分。``get_diagnostics`` のワークスペース全体の呼び出しと同じ）。"""


def needs_priming(language: str) -> bool:
    return language in PRIME_LANGUAGES


@dataclass(frozen=True, slots=True)
class PrimeSet:
    """LSP に開かせるファイル（``(RelPath, LSP の languageId)``）と、上限で開けなかった件数。"""

    files: tuple[tuple[RelPath, str], ...] = ()
    skipped: int = 0

    def warning(self, name: str) -> str | None:
        if not self.skipped:
            return None
        return (
            f"{self.skipped} more file(s) mention {name!r} but were not opened for the LSP (limit {PRIME_MAX} per call); "
            "references there may be missing (narrow with scope_paths)"
        )


def prime_candidates(
    conn: sqlite3.Connection,
    ws: Workspace,
    *,
    name: str,
    exclude: str,
    is_open: Callable[[RelPath], bool],
    limit: int = PRIME_MAX,
) -> PrimeSet:
    """``name`` を単語境界で含む tsserver 系のファイル（索引にある Tier 1〜2）のうち、LSP がまだ開いていないもの。

    参照の探す側を LSP に教えるための候補（``features.open_documents`` に渡す）。``exclude``（対象自身のファイル）は除く。
    テストを先に、次にパス順。``limit`` を超えた分は ``skipped`` に数える。読めないファイルは飛ばす。
    """
    try:
        pattern = re.compile(rb"(?<![\w$])" + re.escape(name.encode("utf-8")) + rb"(?![\w$])")
    except re.error:
        return PrimeSet()
    infos = [
        f
        for f in queries.files_in_scope(conn, ws.workspace_id, ())
        if f.language in PRIME_LANGUAGES and f.tier in (Tier.T1, Tier.T2) and str(f.path) != exclude
    ]
    infos.sort(key=lambda f: (not f.is_test, str(f.path)))
    hits: list[tuple[RelPath, str]] = []
    skipped = 0
    for info in infos:
        rel = RelPath(str(info.path))
        if is_open(rel):
            continue
        try:
            data = to_abs(ws, rel).read_bytes()
        except (OSError, ValueError):
            continue
        if not pattern.search(data):
            continue
        if len(hits) >= limit:
            skipped += 1
            continue
        defs = servers_for(info.language, "types")
        hits.append((rel, defs[0].document_language_id if defs else info.language))
    return PrimeSet(tuple(hits), skipped)


async def prime_documents(handle: LspHandle, prime: PrimeSet) -> int:
    """``prime`` のファイルを LSP に開かせる（失敗は ``LSP_FAILURES`` として呼び手が降格する）。開いた件数を返す。"""
    if not prime.files:
        return 0
    return await features.open_documents(handle, prime.files)


# --- 起動直後の部分的な答えを exact として覚えない ---------------------------------------------------

FIRST_DIAGNOSTIC_LANGUAGES = frozenset({"python"})
"""LSP に聞く前に、対象の文書の最初の ``publishDiagnostics`` を待つ言語。basedpyright は ``ready`` でも、その文書の最初の診断が
来るまで解析が済んでいない（BUG-0012）。診断の無いファイルには何も届けないサーバー（vtsls など。``lsp/diagnostics.py`` 参照）は
待っても来ないので対象にしない。"""

FIRST_DIAGNOSTIC_WAIT_S = 5.0
"""最初の診断を待つ上限（秒）。待ちは要求の期限（``LSP_TIMEOUT_*``）とは別で、超えても答えは返す（目印だけ置かない）。"""


def needs_first_diagnostic(language: str) -> bool:
    return language in FIRST_DIAGNOSTIC_LANGUAGES


async def await_first_diagnostic(handle: LspHandle, rel: RelPath, *, timeout_s: float | None = None) -> bool:
    """``rel`` を LSP に見せ（要求は送らない）、その文書の最初の診断が届くまで待つ。届けば ``True``、待ちの上限なら ``False``。

    ``True`` は「LSP がこの文書の解析を済ませた」合図。呼び手はこのときだけ、LSP の答えを ``exact`` の集合として覚える。
    ``False`` でも答えは ``exact`` で返してよいが、目印は置かない（解析前の部分的な答えを固定しない）。
    すでに開いていて診断を受信済みの文書は待たずに ``True``。``ready`` でない・切断は ``E_LSP_UNAVAILABLE``（呼び手が降格する）。
    """
    limit = FIRST_DIAGNOSTIC_WAIT_S if timeout_s is None else min(FIRST_DIAGNOSTIC_WAIT_S, timeout_s)
    sent_at = await features.sync_document(handle, rel)
    if sent_at is None:
        return False
    return bool(
        await handle.diagnostics.wait_for(
            rel, min_version=None, sent_at=sent_at, timeout_ms=max(limit, 0.0) * 1000
        )
    )


# --- 「この集合は完全」の目印 ------------------------------------------------------------------

_MEMO_MAX = 4096


class _Memo:
    """``(db, ws, 種類, urn)`` → ``(ファイルの指紋, exact 行の指紋)``。LRU。スレッド安全。"""

    def __init__(self) -> None:
        self._data: OrderedDict[tuple[str, ...], tuple[str, str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, ...]) -> tuple[str, str] | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: tuple[str, ...], token: str, fingerprint: str) -> None:
        with self._lock:
            self._data[key] = (token, fingerprint)
            self._data.move_to_end(key)
            while len(self._data) > _MEMO_MAX:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


MEMO = _Memo()


def memo_key(db_path: Path | str, ws_id: str, kind: str, urn: str) -> tuple[str, ...]:
    return (str(db_path), ws_id, kind, urn)


def rows_fingerprint(rows: Iterable[queries.ReferenceRow]) -> str:
    items = sorted(
        (
            r.source_urn,
            r.target_urn,
            r.relation_kind,
            str(r.path),
            r.site.line if r.site else -1,
            r.site.col if r.site else -1,
            r.use_kind or "",
            r.guard_condition or "",
        )
        for r in rows
    )
    return hashlib.sha1(repr(items).encode()).hexdigest()


def cached_rows(
    conn: Any,
    db_path: Path | str,
    ws_id: str,
    kind: str,
    urn: str,
    loader: Callable[[Any], Sequence[queries.ReferenceRow]],
    *,
    token: str | None = None,
) -> list[queries.ReferenceRow] | None:
    """目印が新しければ PaaD の ``exact`` 行（``loader`` が読んだ行のうち ``exact`` だけ）。外れていれば ``None``。

    目印は ``MEMO``（プロセス内）、無ければ DB の ``_exact_marks``（``persist_marks`` が書いたもの。再起動しても残る）。
    """
    key = memo_key(db_path, ws_id, kind, urn)
    mark = MEMO.get(key)
    stored = False
    if mark is None:
        mark = exact_marks.load_mark(conn, ws_id, kind, urn)  # 再起動で MEMO が空でも、DB の目印が残っている
        if mark is None:
            return None
        stored = True
    if token is None:
        token = queries.files_fingerprint(conn, ws_id)
    if mark[0] != token:
        return None
    rows = [r for r in loader(conn) if r.confidence is Confidence.EXACT]
    if mark[1] != rows_fingerprint(rows):
        return None
    if stored:
        MEMO.put(key, mark[0], mark[1])
    return rows


def confirm_marker(
    conn: Any,
    db_path: Path | str,
    ws_id: str,
    kind: str,
    urn: str,
    loader: Callable[[Any], Sequence[queries.ReferenceRow]],
    expected: Sequence[queries.ReferenceRow],
    *,
    token: str,
) -> bool:
    """書き戻した行を読み直し、LSP の答えと一致し、書いている間にファイルが変わっていなければ目印を置く。"""
    if queries.files_fingerprint(conn, ws_id) != token:
        return False
    stored = [r for r in loader(conn) if r.confidence is Confidence.EXACT]
    fingerprint = rows_fingerprint(stored)
    if fingerprint != rows_fingerprint(expected):
        return False
    MEMO.put(memo_key(db_path, ws_id, kind, urn), token, fingerprint)
    return True


async def persist_marks(
    services: Any, db_path: Path | str, ws_id: str, keys: Iterable[tuple[str, str]], *, label: str
) -> bool:
    """``confirm_marker`` が ``MEMO`` に置いた目印（``keys`` = ``(kind, urn)``）を DB の ``_exact_marks`` にも書く。書けたら ``True``。

    再起動で ``MEMO`` が消えても、ファイルが変わっていなければ LSP なしで ``exact`` に答えられるようにする（ADR-16）。
    書き手が無い・書けない・``MEMO`` に無い（外れた）ときは何もしない（目印はプロセス内だけ）。``data_version`` は進めない。
    """
    writer = writer_of(services)
    if writer is None:
        return False
    marks = []
    for kind, urn in keys:
        mark = MEMO.get(memo_key(db_path, ws_id, kind, urn))
        if mark is not None:
            marks.append(ExactMark(kind, urn, mark[0], mark[1]))
    if not marks:
        return False
    try:
        await writer.asubmit(
            lambda conn: exact_marks.store_marks(conn, ws_id, marks), bumps_version=False, label=label
        )
    except Exception:  # noqa: BLE001  目印を書けなくても答えは止めない（プロセス内の目印は有効。次の起動でまた LSP に聞く）
        _log.warning("could not persist the exact marks (%s)", label, exc_info=True)
        return False
    return True


# --- 書き戻し --------------------------------------------------------------------------------


async def write_facts(services: Any, facts: Sequence[RelationFact], *, label: str) -> bool:
    """``exact`` の事実を 1 回のジョブで ``relations`` に入れる。書けたら ``True``（書き手が無い・失敗は ``False``）。

    ``data_version`` は進めない（``bumps_version=False``）。進めると、このツール自身が直前に返した ``next_cursor`` が
    次のページで ``E_CURSOR_EXPIRED`` になる（パイプラインは ``data_version`` をハンドラの前に引くため）。書き戻しは
    同じ問い合わせの答えを ``structural`` から ``exact`` に置き換えるだけで、このツールの結果集合は変わらない。
    """
    writer = writer_of(services)
    if writer is None or not facts:
        return False
    rows = list(facts)
    try:
        await writer.asubmit(
            lambda conn: upsert_exact_relations(conn, rows), bumps_version=False, label=label
        )
    except Exception:  # noqa: BLE001  書き戻しの失敗は答えを止めない（次回また LSP に聞く）
        _log.warning("could not write back exact relations (%s)", label, exc_info=True)
        return False
    return True


# --- 参照箇所の分類 --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteInfo:
    """参照箇所 1 つの構文上の情報（索引器と同じ ``extract_relations`` の候補から）。"""

    source_urn: str  # 囲みの定義シンボル（モジュール直下ならファイルの URN）
    relation_kind: str
    use_kind: str
    guard: str | None


@dataclass(slots=True)
class SiteIndex:
    """ファイルごとに ``(行, 列)``（0 始まり）→ ``SiteInfo``。ファイルは 1 度だけ構文木にかける。"""

    services: Any
    ws: Workspace
    _cache: dict[str, dict[tuple[int, int], SiteInfo]] = field(default_factory=dict)

    async def of(self, rel: str) -> dict[tuple[int, int], SiteInfo]:
        if rel not in self._cache:
            self._cache[rel] = await self._load(rel)
        return self._cache[rel]

    async def _load(self, rel: str) -> dict[tuple[int, int], SiteInfo]:
        try:
            an = await analyze_file(self.services, self.ws, rel)
            spec = get_support(an.language_id)
            grammar = spec.grammar()  # type: ignore[union-attr]

            def run() -> dict[tuple[int, int], SiteInfo]:
                cands = extract_relations(
                    an.parsed,
                    language_id=an.language_id,
                    language=grammar,
                    symbols=list(an.symbols),
                    workspace_id=self.ws.workspace_id,
                    rel=an.rel,
                )
                out: dict[tuple[int, int], SiteInfo] = {}
                for c in cands:
                    out.setdefault(
                        (c.call_site_line, c.call_site_col),
                        SiteInfo(c.source_urn, c.relation_kind, c.use_kind, c.guard_condition),
                    )
                return out

            return await asyncio.to_thread(run)
        except Exception:  # noqa: BLE001  分類できないファイルは REFERENCES / r とファイルの URN で扱う
            _log.debug("could not classify the sites of %s", rel, exc_info=True)
            return {}


def reference_fact(
    target_urn: str, rel: str, rng: Range, info: SiteInfo | None, ws_id: str
) -> RelationFact | None:
    """LSP の参照箇所 1 つ → ``exact`` の ``RelationFact``（元 = 囲み、先 = ``target_urn``）。``to_exact_relations`` を通す。"""
    source = info.source_urn if info is not None else str(Urn(ws_id, rel))
    kind = info.relation_kind if info is not None else "REFERENCES"
    use = info.use_kind if info is not None else "r"
    facts = features.to_exact_relations(
        target_urn,
        [(rel, rng)],  # type: ignore[list-item]
        locate=lambda _rel, _rng: source,
        relation_kind=kind,
        use_kind=use,
        incoming=True,
    )
    if not facts:
        return None
    fact = facts[0]
    if info is not None and info.guard and kind == "CALLS":
        fact = replace(fact, guard_condition=info.guard)
    return fact


def row_of(fact: RelationFact, path: str, *, is_test: bool = False) -> queries.ReferenceRow:
    """``RelationFact`` を ``references_to`` / ``references_from`` が返す形の行にする（読み戻しと同じ指紋にするため）。"""
    site = (
        None
        if fact.call_site_line is None
        else Position(fact.call_site_line, max(fact.call_site_col or 0, 0))
    )
    return queries.ReferenceRow(
        source_urn=str(fact.source_urn),
        target_urn=str(fact.target_urn),
        relation_kind=fact.relation_kind,
        path=path,  # type: ignore[arg-type]
        site=site,
        use_kind=fact.use_kind,
        guard_condition=fact.guard_condition,
        confidence=Confidence.EXACT,
        is_test=is_test,
    )


# --- T-07: 呼び出し階層の辺の供給 --------------------------------------------------------------

EXACT_BUDGET_S = 10.0
"""1 回の ``query_call_hierarchy`` で LSP に使う時間の合計。超えたら ``TimeoutError`` → ``structural`` に降格する。"""

_ASK_SLOW_S = 0.5
"""BUG-0058: 1 ノードの LSP 呼び出し（``_ask``）がこの秒数を超えたら、各段の内訳をログに落とす（閾値ガード付きの計時）。"""


def _external_urn(item: features.CallItem) -> str:
    """ワークスペース外の呼び先（標準ライブラリ・site-packages）の URN。ファイルのパスからパッケージ名を推定する（最善努力）。"""
    uri = item.external_uri or ""
    parts = [p for p in uri.replace("\\", "/").split("/") if p]
    package = "unknown"
    for marker in ("stdlib", "site-packages", "dist-packages"):
        if marker in parts:
            rest = parts[len(parts) - 1 - parts[::-1].index(marker) + 1 :]
            if rest:
                package = rest[0].rsplit(".", 1)[0]
            break
    else:
        if parts:
            package = parts[-1].rsplit(".", 1)[0]
    return str(Urn.external(package, None, item.name))


class CallEdgeSource:
    """T-07 の ``exact`` の辺（``CALLS``）の供給。ワーカースレッドから同期で呼ぶ（``asyncio.to_thread`` の中）。

    ノード（URN x 向き）ごとに、PaaD の ``exact`` 行（目印が新しいとき）か、LSP の呼び出し階層
    （``prepareCallHierarchy`` -> ``incomingCalls`` / ``outgoingCalls``。イベントループへ ``run_coroutine_threadsafe``）から
    ``references_to`` / ``references_from`` と同じ形の行を返す。LSP から取った分は ``staged`` / ``staged_rows`` に溜め、
    木ができたあと呼び手が 1 回のジョブで書き戻して目印を置く（``flush``）。
    失敗（時間切れ・``E_LSP_UNAVAILABLE``）はそのまま上げる（呼び手が木ごと ``structural`` に降格する）。
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        services: Any,
        ws: Workspace,
        db_path: Path | str,
        conn: Any,
        route: LspRoute,
        token: str,
        roots: Iterable[str],
        budget_s: float = EXACT_BUDGET_S,
    ) -> None:
        self.loop, self.services, self.ws, self.db_path, self.conn = loop, services, ws, db_path, conn
        self.route, self.token = route, token
        self.roots = set(roots)
        self.deadline = time.monotonic() + budget_s
        self.sites = SiteIndex(services, ws)
        self.staged: list[RelationFact] = []
        self.staged_rows: dict[tuple[str, str], list[queries.ReferenceRow]] = {}
        self.skipped = 0
        self.from_lsp = 0
        self.warnings: list[str] = []
        """exact 経路で足す警告（LSP に開かせるファイルの上限など）。呼び手は exact で答えたときだけ出す。"""
        self._rows: dict[tuple[str, str], list[queries.ReferenceRow]] = {}
        self._test: dict[str, bool] = {}
        self._settled: dict[str, bool] = {}
        """ノードの定義ファイル → LSP がそのファイルの最初の診断を出したか（解析が済んだ合図。出ていないノードの答えは目印にしない）。"""
        self._node_path: dict[tuple[str, str], str] = {}

    # --- 公開 ---

    def rows(self, urn: str, direction: str, include_tests: bool) -> list[queries.ReferenceRow]:
        key = (urn, direction)
        if key not in self._rows:
            self._rows[key] = self._load(urn, direction)
        rows = self._rows[key]
        return rows if include_tests else [r for r in rows if not r.is_test]

    async def flush(self) -> bool:
        """LSP から取った辺を書き戻し、読み戻して一致したノードに目印を置く。書けたら ``True``。"""
        if not self.staged or not await write_facts(self.services, self.staged, label="lsp:call_hierarchy"):
            return False
        confirmed = await asyncio.to_thread(self._confirm_all)
        await persist_marks(
            self.services, self.db_path, self.ws.workspace_id, confirmed, label="lsp:call_hierarchy:marks"
        )
        return True

    # --- 内部（スレッド） ---

    @staticmethod
    def _kind(direction: str) -> str:
        return "calls_in" if direction == "incoming" else "calls_out"

    @staticmethod
    def _loader(urn: str, direction: str) -> Callable[[Any], Sequence[queries.ReferenceRow]]:
        if direction == "incoming":
            return lambda c: queries.references_to(c, urn, ("CALLS",))
        return lambda c: queries.references_from(c, urn, ("CALLS",))

    def _is_test(self, path: str) -> bool:
        if path not in self._test:
            info = queries.file_by_path(self.conn, self.ws.workspace_id, path)
            self._test[path] = bool(info and info.is_test)
        return self._test[path]

    def _load(self, urn: str, direction: str) -> list[queries.ReferenceRow]:
        ws_id = self.ws.workspace_id
        loader = self._loader(urn, direction)
        cached = cached_rows(
            self.conn, self.db_path, ws_id, self._kind(direction), urn, loader, token=self.token
        )
        if cached is not None:
            return cached
        sym = queries.symbol_by_urn(self.conn, urn)
        if sym is None or sym.path is None:
            return []  # モジュール直下（ファイルの URN）: 呼ばれることはなく、LSP にも項目が無い
        if self.route.handle is None:
            raise LuciankeError(
                ErrorCode.E_LSP_UNAVAILABLE,
                "the LSP is not ready and the index has no fresh exact calls for this node",
                details={"reason": _down_reason(self.route.choice.lsp_status)},
            )
        self._node_path[(self._kind(direction), urn)] = str(sym.path)
        prime = self._prime_set(sym) if direction == "incoming" else None
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the LSP call hierarchy budget was used up")
        fut = asyncio.run_coroutine_threadsafe(self._ask(sym, direction, prime), self.loop)
        try:
            edges, infos = fut.result(timeout=remaining)
        except TimeoutError:
            fut.cancel()
            raise
        rows = self._assemble(urn, direction, edges, infos)
        if not rows and urn in self.roots and self._structural_count(urn, direction):
            # 起動直後の LSP は ready でも解析が済まず 0 件を返すことがある。索引に呼び出しがあるのに 0 件なら信用しない。
            raise LuciankeError(
                ErrorCode.E_LSP_UNAVAILABLE,
                "the LSP found no calls although the index has some; it may not have finished analysing",
                details={"reason": "indexing"},
            )
        self.from_lsp += 1
        return rows

    def _prime_set(self, sym: Any) -> PrimeSet | None:
        """incoming の呼び元になりうるファイル（tsserver 系で、対象の名前を含み、LSP がまだ開いていないもの）。ワーカースレッドで引く。

        tsserver は ``tsconfig.json`` の ``include`` に入らないファイル（``test/`` など）を、開かれるまで ``callHierarchy`` の
        呼び元に出さない（T-06 の BUG-0047 と同じ）。
        """
        handle = self.route.handle
        if handle is None or not needs_priming(self.route.file.language):
            return None
        return prime_candidates(
            self.conn,
            self.ws,
            name=sym.name,
            exclude=str(sym.path),
            is_open=handle.documents.is_open,
            limit=PRIME_MAX,
        )

    def _structural_count(self, urn: str, direction: str) -> int:
        return len(self._loader(urn, direction)(self.conn))

    def _urn_at(self, rel: Any, pos: Position) -> str | None:
        if rel is None:
            return None
        sym = queries.symbol_at(self.conn, self.ws.workspace_id, str(rel), pos.line, pos.col)
        return None if sym is None else str(sym.urn)

    def _assemble(
        self,
        urn: str,
        direction: str,
        edges: Sequence[features.CallEdge],
        infos: dict[str, dict[tuple[int, int], SiteInfo]],
    ) -> list[queries.ReferenceRow]:
        incoming = direction == "incoming"
        facts: list[RelationFact] = []
        rows: list[queries.ReferenceRow] = []
        for edge in edges:
            home = edge.from_rel  # 呼び出し箇所のファイル（incoming は呼び元、outgoing は問い合わせた項目）
            if home is None or not edge.from_ranges:
                continue
            other = edge.item
            if not incoming and other.rel is None:
                other_urn: str | None = _external_urn(other)
            else:
                other_urn = self._urn_at(other.rel, other.selection_range.start)
            if other_urn is None:
                self.skipped += 1
                continue
            for rng in edge.from_ranges:
                info = infos.get(str(home), {}).get((rng.start.line, rng.start.col))
                if incoming:
                    made = features.to_exact_relations(
                        urn,
                        [(home, rng)],
                        locate=lambda _r, _g, u=other_urn: u,
                        relation_kind="CALLS",
                        use_kind="call",
                        incoming=True,
                    )
                else:
                    made = features.to_exact_relations(
                        urn,
                        [(home, other.selection_range)],
                        locate=lambda _r, _g, u=other_urn: u,
                        relation_kind="CALLS",
                        use_kind="call",
                        call_site=rng.start,
                    )
                if not made:
                    continue
                fact = made[0]
                if info is not None and info.guard and info.relation_kind == "CALLS":
                    fact = replace(fact, guard_condition=info.guard)
                facts.append(fact)
                rows.append(row_of(fact, str(home), is_test=self._is_test(str(home))))
        self.staged.extend(facts)
        self.staged_rows[(self._kind(direction), urn)] = rows
        return rows

    def _confirm_all(self) -> list[tuple[str, str]]:
        """読み戻して一致したノードに目印を置き、置いた ``(kind, urn)`` を返す。最初の診断が出ていないファイルのノードは置かない。"""
        from lucianke.index.connection import open_reader  # 読み取り接続はこのスレッドで開く

        conn = open_reader(self.db_path)
        confirmed: list[tuple[str, str]] = []
        try:
            for (kind, urn), rows in self.staged_rows.items():
                if not self._settled.get(self._node_path.get((kind, urn), ""), False):
                    continue  # 解析前の部分的な答えかもしれない: exact で答えるが覚えない
                direction = "incoming" if kind == "calls_in" else "outgoing"
                if confirm_marker(
                    conn,
                    self.db_path,
                    self.ws.workspace_id,
                    kind,
                    urn,
                    self._loader(urn, direction),
                    rows,
                    token=self.token,
                ):
                    confirmed.append((kind, urn))
        finally:
            conn.close()
        return confirmed

    # --- 内部（イベントループ） ---

    async def _settle(self, rel: RelPath) -> bool:
        """``rel`` の最初の診断を待つ（ファイルごとに 1 度。待ちは ``EXACT_BUDGET_S`` の残りの内）。"""
        key = str(rel)
        if key not in self._settled:
            handle = self.route.handle
            assert handle is not None
            if needs_first_diagnostic(self.route.file.language):
                left = max(self.deadline - time.monotonic(), 0.0)
                self._settled[key] = await await_first_diagnostic(handle, rel, timeout_s=left)
            else:
                self._settled[key] = True
        return self._settled[key]

    async def _ask(
        self, sym: Any, direction: str, prime: PrimeSet | None = None
    ) -> tuple[list[features.CallEdge], dict[str, dict[tuple[int, int], SiteInfo]]]:
        handle = self.route.handle
        assert handle is not None
        # BUG-0058: LSP 経路の各段の所要（秒）。合計が閾値を超えた呼び出しだけ内訳をログに落とす（閾値ガード付き）。
        spans: dict[str, float] = {}
        t = time.monotonic()
        if prime is not None:
            await prime_documents(handle, prime)
            note = prime.warning(sym.name)
            if note and note not in self.warnings:
                self.warnings.append(note)
        spans["prime"], t = time.monotonic() - t, time.monotonic()
        await self._settle(sym.path)
        spans["settle"], t = time.monotonic() - t, time.monotonic()
        start = (sym.selection or sym.range).start
        items = await features.prepare_call_hierarchy(handle, sym.path, start)
        spans["prepare"], t = time.monotonic() - t, time.monotonic()
        item = next(
            (i for i in items if i.rel == sym.path and i.selection_range.start == start),
            next((i for i in items if i.rel == sym.path), None),
        )
        if item is None:
            raise LuciankeError(
                ErrorCode.E_LSP_UNAVAILABLE,
                f"the LSP has no call hierarchy item for {sym.qualified_name}",
                details={"reason": "lsp_error"},
            )
        call = features.incoming_calls if direction == "incoming" else features.outgoing_calls
        edges = await call(handle, item)
        spans["calls"], t = time.monotonic() - t, time.monotonic()
        infos: dict[str, dict[tuple[int, int], SiteInfo]] = {}
        for rel in sorted({str(e.from_rel) for e in edges if e.from_rel is not None}):
            infos[rel] = await self.sites.of(rel)
        spans["sites"] = time.monotonic() - t
        total = sum(spans.values())
        if total >= _ASK_SLOW_S:
            _log.info(
                "slow LSP call hierarchy: %s %s total=%.0fms %s",
                sym.qualified_name,
                direction,
                total * 1000,
                " ".join(f"{k}={v * 1000:.0f}ms" for k, v in spans.items()),
                extra={"event": "lsp_call_hierarchy_slow"},
            )
        return edges, infos
