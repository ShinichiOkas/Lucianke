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
   T-07 は木のノードごと: 1 問い合わせの予算 ``EXACT_BUDGET_S`` を使い切る・要求が期限で切れると、LSP が答えたノードは
   ``exact`` のまま、残りのノードの子を ``relations`` で補う（``mixed``。根の子が取れなければ木ごと ``structural``）。警告には
   切れたのが予算か要求の期限か・その値・どこまで ``exact`` か・次に何をすればよいかを書く（``CallEdgeSource``。BUG-0091）。

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
from lucianke.core.limits import LSP_OPEN_DOCS_MAX
from lucianke.core.model import (
    Capability,
    Confidence,
    EngineChoice,
    FileInfo,
    Kind,
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
from lucianke.lsp.pyright_scope import PyrightScope
from lucianke.lsp.servers import servers_for
from lucianke.syntax.relations import extract_relations
from lucianke.syntax.support import get_support
from lucianke.tools._services import analyze_file
from lucianke.workspace.languages import detect
from lucianke.workspace.languages import is_test as is_test_path
from lucianke.workspace.paths import to_abs
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.tools.lsp_exact")

__all__ = [
    "EXACT_BACKGROUND_S",
    "EXACT_BUDGET_S",
    "INFLIGHT_MAX",
    "ExactBudget",
    "budget_timeout_note",
    "budget_timeout_warning",
    "EXACT_LANES",
    "FIRST_DIAGNOSTIC_LANGUAGES",
    "FIRST_DIAGNOSTIC_WAIT_S",
    "MEMO",
    "CallEdgeSource",
    "ExactBudgetExceeded",
    "ExactCut",
    "PRIME_LANGUAGES",
    "TS_FAMILY",
    "PRIME_MAX",
    "LspRoute",
    "PrimeSet",
    "SITES",
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
    "prime_set",
    "preopen_set",
    "degraded_choice",
    "is_lsp_failure",
    "log_slow_spans",
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

    時間切れ、``E_LSP_UNAVAILABLE`` の ``indexing`` / ``restarting`` / ``standby`` / ``unavailable`` / ``disconnected`` /
    ``position_clamped`` / ``lsp_error``。索引の位置がディスクの範囲外（``E_INVALID_ARGUMENT`` / ``param=position``）は ``position_clamped`` と同じ扱い。
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
        elif language == "python":
            # 根の外の ``extends`` の先など、監視が見ない設定ファイルの変更を、聞く前にサーバーへ届けて対象を読み直す（BUG-0135）
            await pool.sync_config_chain(handle)
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

TS_FAMILY = frozenset({"typescript", "tsx", "javascript"})
"""tsserver 系の言語 ID。``tsconfig.json`` / ``jsconfig.json`` の ``include`` に入らないファイル（``test/`` など）は、開くまで
どのプロジェクトにも入らず ``references`` / ``callHierarchy`` に出ない（BUG-0047）。"""

PRIME_LANGUAGES = TS_FAMILY | {"python"}
"""参照元になりうるファイルを先に LSP へ開かせる言語。Python（basedpyright）は解析の対象（``include`` / ``exclude``。
``lsp/pyright_scope``）の内なら未オープンの文書も探すが、対象の外は開いた文書しか探さない（利用者の設定が ``include: ["src"]``
なら ``tests/`` の呼び出しが答えに入らないまま ``exact`` で返っていた。BUG-0132）。Python は対象の外のファイルだけを開かせる。"""

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


@dataclass(frozen=True, slots=True)
class _Listed:
    """走査の列挙から判定したファイル（``FileInfo`` のうち、開かせる候補を選ぶのに使う 4 つ）。"""

    path: str
    language: str
    tier: Tier
    is_test: bool


def _listed(listing: Iterable[RelPath]) -> list[_Listed]:
    """走査の列挙を、索引が記録するのと同じ判定（``workspace.languages.detect`` の拡張子・名前による言語とティア、
    言語サポートの追加パターンつきの ``is_test``）で ``_Listed`` にする。中身は読まない（先頭のバイト列による判定〈シェバン・
    バイナリ〉はしない）。"""
    out: list[_Listed] = []
    for rel in listing:
        detected = detect(rel, b"")
        support = get_support(detected.language_id)
        extra = tuple(getattr(support, "test_path_globs", ()) or ())
        out.append(_Listed(str(rel), detected.language_id, detected.tier, is_test_path(rel, extra)))
    return out


def _prime_pool(
    conn: sqlite3.Connection | None,
    ws: Workspace,
    *,
    exclude: str,
    languages: frozenset[str],
    outside: Callable[[RelPath], bool] | None,
    files: Sequence[FileInfo | _Listed] | None = None,
) -> list[FileInfo | _Listed]:
    """開かせる候補になりうるファイル: 索引の Tier 1〜2 の ``languages`` のファイル（``exclude`` を除き、``outside`` を渡すと
    それが真のものだけ）。テストを先に、次にパス順。``files`` を渡すと索引の代わりにそれ（走査の列挙。``_listed``）から選ぶ。"""
    source: Sequence[FileInfo | _Listed] = (
        files if files is not None else queries.files_in_scope(conn, ws.workspace_id, ())  # type: ignore[arg-type]
    )
    infos = [
        f
        for f in source
        if f.language in languages
        and f.tier in (Tier.T1, Tier.T2)
        and str(f.path) != exclude
        and (outside is None or outside(RelPath(str(f.path))))
    ]
    infos.sort(key=lambda f: (not f.is_test, str(f.path)))
    return infos


def _document_language_id(language: str) -> str:
    defs = servers_for(language, "types")
    return defs[0].document_language_id if defs else language


def prime_candidates(
    conn: sqlite3.Connection,
    ws: Workspace,
    *,
    name: str,
    exclude: str,
    is_open: Callable[[RelPath], bool],
    limit: int = PRIME_MAX,
    languages: frozenset[str] = TS_FAMILY,
    outside: Callable[[RelPath], bool] | None = None,
) -> PrimeSet:
    """``name`` を単語境界で含む ``languages`` のファイル（索引にある Tier 1〜2）のうち、LSP がまだ開いていないもの。

    参照の探す側を LSP に教えるための候補（``features.open_documents`` に渡す）。``exclude``（対象自身のファイル）は除く。
    ``outside`` を渡すと、それが真のファイル（LSP の解析の対象の外）だけ（中身を読む前に絞る）。
    テストを先に、次にパス順。``limit`` を超えた分は ``skipped`` に数える。読めないファイルは飛ばす。
    """
    try:
        pattern = re.compile(rb"(?<![\w$])" + re.escape(name.encode("utf-8")) + rb"(?![\w$])")
    except re.error:
        return PrimeSet()
    hits: list[tuple[RelPath, str]] = []
    skipped = 0
    for info in _prime_pool(conn, ws, exclude=exclude, languages=languages, outside=outside):
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
        hits.append((rel, _document_language_id(info.language)))
    return PrimeSet(tuple(hits), skipped)


def _prime_rule(
    ws: Workspace, language: str, scope: PyrightScope | None
) -> tuple[frozenset[str], Callable[[RelPath], bool] | None] | None:
    """``language`` の LSP に開かせる候補の言語と絞り込み（``prime_set`` / ``preopen_set`` の共通の規則）。開かせないなら ``None``。"""
    if language in TS_FAMILY:
        return TS_FAMILY, None
    if language != "python" or scope is None or not scope.narrowed:
        return None
    inside = scope.contains
    return frozenset({"python"}), lambda rel: not inside(to_abs(ws, rel))


def prime_set(
    conn: sqlite3.Connection,
    ws: Workspace,
    language: str,
    *,
    name: str,
    exclude: str,
    is_open: Callable[[RelPath], bool],
    limit: int = PRIME_MAX,
    scope: PyrightScope | None = None,
) -> PrimeSet:
    """対象の言語 ``language`` で、参照元になりうるのに LSP が探さないファイル（``prime_candidates``）。ブロックする。

    * tsserver 系: 名前を含む tsserver 系のファイルすべて（``tsconfig`` の外かどうかは見ない。BUG-0047）。
    * Python: 名前を含む Python のファイルのうち、basedpyright の解析の対象（``scope``。``LspHandle.analysis_scope`` = サーバーが
      読んでいるもの。起動のときと、根の設定の変更を届けたときに引く。``lsp/pyright_scope``）の外のもの（BUG-0132・BUG-0135）。``scope`` が無い（設定が読めない・判断できない）とき・
      利用者の設定が ``include`` / ``exclude`` を書いていない（既定の除外だけ）ときは開かせない（ファイルの一覧も引かない。今までどおり）。
    * ほかの言語: 空。
    """
    rule = _prime_rule(ws, language, scope)
    if rule is None:
        return PrimeSet()
    languages, outside = rule
    return prime_candidates(
        conn, ws, name=name, exclude=exclude, is_open=is_open, limit=limit, languages=languages, outside=outside
    )


def preopen_set(
    conn: sqlite3.Connection,
    ws: Workspace,
    language: str,
    *,
    is_open: Callable[[RelPath], bool],
    open_count: int,
    max_open: int = LSP_OPEN_DOCS_MAX,
    scope: PyrightScope | None = None,
    listing: Sequence[RelPath] | None = None,
) -> PrimeSet:
    """LSP が ``initialize`` を済ませた後に背景で開かせるファイル（BUG-0134）。ブロックする（スレッドで呼ぶ）。

    ``listing``（索引の走査の列挙）を渡すと、索引の代わりにそれから同じ規則で選ぶ（走査の途中でも選べる。索引が記録するのと
    同じ判定〈拡張子・名前による言語とティア、``is_test``〉）。

    ``prime_set`` がどれかの名前で選びうるファイルの和（同じ規則で、名前では絞らない。tsserver 系は同系のファイルすべて、Python は
    basedpyright の解析の対象の外の Python のファイル、ほかの言語は空）のうち、LSP がまだ開いていないもの。中身は読まない。

    * 開いている ``open_count`` 件と合わせて、開いておける上限 ``max_open``（``LSP_OPEN_DOCS_MAX`` = 200）に収まるなら全部
      （テストが先、次にパス順）。以後の参照検索の ``prime_set`` は開くファイルが無くなり、tsserver が組み直さない。
    * 収まらなければ、1 回の問い合わせが開かせる分（``PRIME_MAX``）の余地を残した ``max_open - PRIME_MAX - open_count`` 件を、
      索引の関係（呼び出し・参照・import）を多く持つファイルから選ぶ（多くの名前を含むので、どの対象の ``prime_set`` にも入りやすい）。
      残りは ``skipped`` に数える（問い合わせのたびに ``prime_set`` が今までどおり開かせる）。
    """
    rule = _prime_rule(ws, language, scope)
    if rule is None:
        return PrimeSet()
    languages, outside = rule
    files = _listed(listing) if listing is not None else None
    infos = [
        f
        for f in _prime_pool(conn, ws, exclude="", languages=languages, outside=outside, files=files)
        if not is_open(RelPath(str(f.path)))
    ]
    if len(infos) > max_open - open_count:
        keep = max(0, max_open - PRIME_MAX - open_count)
        weight = queries.relation_counts_by_source(conn, ws.workspace_id)
        infos.sort(key=lambda f: -weight.get(str(f.path), 0))  # 安定ソート: 同数はテストが先、次にパス順のまま
        chosen, skipped = infos[:keep], len(infos) - keep
    else:
        chosen, skipped = infos, 0
    return PrimeSet(tuple((RelPath(str(f.path)), _document_language_id(f.language)) for f in chosen), skipped)


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

    LSP が起動後にどれかの文書の診断をすでに出していれば（``DiagnosticsHub.analysed``）、開かず待たずに ``True``（BUG-0058）。
    起動直後の部分的な答えは LSP 全体の状態（ファイルの一覧を持って解析を 1 周する前）で、文書ごとの状態ではない（T-07 の
    ``_settle_path`` と同じ理由）。以前は対象の文書ごとに開いて最初の診断を待っていたので、冷えた対象の問い合わせのたびに
    その文書の型検査（と、診断の無い文書では空の最初の診断を信用するまでの 300 ms）を待っていた。要求はその文書を開いて送る。
    """
    if getattr(handle.diagnostics, "analysed", False) is True:
        return True
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


_SITES_MAX = 1024


class _SiteCache:
    """``(ワークスペース, ファイル, 内容の sha256)`` → そのファイルの ``(行, 列)`` → ``SiteInfo``。LRU。スレッド安全（BUG-0058）。

    参照箇所の分類は内容だけで決まる（``extract_relations`` は構文木と、同じ内容から取ったシンボルしか見ない）ので、内容の
    sha256 が同じなら問い合わせをまたいで使い回せる。以前は問い合わせごとに参照元のファイルを構文木にかけ直していて、冷えた
    T-06 の所要の大半（参照元が数ファイルで 100〜200 ms）がこの分類だった。
    """

    def __init__(self) -> None:
        self._data: OrderedDict[tuple[str, str, str], dict[tuple[int, int], SiteInfo]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str, str]) -> dict[tuple[int, int], SiteInfo] | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: tuple[str, str, str], value: dict[tuple[int, int], SiteInfo]) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > _SITES_MAX:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


SITES = _SiteCache()


def _read_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@dataclass(slots=True)
class SiteIndex:
    """ファイルごとに ``(行, 列)``（0 始まり）→ ``SiteInfo``。ファイルは 1 度だけ構文木にかける（並行に聞かれても 1 度。BUG-0091）。

    構文木にかけた結果は内容の sha256 ごとにプロセスの中で覚え（``SITES``）、問い合わせをまたいで使い回す（BUG-0058）。
    """

    services: Any
    ws: Workspace
    _cache: dict[str, dict[tuple[int, int], SiteInfo]] = field(default_factory=dict)
    _loading: dict[str, asyncio.Future[dict[tuple[int, int], SiteInfo]]] = field(default_factory=dict)

    def start(self, rel: str) -> None:
        """``rel`` の分類を始めておく（待たない。後の ``of`` が同じ分類を待つ。LSP の要求の待ちと重ねる。BUG-0134）。"""
        if rel not in self._cache and rel not in self._loading:
            self._loading[rel] = asyncio.ensure_future(self._load(rel))

    async def of(self, rel: str) -> dict[tuple[int, int], SiteInfo]:
        if rel in self._cache:
            return self._cache[rel]
        task = self._loading.get(rel)
        if task is None:
            task = self._loading[rel] = asyncio.ensure_future(self._load(rel))
        # 1 つの待ち手が取り消されても、同じファイルを待つ他のノードの分は続ける
        out = await asyncio.shield(task)
        self._cache[rel] = out
        self._loading.pop(rel, None)
        return out

    async def _load(self, rel: str) -> dict[tuple[int, int], SiteInfo]:
        try:
            sha = await asyncio.to_thread(_read_sha, to_abs(self.ws, RelPath(rel)))
            hit = SITES.get((self.ws.workspace_id, rel, sha)) if sha is not None else None
            if hit is not None:
                return hit
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

            out = await asyncio.to_thread(run)
            SITES.put((self.ws.workspace_id, rel, an.sha256), out)
            return out
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


# --- 予算つきの exact の問い合わせ（T-06 の references。BUG-0091） ----------------------------------------


@dataclass(slots=True)
class ExactBudget:
    """1 問い合わせで LSP に使える時間（``EXACT_BUDGET_S``。作るときの値）。要求の期限は残り、数えるかは残りと要求 1 つの期限で決める。

    T-06 は references の要求 1 つで答えるので、その要求は要求 1 つの期限（``LSP_TIMEOUT_REFERENCES_MS``）ではなく予算の残りまで
    待つ。連続時間切れ（3 回で ``unavailable``）に数えるのは、要求 1 つの期限以上待っても答えなかったときだけ（本当の無応答）。
    予算の内に遅れて答えた要求は ``record_ok``、残りが期限より短くて切れた要求は数えない（仕様書 §12.3）。
    """

    budget_s: float = field(default_factory=lambda: EXACT_BUDGET_S)
    started: float = field(default_factory=time.monotonic)

    @property
    def deadline(self) -> float:
        return self.started + self.budget_s

    def left_s(self) -> float:
        return max(self.deadline - time.monotonic(), 0.0)

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    def request(self, request_limit_ms: float) -> dict[str, Any]:
        """``features`` の要求に渡す ``timeout_ms``（予算の残り）と ``strike``（残りが要求 1 つの期限以上か）。"""
        left_ms = self.left_s() * 1000
        return {"timeout_ms": left_ms, "strike": left_ms >= request_limit_ms}


def budget_timeout_warning(budget: ExactBudget, exc: BaseException, *, what: str) -> str:
    """予算の内に LSP が答えなかったときの ``accuracy_warning``（日本語）。切れた上限とその値・次にすること（BUG-0091）。"""
    server = getattr(exc, "server", None)
    method = getattr(exc, "method", None) or "要求"
    who = f"LSP（{server}）" if server else "LSP"
    return (
        f"{who}が {method} に、1 問い合わせの予算 {budget.budget_s:g} 秒（この問い合わせで LSP に使える時間の合計）の内に"
        f"答えませんでした（開始から {budget.elapsed_s():.1f} 秒）。{what}を構文木（relations）で解決しました。"
        "型・オーバーロード・再エクスポート越しの参照は区別されていません。LSP は動いています（起動直後で解析が冷えているか、"
        "負荷が高い）。今の要求で解析が進んだので、同じ問い合わせを呼び直すと exact になる見込みです。"
    )


def budget_timeout_note(budget: ExactBudget, exc: BaseException, *, what: str) -> str:
    """同じ事実の ``warnings[]`` の 1 行（英語）。"""
    server = getattr(exc, "server", None) or "the LSP"
    method = getattr(exc, "method", None) or "the request"
    return (
        f"exact {what} unavailable: {server} did not answer {method} within the {budget.budget_s:g} s exact budget for one"
        f" query ({budget.elapsed_s():.1f} s); answered from the syntax index (conf=structural); the LSP is up and warmer"
        " now - call again for exact"
    )


# --- T-07: 呼び出し階層の辺の供給 --------------------------------------------------------------

EXACT_BUDGET_S = 10.0
"""1 回の ``query_call_hierarchy`` で LSP に使う時間の合計。使い切ったら、LSP が答えたノードは ``exact`` のまま、残りのノードの
子は ``structural``（``relations``）で補う（根の子が取れていなければ木ごと ``structural``。BUG-0091）。"""

EXACT_LANES = 1
"""同時に LSP に送る呼び出し階層の要求（prepare → incoming / outgoing の組）の数。basedpyright は要求を 1 本ずつ処理するので、
増やしても LSP の仕事は速くならず、待ち行列の分だけ要求 1 つの期限（``LSP_TIMEOUT_CALL_HIERARCHY_MS``）に近づく（その時間切れは
連続 3 回で LSP を落とす）。重ねるのは LSP を待たない仕事（最初の診断の待ち・呼び出し箇所の構文木）だけにする（BUG-0091 の実測）。"""

_ASK_SLOW_S = 0.5
"""BUG-0058: 1 ノードの LSP 呼び出し（``_ask``）・T-06 の references 1 回がこの秒数を超えたら、各段の内訳をログに落とす
（閾値ガード付きの計時。``log_slow_spans``）。"""


def log_slow_spans(what: str, name: str, spans: dict[str, float], **extra: Any) -> None:
    """``spans``（段 → 秒）の合計が ``_ASK_SLOW_S`` 以上なら、内訳を 1 行の INFO に落とす（event ``lsp_exact_slow``）。"""
    total = sum(spans.values())
    if total < _ASK_SLOW_S:
        return
    _log.info(
        "slow LSP %s: %s total=%.0fms %s%s",
        what,
        name,
        total * 1000,
        " ".join(f"{k}={v * 1000:.0f}ms" for k, v in spans.items()),
        "".join(f" {k}={v}" for k, v in extra.items()),
        extra={"event": "lsp_exact_slow"},
    )


EXACT_BACKGROUND_S = 60.0
"""1 問い合わせの予算で打ち切ったとき、すでに LSP に送っていた要求の答えを裏で待ち続ける上限（秒。根の要求の期限は予算の残り
＋この値。深いノードの要求は要求 1 つの期限のまま）。届いた答えは PaaD に書き戻して目印を置き、同じノードを聞く次の問い合わせは
LSP に聞き直さずその答えを待つ（``_INFLIGHT``。BUG-0123）。

basedpyright は ``$/cancelRequest`` を受けても計算中の要求を止めない（同期の処理の間はメッセージを読まない）。以前は予算で
取り消して遅れて届いた答えを捨てていたので、冷えた LSP で 17〜27 秒かかる根の ``incomingCalls`` は毎回やり直しになり、
呼び直しの要求もその後ろに並んで予算を使い切っていた（Mnemo の能力ベンチ T11・T12）。"""

INFLIGHT_MAX = 8
"""裏で答えを待つノードの上限（プロセス全体）。超えた分は今までどおり取り消して答えを捨てる。待つ時間も要求の期限で区切られる
（根は予算の残り＋``EXACT_BACKGROUND_S``、深いノードは要求 1 つの期限）。LSP が落ちる・起動し直すと要求は切断で終わる。"""

_BACKSTOP_S = 2.0
"""ワーカースレッドがイベントループの答えを待つ上限の上乗せ（ループ側が予算で打ち切るので、通常は使わない歯止め）。"""

_NAMES_SHOWN = 3
"""警告に名前を出す、構文木で補ったノードの数（残りは件数だけ）。"""


class ExactBudgetExceeded(TimeoutError):
    """T-07 の 1 問い合わせの LSP の予算（``EXACT_BUDGET_S``）を使い切った。要求 1 つの期限（``features.LspRequestTimeout``）とは別。"""

    def __init__(self, budget_s: float) -> None:
        super().__init__(f"the LSP call hierarchy budget of {budget_s:g} s was used up")
        self.budget_s = budget_s


class _NotAsked(Exception):
    """この問い合わせが LSP に聞くのをやめた後のノード（聞かずに構文木で補う）。"""


@dataclass(frozen=True, slots=True)
class _Joined:
    """前の問い合わせが LSP に送っていた要求の答え（裏で届いて書き戻した行。``_INFLIGHT``）。"""

    rows: list[queries.ReferenceRow]
    analysed: bool


@dataclass(slots=True)
class _Pending:
    """予算で打ち切った後も答えを待っているノード 1 つ。``done`` は書き戻した行と解析済みの合図（届かなければ ``None``）。"""

    token: str
    done: asyncio.Future[tuple[list[queries.ReferenceRow], bool] | None]


_INFLIGHT: dict[tuple[str, str, str, str], _Pending] = {}
"""``(db, ws, kind, urn)`` → 裏で答えを待っているノード。イベントループからだけ触る（BUG-0123）。"""

_BACKGROUND: set[asyncio.Future[Any]] = set()
"""裏で答えを待つタスク（終わるまで参照を持つ。``asyncio`` はタスクを弱い参照でしか持たない）。"""


@dataclass(frozen=True, slots=True)
class ExactCut:
    """1 問い合わせが LSP に聞くのをやめた理由（最初の 1 つ）。降格の警告の材料（BUG-0091）。

    * ``kind``: ``budget``（1 問い合わせの予算）/ ``request_timeout``（要求 1 つの期限）/ ``down``（LSP が使えなくなった・
      ``ready`` でない。``reason`` に ``E_LSP_UNAVAILABLE`` の ``details.reason`` か LSP の状態）。
    """

    kind: str
    elapsed_s: float
    node: str | None = None
    method: str | None = None
    timeout_ms: float | None = None
    reason: str | None = None


@dataclass(slots=True)
class _Job:
    """LSP に聞くノード 1 つ（ワーカースレッドで用意し、イベントループで聞く）。"""

    key: tuple[str, str]  # (urn, direction)
    sym: Any
    depth: int
    prime: PrimeSet | None
    item: features.CallItem | None = None
    """親の答えにあった、このノードの項目（あれば ``prepareCallHierarchy`` を送らずに使う）。"""
    ask_at: tuple[RelPath, Position] | None = None
    """``prepareCallHierarchy`` を送る位置（``None`` はノード自身の名前の位置）。Python の ``__init__`` / ``__new__`` の incoming は
    クラスの名前の位置（``_constructor_owner``）。"""


_CONSTRUCTORS = frozenset({"__init__", "__new__"})
_CLASS_KINDS = frozenset({Kind.CLASS, Kind.INTERFACE, Kind.STRUCT, Kind.TRAIT, Kind.ENUM})


def _constructor_owner(conn: Any, ws_id: str, sym: Any) -> tuple[RelPath, Position] | None:
    """Python の ``__init__`` / ``__new__`` なら、その持ち主のクラスの名前の位置。それ以外・クラスが見つからなければ ``None``。

    basedpyright の incomingCalls は名前で呼び出しを探すので、``__init__`` の項目に聞くと ``LimitTable()`` のようなコンストラクタの
    呼び出しを 1 つも返さない（明示の ``__init__(...)`` の呼び出ししか無い）。索引には構文木の ``CALLS`` があるので、0 件の答えを
    「LSP が解析中」と取り違えて降格し、``lsp_status: indexing`` と報告していた（BUG-0129）。クラスの項目に聞くと、
    コンストラクタの呼び出し元が返る。
    """
    if getattr(sym, "name", None) not in _CONSTRUCTORS or not str(sym.path).endswith((".py", ".pyi")):
        return None
    owner_q = str(sym.qualified_name).rpartition(".")[0]
    if not owner_q:
        return None
    for cand in queries.symbols_by_qualified_suffix(conn, ws_id, str(sym.path), owner_q):
        if cand.qualified_name == owner_q and cand.kind in _CLASS_KINDS and cand.path is not None:
            return RelPath(str(cand.path)), (cand.selection or cand.range).start
    return None


@dataclass(slots=True)
class _Level:
    """深さ（子の深さ）ごとの、子を求めたノードの数え。"""

    answered: int = 0  # LSP が答えた
    cached: int = 0  # PaaD の exact 行（目印が新しい）で答えた
    filled: int = 0  # LSP に聞けず、構文木（relations）で補った


_SYMBOL_KIND_FILE = 1
_SYMBOL_KIND_MODULE = 2


def _is_module_item(item: features.CallItem) -> bool:
    """呼び元の項目がファイル（モジュール）そのものか（囲みの関数の無い、モジュール直下の呼び出し）。

    tsserver は名前の無い関数（``test('x', async t => { f() })`` のコールバック）の中の呼び出しの呼び元を、ソースファイルの項目
    （``SymbolKind.File``。選択範囲はファイルの先頭）で返し、basedpyright はモジュール直下の呼び出しをモジュールの項目
    （``SymbolKind.Module``・先頭）で返す。ファイルの先頭の位置で索引を引くと、先頭の行の定義を呼び元と取り違える。
    TS の ``namespace`` も ``Module`` だが、選択範囲は名前の位置なので先頭にはならない（``namespace`` のキーワードがある）。
    """
    if item.kind == _SYMBOL_KIND_FILE:
        return True
    start = item.selection_range.start
    return item.kind == _SYMBOL_KIND_MODULE and start.line == 0 and start.col == 0


_FILE_START = Range(Position(0, 0), Position(0, 0))


def _reference_call_edges(
    edges: Sequence[features.CallEdge],
    locs: Sequence[features.RefLocation],
    infos: dict[str, dict[tuple[int, int], SiteInfo]],
) -> list[features.CallEdge]:
    """``references`` の箇所のうち、構文木で呼び出し（``CALLS``）と分かり、``edges`` に無いものの辺（ファイルごとに 1 本）。

    呼び元の項目はファイルの項目の形にする（``_is_module_item``）。``_build`` が箇所ごとに索引の囲み（``SiteInfo.source_urn``。
    T-06 と同じ）に結ぶ。
    """
    seen = {(str(e.from_rel), r.start.line, r.start.col) for e in edges if e.from_rel is not None for r in e.from_ranges}
    extra: dict[RelPath, list[Range]] = {}
    for loc in locs:
        key = (str(loc.rel), loc.range.start.line, loc.range.start.col)
        info = infos.get(key[0], {}).get(key[1:])
        if info is None or info.relation_kind != "CALLS" or key in seen:
            continue
        seen.add(key)
        extra.setdefault(loc.rel, []).append(loc.range)
    return [
        features.CallEdge(
            features.CallItem("", rel, _FILE_START, _FILE_START, kind=_SYMBOL_KIND_FILE), rel, tuple(ranges)
        )
        for rel, ranges in extra.items()
    ]


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


_STOP_REASONS = frozenset({"standby", "unavailable", "disconnected", "restarting"})
"""この ``E_LSP_UNAVAILABLE`` の ``details.reason`` の後は、同じ問い合わせで LSP に聞かない（LSP が使えない・起動し直している）。"""


def _stops_lsp(exc: BaseException) -> bool:
    """この失敗のあと、同じ問い合わせで LSP に新しい要求を送らない（時間切れ・LSP が使えない）。ノード固有の失敗は続ける。"""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, LuciankeError) and str(exc.details.get("reason", "")) in _STOP_REASONS


def _names_text(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_NAMES_SHOWN])
    return shown + (f" ほか {len(names) - _NAMES_SHOWN} 件" if len(names) > _NAMES_SHOWN else "")


def _names_text_en(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_NAMES_SHOWN])
    return shown + (f" and {len(names) - _NAMES_SHOWN} more" if len(names) > _NAMES_SHOWN else "")


class CallEdgeSource:
    """T-07 の ``exact`` の辺（``CALLS``）の供給。ワーカースレッドから同期で呼ぶ（``asyncio.to_thread`` の中）。

    ノード（URN x 向き）ごとに、PaaD の ``exact`` 行（目印が新しいとき）か、LSP の呼び出し階層
    （``prepareCallHierarchy`` -> ``incomingCalls`` / ``outgoingCalls``。イベントループへ ``run_coroutine_threadsafe``）から
    ``references_to`` / ``references_from`` と同じ形の行を返す。LSP から取った分は ``staged`` / ``staged_rows`` に溜め、
    木ができたあと呼び手が 1 回のジョブで書き戻して目印を置く（``flush``）。

    木は深さごとに聞く（``prefetch``。BUG-0091）: LSP への要求は ``EXACT_LANES`` 本ずつ、呼び出し箇所の構文木は要求と重ねる。
    最初の診断を待つのは根のファイルだけ（``_needs_settle``）、深いノードの項目は親の答えのものを使う（``_item_for``。
    ``prepareCallHierarchy`` を省く）。1 問い合わせの予算（``EXACT_BUDGET_S``）を使い切る・要求が
    期限で切れる・LSP が使えなくなると、以降のノードは LSP に聞かず ``relations`` の行で補う（``filled``）。根の子
    （``required``）が取れないときだけ失敗を上げる（呼び手が木ごと ``structural`` に降格する）。補ったノードは警告に書く
    （``cut`` が理由、``degradation_warning`` / ``cut_note`` が文面）。
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
        budget_s: float | None = None,
    ) -> None:
        self.loop, self.services, self.ws, self.db_path, self.conn = loop, services, ws, db_path, conn
        self.route, self.token = route, token
        self.roots = set(roots)
        # 省略時は作るときの ``EXACT_BUDGET_S``（定義時に固定しない。契約テストが真値を取る間だけ延ばせるように。BUG-0080）
        self.budget_s = EXACT_BUDGET_S if budget_s is None else budget_s
        self.started = time.monotonic()
        self.deadline = self.started + self.budget_s
        self.sites = SiteIndex(services, ws)
        self.staged: list[RelationFact] = []
        self.staged_rows: dict[tuple[str, str], list[queries.ReferenceRow]] = {}
        self.skipped = 0
        self.from_lsp = 0
        self.from_cache = 0
        self.wrote = False
        """``flush`` が LSP の答えを PaaD に書き戻せたか（警告の「呼び直すと続きから」の根拠）。"""
        self.cut: ExactCut | None = None
        """LSP に聞くのをやめた最初の理由（無ければ ``None``）。"""
        self.filled: dict[tuple[str, str], tuple[str, str]] = {}
        """構文木で補ったノード ``(urn, direction)`` → ``(修飾名, 理由)``。"""
        self.levels: dict[int, _Level] = {}
        self.warnings: list[str] = []
        """exact 経路で足す警告（LSP に開かせるファイルの上限など）。呼び手は exact で答えたときだけ出す。"""
        self._rows: dict[tuple[str, str], list[queries.ReferenceRow]] = {}
        self._test: dict[str, bool] = {}
        self._known: dict[str, bool] = {}
        self._settle_tasks: dict[str, asyncio.Future[bool]] = {}
        self._memo_ok: dict[tuple[str, str], bool] = {}
        """``(urn, direction)`` → LSP が解析を済ませた後に聞いた答えか（目印を置いてよいか）。"""
        self._analysed: bool | None = None
        """根の子を聞く前に根のファイルの最初の診断が届いたか（LSP の解析が済んだ合図）。根の子を LSP に聞いていなければ ``None``。"""
        self._items: dict[str, features.CallItem] = {}
        """URN → 親の答えにあったその項目（incoming の ``from`` / outgoing の ``to``）。"""
        self._stop = False
        self._sent: set[tuple[str, str]] = set()
        """イベントループ側: LSP に要求を送ったノード（予算で打ち切ったとき、裏で答えを待つのはこれだけ。BUG-0123）。"""
        self.background: list[str] = []
        """予算で打ち切った後も裏で LSP の答えを待っているノードの修飾名（この問い合わせが送った要求か、前の問い合わせの要求）。"""
        self._asking: str | None = None
        """イベントループ側: いま LSP に聞いているノードの修飾名（予算が切れたときの警告に書く）。"""
        """イベントループ側: 新しい要求を送らない（``cut`` が決まった）。"""

    # --- 公開 ---

    def rows(self, urn: str, direction: str, include_tests: bool) -> list[queries.ReferenceRow]:
        key = (urn, direction)
        if key not in self._rows:
            self.prefetch([key], depth=0, required=urn in self.roots)
        rows = self._rows[key]
        return rows if include_tests else [r for r in rows if not r.is_test]

    def filled_in(self, urn: str, direction: str) -> bool:
        """``(urn, direction)`` の子を LSP に聞けず構文木で補ったか。"""
        return (urn, direction) in self.filled

    def prefetch(self, keys: Iterable[tuple[str, str]], *, depth: int, required: bool = False) -> None:
        """``keys`` のノードの子の行を揃える（深さ ``depth`` の子。木の 1 段ぶんを一度に）。

        PaaD の ``exact`` 行（目印が新しい）があればそれ、無ければ LSP に聞く（予算の内で、要求は ``EXACT_LANES`` 本ずつ）。
        LSP に聞けなかったノードは ``relations`` の行で補う。``required``（根の子）が取れなければ失敗を上げる。
        """
        level = self.levels.setdefault(depth, _Level())
        jobs: list[_Job] = []
        for key in dict.fromkeys(keys):
            if key in self._rows:
                continue
            job = self._plan(key, depth, level, required=required)
            if job is not None:
                jobs.append(job)
        if not jobs:
            return
        if self.cut is not None:
            if required:
                raise self._cut_error()
            for job in jobs:
                self._fill(job, level, "not asked: " + self._cut_reason_en())
            return
        results = self._run(jobs)
        if depth <= 1 and self._analysed is None:
            asked = [self._memo_ok[job.key] for job in jobs if job.key in self._memo_ok]
            self._analysed = all(asked) if asked else None
        first: BaseException | None = None
        for job in jobs:
            res = results.get(job.key)
            if res is None:
                res = ExactBudgetExceeded(self.budget_s)
            if isinstance(res, _Joined):  # 前の問い合わせが送った要求の答え（裏で届いて書き戻し済み）
                self.from_lsp += 1
                level.answered += 1
                self._rows[job.key] = res.rows
                continue
            if isinstance(res, tuple):
                urn, direction = job.key
                rows = self._assemble(urn, direction, *res)
                if not rows and urn in self.roots and self._structural_count(urn, direction):
                    # 起動直後の LSP は ready でも解析が済まず 0 件を返すことがある。索引に呼び出しがあるのに 0 件なら信用しない。
                    # 信用しない答えは書き戻さない（同じ問い合わせの他のノードを書き戻すとき「0 件」の目印を置かないように）
                    self.staged_rows.pop((self._kind(direction), urn), None)
                    res = LuciankeError(
                        ErrorCode.E_LSP_UNAVAILABLE,
                        "the LSP found no calls although the index has some; it may not have finished analysing",
                        details={"reason": "indexing"},
                    )
                else:
                    self.from_lsp += 1
                    level.answered += 1
                    self._rows[job.key] = rows
                    continue
            if not isinstance(res, _NotAsked) and not is_lsp_failure(res):
                raise res
            if required:
                first = first or (res if not isinstance(res, _NotAsked) else None)
                continue
            self._fill(job, level, self._why_en(res))
        if required and any(job.key not in self._rows for job in jobs):
            raise first if first is not None else self._cut_error()

    async def flush(self) -> bool:
        """LSP から取った辺を書き戻し、読み戻して一致したノードに目印を置く。書けたら ``True``。

        LSP が答えたノードがすべて 0 件（辺が無い）でも、書き手があれば目印は置く（読み戻した exact 行が 0 件のときだけ一致する。
        BUG-0058。以前は覚えず、呼び出し元の無い関数を聞くたびに LSP に聞き直していた）。
        """
        if not self.staged_rows:
            return False
        if self.staged:
            if not await write_facts(self.services, self.staged, label="lsp:call_hierarchy"):
                return False
        elif writer_of(self.services) is None:
            return False
        self.wrote = True
        confirmed = await asyncio.to_thread(self._confirm_all)
        await persist_marks(
            self.services, self.db_path, self.ws.workspace_id, confirmed, label="lsp:call_hierarchy:marks"
        )
        return True

    # --- 降格の警告（BUG-0091） ---

    def exact_counts(self) -> tuple[int, int]:
        """``(LSP か PaaD の exact 行で子を求めたノード, 子を求めたノード)``。"""
        done = sum(lv.answered + lv.cached for lv in self.levels.values())
        return done, done + sum(lv.filled for lv in self.levels.values())

    def exact_depth(self) -> int:
        """この深さまでは、子を求めたノードがすべて ``exact`` で答えた（補ったノードが無い）。"""
        depth = 0
        for d in sorted(k for k in self.levels if k > 0):
            if self.levels[d].filled:
                break
            depth = d
        return depth

    def degradation_warning(self, *, whole_tree: bool, direction: str) -> str | None:
        """``accuracy_warning``（日本語）。何が切れたか（実際の予算・期限）・どこまで ``exact`` か・次に何をすればよいか。

        ``whole_tree``: 根の子も取れず、木ごと ``structural`` で答えた。補ったノードも ``cut`` も無ければ ``None``。
        """
        if self.cut is None and not self.filled and not whole_tree:
            return None
        side = {"outgoing": "呼び出し先", "both": "呼び出し元・呼び出し先"}.get(direction, "呼び出し元")
        reason = self._cut_reason_ja()
        if whole_tree:
            what = f"根の{side}を LSP から取れなかったため、木全体を構文木（relations）で解決しました。"
        else:
            done, total = self.exact_counts()
            names = [name for name, _ in self.filled.values()]
            what = (
                f"子を求めた {total} ノードのうち {done} ノードは LSP の答え（exact）で、depth {self.exact_depth()} までは"
                f"すべて exact です。残り {len(names)} ノード（{_names_text(names)}）の{side}は構文木（relations）で"
                "補いました（そのノードの子は confidence_level が structural か heuristic。動的ディスパッチと同名メソッドの"
                "区別が落ちます）。"
            )
        return (reason + "。" if reason else "") + what + self._next_step_ja(whole_tree)

    def cut_note(self, *, whole_tree: bool, direction: str) -> str | None:
        """``warnings[]`` の 1 行（英語）。``degradation_warning`` と同じ事実を短く。exact だけで答えたときも補ったノードを示す。"""
        if self.cut is None and not self.filled and not whole_tree:
            return None
        side = {"outgoing": "callees", "both": "callers/callees"}.get(direction, "callers")
        why = self._cut_reason_en()
        if whole_tree:
            head = f"exact call hierarchy unavailable ({why}); the whole tree comes from the syntax index"
        else:
            done, total = self.exact_counts()
            names = [name for name, _ in self.filled.values()]
            head = (
                f"exact LSP answers for {done}/{total} expanded nodes (complete to depth {self.exact_depth()})"
                + (f"; {why}" if why else "")
                + f"; the {side} of {len(names)} node(s) ({_names_text_en(names)}) come from the syntax index"
                " (conf=structural, nodes[].children_confidence_level)"
            )
        return head + self._next_step_en(whole_tree)

    def _server(self) -> str:
        """LSP のサーバー名（``basedpyright`` など）。分からなければ ``LSP``。"""
        handle = self.route.handle
        name = getattr(handle, "server_name", None) if handle is not None else None
        return str(name or "LSP")

    def _lsp(self) -> str:
        """文中の LSP の呼び名（``LSP（basedpyright）``。サーバー名が分からなければ ``LSP``）。"""
        name = self._server()
        return "LSP" if name == "LSP" else f"LSP（{name}）"

    def _cut_reason_ja(self) -> str:
        cut = self.cut
        if cut is None:
            if not self.filled:
                return ""
            why = next(iter(self.filled.values()))[1]
            return f"{self._lsp()}が {len(self.filled)} ノードの呼び出し階層に答えられませんでした（{why}）"
        if cut.kind == "budget":
            return (
                f"{self._lsp()}の呼び出し階層に使う 1 問い合わせの予算 {self.budget_s:g} 秒（この問い合わせで LSP に"
                f"使える時間の合計）を使い切りました（{cut.node or '?'} を聞いている途中）"
            )
        if cut.kind == "request_timeout":
            limit = cut.timeout_ms if cut.timeout_ms is not None else self._request_limit_ms()
            return (
                f"{self._lsp()}が {cut.method or '呼び出し階層'} の要求に、要求 1 つの期限 {limit:.0f} ms 内に"
                f"答えませんでした（{cut.node or '?'}。問い合わせの開始から {cut.elapsed_s:.1f} 秒の時点。"
                f"1 問い合わせの予算は {self.budget_s:g} 秒）"
            )
        return f"{self._lsp()}が使えないため、続きを聞けませんでした（{cut.reason or 'unavailable'}）"

    def _cut_reason_en(self) -> str:
        cut = self.cut
        if cut is None:
            if not self.filled:
                return ""
            why = next(iter(self.filled.values()))[1]
            return f"the LSP could not answer {len(self.filled)} node(s) ({why})"
        if cut.kind == "budget":
            return f"the {self.budget_s:g} s exact budget for one query ran out after {cut.elapsed_s:.1f} s"
        if cut.kind == "request_timeout":
            limit = cut.timeout_ms if cut.timeout_ms is not None else self._request_limit_ms()
            return (
                f"{self._server()} did not answer {cut.method or 'a call hierarchy request'} within the"
                f" {limit:.0f} ms per-request limit ({cut.node or '?'}, {cut.elapsed_s:.1f} s into the query)"
            )
        return f"the LSP is {cut.reason or 'unavailable'}"

    def _next_step_ja(self, whole_tree: bool) -> str:
        cut = self.cut
        if cut is not None and cut.kind == "down":
            if cut.reason in ("indexing", "restarting"):
                return "LSP の起動（再起動）・索引の完了後に呼び直すと exact になります。"
            return "`configure_workspace status` で LSP の状態を確認してください。"
        if cut is None:
            return ""  # ノード固有の失敗だけ（LSP がそのノードの項目を返さない）。呼び直しても変わらない
        saved = "LSP が答えた分は PaaD に書き戻したので、" if self.wrote else ""
        late = self._background_ja()
        if whole_tree:
            return (
                "LSP は動いています（起動直後で解析が冷えているか、負荷が高い）。今の要求で解析が進んだので、"
                f"{late}{saved}同じ問い合わせを呼び直すと exact になる見込みです。"
            )
        return (
            "LSP は動いています（起動直後で解析が冷えているか、負荷が高い）。"
            f"{late}{saved}同じ問い合わせを呼び直すと残りのノードだけを LSP に聞き、exact で答えきれる見込みです。"
        )

    def _background_ja(self) -> str:
        if not self.background:
            return ""
        return (
            f"送った要求のうち {len(self.background)} ノード（{_names_text(self.background)}）は LSP の答えを裏で待ち続け、"
            f"届けば PaaD に保存します（最大 {EXACT_BACKGROUND_S:g} 秒。呼び直すとその答えを待ち、LSP に聞き直しません）。"
        )

    def _next_step_en(self, whole_tree: bool) -> str:
        cut = self.cut
        if cut is not None and cut.kind == "down":
            if cut.reason in ("indexing", "restarting"):
                return "; call again when the LSP is ready (it is starting or indexing)"
            return "; check `configure_workspace status`"
        if cut is None:
            return ""
        saved = " and the answered nodes are saved" if self.wrote else ""
        if self.background:
            saved += (
                f"; the LSP answer for {len(self.background)} node(s) ({_names_text_en(self.background)}) is still"
                f" awaited in the background and will be saved (up to {EXACT_BACKGROUND_S:g} s)"
            )
        if whole_tree:
            return f"; the LSP is up and warmer now{saved} - call again for an exact tree"
        return f"; the LSP is up and warmer now{saved} - call again to finish the rest exact"

    @staticmethod
    def _request_limit_ms() -> float:
        return float(features.LSP_TIMEOUT_CALL_HIERARCHY_MS)  # 実行時の値（試験が延ばすことがある）

    def _cut_error(self) -> BaseException:
        cut = self.cut
        if cut is None or cut.kind == "budget":
            return ExactBudgetExceeded(self.budget_s)
        if cut.kind == "request_timeout":
            return features.LspRequestTimeout(
                cut.method or "callHierarchy", cut.timeout_ms or self._request_limit_ms(), self._server()
            )
        return LuciankeError(
            ErrorCode.E_LSP_UNAVAILABLE,
            f"the LSP became {cut.reason} during the query",
            details={"reason": cut.reason or "unavailable"},
        )

    def _why_en(self, exc: BaseException) -> str:
        if isinstance(exc, _NotAsked):
            return "not asked: " + self._cut_reason_en()
        if isinstance(exc, ExactBudgetExceeded):
            return "the exact budget ran out"
        if isinstance(exc, TimeoutError):
            return "the request timed out"
        return str(exc)

    # --- 内部（スレッド） ---

    @staticmethod
    def _kind(direction: str) -> str:
        return "calls_in" if direction == "incoming" else "calls_out"

    @staticmethod
    def _loader(urn: str, direction: str) -> Callable[[Any], Sequence[queries.ReferenceRow]]:
        if direction == "incoming":
            return lambda c: queries.references_to(c, urn, ("CALLS",))
        return lambda c: queries.references_from(c, urn, ("CALLS",))

    def _is_test(self, path: str, conn: Any = None) -> bool:
        if path not in self._test:
            self._file_of(path, conn)
        return self._test[path]

    def _is_known(self, path: str, conn: Any = None) -> bool:
        """``path`` が索引にあるファイルか（呼び元をファイルの URN に結んでよいか）。"""
        if path not in self._known:
            self._file_of(path, conn)
        return self._known[path]

    def _file_of(self, path: str, conn: Any) -> None:
        info = queries.file_by_path(self.conn if conn is None else conn, self.ws.workspace_id, path)
        self._known[path] = info is not None
        self._test[path] = bool(info and info.is_test)

    def _plan(self, key: tuple[str, str], depth: int, level: _Level, *, required: bool) -> _Job | None:
        """PaaD の exact 行・シンボルの無いノードはここで行を入れて ``None``。LSP に聞くノードは ``_Job``。"""
        urn, direction = key
        cached = cached_rows(
            self.conn,
            self.db_path,
            self.ws.workspace_id,
            self._kind(direction),
            urn,
            self._loader(urn, direction),
            token=self.token,
        )
        if cached is not None:
            self._rows[key] = cached
            self.from_cache += 1
            level.cached += 1
            return None
        sym = queries.symbol_by_urn(self.conn, urn)
        if sym is None or sym.path is None:
            self._rows[key] = []  # モジュール直下（ファイルの URN）: 呼ばれることはなく、LSP にも項目が無い
            return None
        if self.route.handle is None:
            reason = _down_reason(self.route.choice.lsp_status)
            if required:
                raise LuciankeError(
                    ErrorCode.E_LSP_UNAVAILABLE,
                    "the LSP is not ready and the index has no fresh exact calls for this node",
                    details={"reason": reason},
                )
            if self.cut is None:
                self.cut = ExactCut("down", time.monotonic() - self.started, reason=reason)
        prime = self._prime_set(sym) if direction == "incoming" else None
        ask_at = _constructor_owner(self.conn, self.ws.workspace_id, sym) if direction == "incoming" else None
        item = None if ask_at is not None else self._item_for(urn, sym)
        return _Job(key, sym, depth, prime, item, ask_at)

    def _item_for(self, urn: str, sym: Any) -> features.CallItem | None:
        """親の答えにあった ``urn`` の項目。索引のシンボルと同じファイル・同じ名前の位置のときだけ使う（違えば聞き直す）。"""
        item = self._items.get(urn)
        start = (sym.selection or sym.range).start
        if item is None or item.raw is None or item.rel != sym.path or item.selection_range.start != start:
            return None
        return item

    def _fill(self, job: _Job, level: _Level, why: str) -> None:
        """LSP に聞けなかったノードの子を ``relations`` の行（``structural`` ほか、索引にある水準のまま）で補う。"""
        urn, direction = job.key
        self._rows[job.key] = list(self._loader(urn, direction)(self.conn))
        self.filled[job.key] = (str(job.sym.qualified_name), why)
        level.filled += 1

    def _run(self, jobs: list[_Job]) -> dict[tuple[str, str], Any]:
        """``jobs`` をイベントループで聞き、ノードごとの ``(edges, infos)`` か例外を返す。予算を使い切った分は入らない。"""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            if self.cut is None:
                self.cut = ExactCut(
                    "budget", time.monotonic() - self.started, node=str(jobs[0].sym.qualified_name)
                )
            return {}
        fut = asyncio.run_coroutine_threadsafe(self._ask_all(jobs), self.loop)
        try:
            return fut.result(timeout=remaining + _BACKSTOP_S)
        except TimeoutError:
            fut.cancel()
            if self.cut is None:
                self.cut = ExactCut("budget", time.monotonic() - self.started)
            return {}

    def _prime_set(self, sym: Any) -> PrimeSet | None:
        """incoming の呼び元になりうるファイル（対象の名前を含み、LSP がまだ開いていないもの。``prime_set``）。ワーカースレッドで引く。

        tsserver は ``tsconfig.json`` の ``include`` に入らないファイル（``test/`` など）を、basedpyright は解析の対象（``include`` /
        ``exclude``）の外のファイルを、開かれるまで ``callHierarchy`` の呼び元に出さない（T-06 の BUG-0047・BUG-0132 と同じ）。
        """
        handle = self.route.handle
        if handle is None or not needs_priming(self.route.file.language):
            return None
        return prime_set(
            self.conn,
            self.ws,
            self.route.file.language,
            name=sym.name,
            exclude=str(sym.path),
            is_open=handle.documents.is_open,
            limit=PRIME_MAX,
            scope=getattr(handle, "priming_scope", None),
        )

    def _structural_count(self, urn: str, direction: str, conn: Any = None) -> int:
        return len(self._loader(urn, direction)(self.conn if conn is None else conn))

    def _urn_at(self, rel: Any, pos: Position, conn: Any = None) -> str | None:
        if rel is None:
            return None
        sym = queries.symbol_at(
            self.conn if conn is None else conn, self.ws.workspace_id, str(rel), pos.line, pos.col
        )
        return None if sym is None else str(sym.urn)

    def _assemble(
        self,
        urn: str,
        direction: str,
        edges: Sequence[features.CallEdge],
        infos: dict[str, dict[tuple[int, int], SiteInfo]],
    ) -> list[queries.ReferenceRow]:
        facts, rows = self._build(self.conn, urn, direction, edges, infos, items=self._items)
        self.staged.extend(facts)
        self.staged_rows[(self._kind(direction), urn)] = rows
        return rows

    def _build(
        self,
        conn: Any,
        urn: str,
        direction: str,
        edges: Sequence[features.CallEdge],
        infos: dict[str, dict[tuple[int, int], SiteInfo]],
        *,
        items: dict[str, features.CallItem] | None = None,
    ) -> tuple[list[RelationFact], list[queries.ReferenceRow]]:
        """LSP の辺 → ``exact`` の事実と、``references_to`` / ``references_from`` と同じ形の行（``conn`` で索引を引く）。"""
        incoming = direction == "incoming"
        facts: list[RelationFact] = []
        rows: list[queries.ReferenceRow] = []
        for edge in edges:
            home = edge.from_rel  # 呼び出し箇所のファイル（incoming は呼び元、outgoing は問い合わせた項目）
            if home is None or not edge.from_ranges:
                continue
            other = edge.item
            by_site = False  # 呼び元を呼び出し箇所ごとに索引の囲みで決める（モジュール直下の呼び出し。BUG-0133）
            if not incoming and other.rel is None:
                other_urn: str | None = _external_urn(other)
            elif incoming and _is_module_item(other):
                other_urn = None
            else:
                other_urn = self._urn_at(other.rel, other.selection_range.start, conn)
            if other_urn is None and incoming and other.rel is not None and self._is_known(str(home), conn):
                by_site = True
            elif other_urn is None:
                self.skipped += 1
                continue
            elif other.rel is not None and items is not None:
                items.setdefault(other_urn, other)  # 子のノードを聞くときに prepare を省く
            for rng in edge.from_ranges:
                info = infos.get(str(home), {}).get((rng.start.line, rng.start.col))
                if by_site:
                    # 呼び元の項目が索引のシンボルに当たらない（ファイルそのもの・索引に無い入れ子）: 構文木の索引・T-06 と同じく
                    # その箇所の囲みの定義、無ければファイルの URN（T-07 の ``(module)`` のノード）に結ぶ
                    other_urn = info.source_urn if info is not None else str(Urn(self.ws.workspace_id, str(home)))
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
                rows.append(row_of(fact, str(home), is_test=self._is_test(str(home), conn)))
        return facts, rows

    def _confirm_all(self) -> list[tuple[str, str]]:
        """読み戻して一致したノードに目印を置き、置いた ``(kind, urn)`` を返す。最初の診断が出ていないファイルのノードは置かない。"""
        from lucianke.index.connection import open_reader  # 読み取り接続はこのスレッドで開く

        conn = open_reader(self.db_path)
        confirmed: list[tuple[str, str]] = []
        try:
            for (kind, urn), rows in self.staged_rows.items():
                direction = "incoming" if kind == "calls_in" else "outgoing"
                if not self._memo_ok.get((urn, direction), False):
                    continue  # 解析前の部分的な答えかもしれない: exact で答えるが覚えない
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

    async def _ask_all(self, jobs: list[_Job]) -> dict[tuple[str, str], Any]:
        """``jobs`` を聞く。根のファイルの最初の診断の待ちを先に始め、要求は ``EXACT_LANES`` 本ずつ。予算を使い切ったら残りを取り消す。"""
        for job in jobs:
            if self._needs_settle(job):
                self._settle_task(self._settle_path(job))  # 根のファイルを開いて、最初の診断の待ちを始める
        lane = asyncio.Semaphore(max(1, EXACT_LANES))
        out: dict[tuple[str, str], Any] = {}

        async def run(job: _Job) -> Any:
            try:
                res: Any = await self._ask(job, lane)
            except _NotAsked as exc:
                res = exc
            except LSP_FAILURES as exc:
                res = exc
                if _stops_lsp(exc) and is_lsp_failure(exc):
                    self._stop = True
                    if self.cut is None:
                        self.cut = self._cut_of(exc, job)
            out[job.key] = res
            return res

        tasks = {asyncio.ensure_future(run(job)): job for job in jobs}
        done, pending = await asyncio.wait(tasks, timeout=max(self.deadline - time.monotonic(), 0.0))
        if pending:
            self._stop = True
            if self.cut is None:
                first = next(job for task, job in tasks.items() if task in pending)
                node = self._asking or str(first.sym.qualified_name)
                self.cut = ExactCut("budget", time.monotonic() - self.started, node=node)
            # LSP に送った要求は取り消さず、答えを裏で待って書き戻す（basedpyright は取り消しても計算を止めない。BUG-0123）
            sent = [
                task for task in pending if tasks[task].key in self._sent and self._detach(tasks[task], task)
            ]
            dropped = [task for task in pending if task not in sent]
            for task in dropped:
                task.cancel()
            await asyncio.gather(*dropped, return_exceptions=True)
            for task in pending:
                out[tasks[task].key] = ExactBudgetExceeded(self.budget_s)
        if self._stop:  # 以降は LSP に聞かない: 残っている最初の診断の待ちも止める
            for settle in self._settle_tasks.values():
                settle.cancel()
        for task in done:
            task.result()  # 握らなかった例外（降格の合図でない誤り）はそのまま上げる
        return dict(out)  # 裏で答えを待つタスクは ``out`` に書き続けるので、写しを返す

    def _inflight_key(self, key: tuple[str, str]) -> tuple[str, str, str, str]:
        urn, direction = key
        return (str(self.db_path), self.ws.workspace_id, self._kind(direction), urn)

    def _detach(self, job: _Job, task: asyncio.Future[Any]) -> bool:
        """予算で打ち切ったノードの要求を裏で待ち続ける（``_INFLIGHT`` に登録）。上限を超えるときは ``False``（取り消す）。"""
        ikey = self._inflight_key(job.key)
        for stale in [k for k, p in _INFLIGHT.items() if p.done.get_loop().is_closed()]:
            del _INFLIGHT[stale]  # 閉じたイベントループの残り（試験の後など）は数えない
        if ikey in _INFLIGHT or len(_INFLIGHT) >= INFLIGHT_MAX:
            return False
        pending = _Pending(self.token, asyncio.get_running_loop().create_future())
        _INFLIGHT[ikey] = pending
        self.background.append(str(job.sym.qualified_name))
        late = asyncio.ensure_future(self._late(job, task, pending, ikey))
        _BACKGROUND.add(late)
        late.add_done_callback(_BACKGROUND.discard)
        return True

    async def _late(
        self, job: _Job, task: asyncio.Future[Any], pending: _Pending, ikey: tuple[str, str, str, str]
    ) -> None:
        """裏で待った答えを書き戻し、目印を置く（``flush`` と同じ規則）。待っていた次の問い合わせには行を渡す。"""
        result: tuple[list[queries.ReferenceRow], bool] | None = None
        try:
            res = await task
            if isinstance(res, tuple):
                analysed = bool(self._memo_ok.get(job.key, False))
                rows = await self._save_late(job, res[0], res[1], analysed)
                if rows is not None:
                    result = (rows, analysed)
                    _log.info(
                        "late LSP call hierarchy answer saved: %s %s (%d calls)",
                        job.sym.qualified_name,
                        job.key[1],
                        len(rows),
                        extra={"event": "lsp_call_hierarchy_late"},
                    )
        except Exception:  # noqa: BLE001  裏の書き戻しの失敗は答えを止めない（次の問い合わせがまた LSP に聞く）
            _log.debug("could not save a late call hierarchy answer", exc_info=True)
        finally:
            if not pending.done.done():
                pending.done.set_result(result)
            if _INFLIGHT.get(ikey) is pending:
                del _INFLIGHT[ikey]

    async def _save_late(
        self,
        job: _Job,
        edges: Sequence[features.CallEdge],
        infos: dict[str, dict[tuple[int, int], SiteInfo]],
        analysed: bool,
    ) -> list[queries.ReferenceRow] | None:
        """裏で届いた 1 ノードの答えを ``relations`` に書き、読み戻して一致すれば目印を置く。信用しない答えは ``None``。"""
        from lucianke.index.connection import open_reader  # このスレッドで開く

        urn, direction = job.key
        kind = self._kind(direction)

        def build() -> tuple[list[RelationFact], list[queries.ReferenceRow]] | None:
            conn = open_reader(self.db_path)
            try:
                facts, rows = self._build(conn, urn, direction, edges, infos)
                if not rows and urn in self.roots and self._structural_count(urn, direction, conn):
                    return None  # 解析前の 0 件かもしれない（prefetch と同じ規則）
                return facts, rows
            finally:
                conn.close()

        built = await asyncio.to_thread(build)
        if built is None:
            return None
        facts, rows = built
        if facts and not await write_facts(self.services, facts, label="lsp:call_hierarchy:late"):
            return rows
        if not analysed:
            return rows  # 解析前の部分的な答えかもしれない: exact で答えるが覚えない

        def confirm() -> bool:
            conn = open_reader(self.db_path)
            try:
                loader = self._loader(urn, direction)
                return confirm_marker(
                    conn, self.db_path, self.ws.workspace_id, kind, urn, loader, rows, token=self.token
                )
            finally:
                conn.close()

        if await asyncio.to_thread(confirm):
            await persist_marks(
                self.services,
                self.db_path,
                self.ws.workspace_id,
                [(kind, urn)],
                label="lsp:call_hierarchy:late",
            )
        return rows

    async def _join(self, job: _Job) -> _Joined | None:
        """前の問い合わせが送ったこのノードの要求が裏で待たれていれば、その答えを待つ（同じファイルの状態のときだけ）。"""
        pending = _INFLIGHT.get(self._inflight_key(job.key))
        if pending is None or pending.token != self.token:
            return None
        if pending.done.get_loop() is not asyncio.get_running_loop():
            return None
        name = str(job.sym.qualified_name)
        self.background.append(name)  # 予算の内に届かなければ、警告に「裏で待っている」と書く
        result = await asyncio.shield(pending.done)
        self.background.remove(name)
        if result is None:
            return None
        rows, analysed = result
        self._memo_ok[job.key] = analysed
        return _Joined(rows, analysed)

    def _cut_of(self, exc: BaseException, job: _Job) -> ExactCut:
        elapsed = time.monotonic() - self.started
        node = str(job.sym.qualified_name)
        if isinstance(exc, ExactBudgetExceeded) or (isinstance(exc, TimeoutError) and job.depth <= 1):
            return ExactCut("budget", elapsed, node=node)  # 根の要求の期限は予算の残り（_root_limits）
        if isinstance(exc, TimeoutError):
            method = getattr(exc, "method", None)
            limit = getattr(exc, "timeout_ms", None)
            return ExactCut(
                "request_timeout",
                elapsed,
                node=node,
                method=method if isinstance(method, str) else None,
                timeout_ms=float(limit) if isinstance(limit, (int, float)) else None,
            )
        reason = exc.details.get("reason") if isinstance(exc, LuciankeError) else None
        return ExactCut("down", elapsed, node=node, reason=str(reason or "unavailable"))

    def _settle_task(self, rel: RelPath) -> asyncio.Future[bool]:
        """``rel`` の最初の診断の待ち（ファイルごとに 1 つ。同じ問い合わせのノードで共有する）。"""
        key = str(rel)
        task = self._settle_tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(self._first_diagnostic(rel))
            # 待ち手が取り消されても、待ちの例外は拾っておく（「取り出されなかった例外」のログを出さない）
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._settle_tasks[key] = task
        return task

    async def _first_diagnostic(self, rel: RelPath) -> bool:
        handle = self.route.handle
        assert handle is not None
        if not needs_first_diagnostic(self.route.file.language):
            return True
        left = max(self.deadline - time.monotonic(), 0.0)  # 待ちは EXACT_BUDGET_S の残りの内
        return await await_first_diagnostic(handle, rel, timeout_s=left)

    async def _settle(self, rel: RelPath) -> bool:
        """``rel`` の最初の診断を待つ（共有の待ち。取り消されても待ちそのものは他のノードのために残す）。"""
        return await asyncio.shield(self._settle_task(rel))

    def _root_limits(self, job: _Job) -> dict[str, Any]:
        """根の要求（根の子を求める prepare と incoming / outgoing）の期限と、時間切れを連続時間切れに数えるか（BUG-0091）。

        根の要求は要求 1 つの期限（``LSP_TIMEOUT_CALL_HIERARCHY_MS``）ではなく、1 問い合わせの予算の残りまで待つ（冷えた LSP の
        最初の要求は 5 秒を超えることがあり、そこで切ると予算を残したまま木ごと降格していた）。予算で打ち切った後も
        ``EXACT_BACKGROUND_S`` まで裏で答えを待つ（BUG-0123）ので、期限は予算の残り＋その値。時間切れを数えるのは、要求 1 つの
        期限以上待っても答えなかったときだけ（本当の無応答）。遅れて答えた要求は ``record_ok``（数えない）。深いノードの要求は
        今までどおり（省略 = 要求 1 つの期限・数える）。
        """
        if job.depth > 1:
            return {}
        limit_ms = (max(self.deadline - time.monotonic(), 0.0) + EXACT_BACKGROUND_S) * 1000
        return {"timeout_ms": limit_ms, "strike": limit_ms >= self._request_limit_ms()}

    def _needs_settle(self, job: _Job) -> bool:
        """このノードを聞く前に、根のファイルの最初の診断を待つか（待つファイルは ``_settle_path``）。

        待つのは根の子を聞くとき（根のファイル）だけ。最初の診断は「LSP が解析を済ませた」合図で、起動直後の部分的な答えを
        目印にしないためのもの（BUG-0012・20e）。LSP 全体の状態なので、根のファイルで 1 度確かめれば足りる（T-06 も対象の
        ファイルだけを待つ）。深いノードのファイルごとに待つと、LSP がそのファイルを検査するあいだ要求が遅れ、冷えた LSP では
        1 問い合わせの予算の 3 割ほどを使っていた（BUG-0091 の実測）。根の子が PaaD から来てまだ確かめていない
        （``_analysed`` が ``None``）ときも、根のファイルで 1 度だけ確かめる（BUG-0123）。
        """
        return job.depth <= 1 or self._analysed is None

    def _settle_path(self, job: _Job) -> RelPath:
        """最初の診断を待つファイル。根の子を聞くときはそのノード（根）のファイル、それ以外は問い合わせの根のファイル。

        以前は根の子が PaaD から来たとき（呼び直し）、深いノードのファイルごとに ``didOpen`` して最初の診断を待っていた。
        1 ノード 0.7〜1.6 秒の待ちに加え、basedpyright がそのファイルを検査するあいだ他の要求も遅れた（BUG-0123 の親の計測の
        T12 の 2 回目）。合図は LSP 全体の状態なので、根のファイル（前の問い合わせで開いて診断を受けていれば待たない）で足りる。
        """
        if job.depth <= 1:
            return job.sym.path
        return self.route.file.path

    async def _ask(
        self, job: _Job, lane: asyncio.Semaphore
    ) -> tuple[list[features.CallEdge], dict[str, dict[tuple[int, int], SiteInfo]]] | _Joined:
        handle = self.route.handle
        if handle is None or self._stop:
            raise _NotAsked
        # 前の問い合わせの要求の答えを待つ（同じ要求を LSP に送り直さない。BUG-0123）
        joined = await self._join(job)
        if joined is not None:
            return joined
        if self._stop:
            raise _NotAsked
        sym, direction, prime = job.sym, job.key[1], job.prime
        # tsserver 系の incoming は references でプロジェクトをまたぐ呼び出しも取る（BUG-0133。basedpyright の callHierarchy は
        # 解析の対象と開いた文書をすべて探すので要らない）
        by_refs = direction == "incoming" and self.route.file.language in TS_FAMILY
        # BUG-0058: LSP 経路の各段の所要（秒）。合計が閾値を超えた呼び出しだけ内訳をログに落とす（閾値ガード付き）。
        spans: dict[str, float] = {}
        t = time.monotonic()
        if prime is not None:
            await prime_documents(handle, prime)
            note = prime.warning(sym.name)
            if note and note not in self.warnings:
                self.warnings.append(note)
        spans["prime"], t = time.monotonic() - t, time.monotonic()
        if self._needs_settle(job):
            self._memo_ok[job.key] = await self._settle(self._settle_path(job))
        else:
            self._memo_ok[job.key] = bool(self._analysed)
        spans["settle"], t = time.monotonic() - t, time.monotonic()
        async with lane:
            if self._stop:
                raise _NotAsked
            spans["queue"], t = time.monotonic() - t, time.monotonic()
            self._asking = str(sym.qualified_name)
            self._sent.add(job.key)
            item = job.item
            if item is None:
                path, start = job.ask_at or (sym.path, (sym.selection or sym.range).start)
                items = await features.prepare_call_hierarchy(handle, path, start, **self._root_limits(job))
                item = next(
                    (i for i in items if i.rel == path and i.selection_range.start == start),
                    next((i for i in items if i.rel == path), None),
                )
            spans["prepare"], t = time.monotonic() - t, time.monotonic()
            if item is None and not by_refs:
                raise LuciankeError(
                    ErrorCode.E_LSP_UNAVAILABLE,
                    f"the LSP has no call hierarchy item for {sym.qualified_name}",
                    details={"reason": "lsp_error"},
                )
            edges: list[features.CallEdge] = []
            if item is not None:
                call = features.incoming_calls if direction == "incoming" else features.outgoing_calls
                edges = await call(handle, item, **self._root_limits(job))
            spans["calls"], t = time.monotonic() - t, time.monotonic()
            locs: list[features.RefLocation] = []
            if by_refs:
                # tsserver の callHierarchy は対象のファイルの既定のプロジェクト（tsconfig の include）の中しか探さない
                # （``provideCallHierarchyIncomingCalls`` は ``getFileAndProject`` の 1 つのプロジェクト）。開いた test/ などの別の
                # プロジェクトの呼び出しは references（プロジェクトをまたぐ）で集め、構文木で呼び出しと分かる箇所を足す（BUG-0133）。
                # 呼び出し階層の項目が無いノード（関数でない値を持つ変数など）も、references の呼び出し箇所だけで答える
                pos = (sym.selection or sym.range).start
                locs = await features.references(
                    handle, sym.path, pos, include_declaration=True, **self._root_limits(job)
                )
                spans["references"], t = time.monotonic() - t, time.monotonic()
        rels = sorted({str(e.from_rel) for e in edges if e.from_rel is not None} | {str(loc.rel) for loc in locs})
        # 呼び出し箇所の構文木はスレッドで、ファイルごとに並行（LSP の要求の枠の外。次のノードの要求と重なる）
        loaded = await asyncio.gather(*(self.sites.of(rel) for rel in rels))
        infos = dict(zip(rels, loaded, strict=True))
        if locs:
            edges = [*edges, *_reference_call_edges(edges, locs, infos)]
        spans["sites"] = time.monotonic() - t
        log_slow_spans(f"call hierarchy {direction}", str(sym.qualified_name), spans, depth=job.depth)
        return edges, infos
