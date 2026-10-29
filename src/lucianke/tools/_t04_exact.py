"""T-04 ``resolve_symbols`` の ``exact`` 経路（仕様書 §11 T-04 処理規約 2、設計書 §8.3・ADR-6・R-11）。

構文木と索引で解決したシンボル 1 件を、LSP の ``definition`` と ``hover`` で ``exact`` に上げる。判断は 3 つだけ。

1. **キャッシュが先**: ``_hover_cache(urn, sha256)`` に行があれば、LSP を呼ばずに（起動も促さずに）``exact`` で答える
   （R-11。LSP が止まっていても、別のセッションでも、定義ファイルの内容が同じなら ``exact``）。
2. **キャッシュに無ければ LSP**: LSP が ``ready`` のときだけ、名前の位置（``selection.start``。文字境界）に ``definition`` と
   ``hover`` を送る。``ready`` でなければ **待たずに** ``structural``（``standby`` なら ``ensure_started`` で起動を促すだけ。
   ``select`` の判定表どおり）。待つのは ``lsp.features`` の要求ごとの上限（``LSP_TIMEOUT_*``）だけ。
3. **失敗は降格**: ``TimeoutError`` と ``E_LSP_UNAVAILABLE``（``position_clamped`` を含む）は ``selector.on_lsp_failure`` で
   ``structural`` に降ろす（その項目だけ。タイムアウト等はその呼び出しの残りの項目も LSP を呼ばない）。
   それ以外の例外は握らず上へ（項目単位の失敗として扱われる）。

``definition`` は **確認** に使う: 宣言の名前の位置から引いた定義が同じ場所でなければ（別名・再エクスポート・オーバーロード）、
項目の ``warnings`` に LSP の答えを書く（解決先の付け替えはしない。構文木の解決を変えると ``definition.range`` と T-05 の
``range_returned`` の一致〈事後条件 1〉がオーバーロードなどで崩れうるため）。
``hover`` の型文字列は ``signature_expanded`` にそのまま入れる（事後条件: 同じ位置の hover と一致）。hover が空なら ``structural``。

``ctx.services`` に求めるもの（すべて ``getattr`` の duck typing。無ければ無いなりに動く）:

* ``lsp_pool``: ``LspPool``（``status(language, ws)``・``ensure_started(language, ws)``・``get(language, ws, start_if_needed=False)``）。
  無ければ LSP は ``unavailable`` として ``structural``。
* 書き込み手: ``runtime(workspace_id).writer``（``WiredServices``。``IndexWriter.asubmit``）、無ければ ``writer`` 属性。
  無ければ ``_hover_cache`` に書かない（2 回目も LSP に聞く）。書き込みは ``data_version`` を進めない（内部テーブル）。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from lucianke.core.errors import LuciankeError
from lucianke.core.model import Capability, Confidence, EngineChoice, FileInfo, LspStatus, SymbolRecord
from lucianke.engine import selector
from lucianke.lsp import features
from lucianke.workspace.types import Workspace

_log = logging.getLogger("lucianke.tools.t04")

CAP = Capability.DEFINITION

NO_HOVER_WARNING = "LSP が型情報を返さなかったため、宣言どおりのシグネチャを返しました。"


@dataclass(frozen=True, slots=True)
class Lift:
    """1 件を ``exact`` に上げる試みの結果。

    * ``text``: hover の型文字列（``exact`` のとき。``None`` なら ``structural`` のまま）。
    * ``choice``: この項目のエンジン選択（応答の代表を決めるのに使う）。
    * ``from_cache``: ``_hover_cache`` から答えた。
    * ``warnings``: 項目の ``warnings`` に足す文。
    """

    text: str | None
    choice: EngineChoice
    from_cache: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def exact(self) -> bool:
        return self.text is not None


@dataclass(slots=True)
class _Lang:
    """言語ごとの、この呼び出しでの LSP の使い方。``handle`` が ``None`` なら LSP を呼ばない。"""

    choice: EngineChoice
    handle: Any | None


# --- _hover_cache ---------------------------------------------------------------------------


def cached_hover(conn: sqlite3.Connection, urn: str, sha256: str) -> str | None:
    """``_hover_cache`` の行（定義ファイルの内容が同じとき）。無ければ ``None``。"""
    row = conn.execute(
        "SELECT hover_text FROM _hover_cache WHERE urn = ? AND sha256 = ?", (urn, sha256)
    ).fetchone()
    return None if row is None else str(row[0])


def _store_job(urn: str, sha256: str, text: str) -> Any:
    now = datetime.now(UTC).isoformat(timespec="seconds")

    def job(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM _hover_cache WHERE urn = ? AND sha256 <> ?", (urn, sha256))  # 古い版の行
        conn.execute(
            "INSERT OR REPLACE INTO _hover_cache(urn, sha256, hover_text, cached_at) VALUES (?, ?, ?, ?)",
            (urn, sha256, text, now),
        )

    return job


def writer_of(services: object, workspace_id: str) -> Any | None:
    """``IndexWriter``（``asubmit`` を持つもの）。``runtime(workspace_id).writer`` → ``writer`` 属性。無ければ ``None``。"""
    runtime = getattr(services, "runtime", None)
    if callable(runtime):
        rt = runtime(workspace_id)
        writer = getattr(rt, "writer", None)
        if writer is not None:
            return writer
    return getattr(services, "writer", None)


# --- 本体 -----------------------------------------------------------------------------------


class ExactLifter:
    """1 回の呼び出しの間だけ生きる。言語ごとの LSP の状態判断を覚え、項目ごとに ``lift`` を返す。"""

    def __init__(self, services: object, ws: Workspace, conn: sqlite3.Connection) -> None:
        self._svc = services
        self._ws = ws
        self._conn = conn
        self._pool: Any | None = getattr(services, "lsp_pool", None)
        self._writer = writer_of(services, ws.workspace_id)
        self._langs: dict[str, _Lang] = {}

    # --- LSP の状態 ---

    def _status(self, language: str) -> tuple[LspStatus, int | None, str | None]:
        if self._pool is None:
            return LspStatus.UNAVAILABLE, None, None
        st = self._pool.status(language, self._ws)
        return LspStatus(st.status), st.progress, st.server_name

    def _select(
        self, info: FileInfo, status: LspStatus, progress: int | None, name: str | None
    ) -> EngineChoice:
        return selector.select(
            info,
            CAP,
            lsp_status=status,
            lsp_progress=progress,
            has_grammar=True,
            has_ctags=False,
            lsp_name=name,
        )

    async def _prepare(self, info: FileInfo) -> _Lang:
        """この言語の LSP を使えるか（待たない）。``standby`` なら起動を促すだけ。"""
        known = self._langs.get(info.language)
        if known is not None:
            return known
        status, progress, name = self._status(info.language)
        choice = self._select(info, status, progress, name)
        if self._pool is not None and status is LspStatus.STANDBY and selector.starts_lsp(CAP):
            await self._pool.ensure_started(info.language, self._ws)  # 待たずに戻る
        handle = None
        if choice.confidence is Confidence.EXACT and self._pool is not None:
            handle = self._pool.get(info.language, self._ws, start_if_needed=False)
            if handle is None:  # 選んだ直後に使えなくなった
                choice = self._select(info, LspStatus.UNAVAILABLE, None, name)
        lang = self._langs[info.language] = _Lang(choice, handle)
        return lang

    def _cache_choice(self, info: FileInfo) -> EngineChoice:
        status, progress, _ = self._status(info.language)
        return EngineChoice(
            engine=selector.ENGINE_LSP_CACHE,
            confidence=Confidence.EXACT,
            lsp_status=status.value,
            lsp_progress=progress if status is LspStatus.INDEXING else None,
        )

    # --- 1 件 ---

    async def lift(self, sym: SymbolRecord, info: FileInfo, sha256: str) -> Lift:
        """``sym`` を ``exact`` に上げる。上げられなければ ``text=None`` と、降格したエンジン選択。"""
        urn = str(sym.urn)
        cached = cached_hover(self._conn, urn, sha256)
        if cached is not None:  # R-11: 2 回目以降は PaaD から。LSP は呼ばず、起動も促さない
            return Lift(cached, self._cache_choice(info), from_cache=True)
        lang = await self._prepare(info)
        choice = lang.choice
        if lang.handle is None or choice.confidence is not Confidence.EXACT or sym.selection is None:
            return Lift(None, choice)
        pos = sym.selection.start
        try:
            defs = await features.definition(lang.handle, sym.path, pos)
            text = await features.hover(lang.handle, sym.path, pos)
        except (TimeoutError, LuciankeError) as exc:
            if not selector.is_lsp_degradation(exc):
                raise
            demoted = selector.on_lsp_failure(choice, CAP, exc)
            if not (isinstance(exc, LuciankeError) and exc.details.get("reason") == "position_clamped"):
                self._langs[info.language] = _Lang(demoted, None)  # この呼び出しの残りは LSP に聞かない
            _log.info("T-04: LSP demoted for %s (%s: %s)", urn, type(exc).__name__, exc)
            return Lift(None, demoted)
        if not text:
            return Lift(None, replace(choice, confidence=Confidence.STRUCTURAL, warning=NO_HOVER_WARNING),
                        warnings=(NO_HOVER_WARNING,))  # fmt: skip
        warnings = _definition_note(sym, defs)
        await self._store(urn, sha256, text)
        return Lift(text, choice, warnings=warnings)

    async def _store(self, urn: str, sha256: str, text: str) -> None:
        """hover を ``_hover_cache`` に書く。書き終えてから戻る（直後の 2 回目の呼び出しが PaaD から答えられるように）。"""
        if self._writer is None:
            return
        try:
            await self._writer.asubmit(
                _store_job(urn, sha256, text), bumps_version=False, label="hover_cache"
            )
        except Exception:  # noqa: BLE001  キャッシュに書けなくても応答は返す
            _log.warning("T-04: could not write _hover_cache for %s", urn, exc_info=True)


def _definition_note(sym: SymbolRecord, defs: list[features.Location]) -> tuple[str, ...]:
    """宣言の名前の位置から引いた定義が、同じ宣言を指していなければ、LSP の答えを 1 文にする（付け替えはしない）。"""
    if not defs or sym.selection is None:
        return ()
    line = sym.selection.start.line
    if any(d.rel == sym.path and d.range.start.line == line for d in defs):
        return ()
    first = defs[0]
    where = (
        f"{first.rel}:{first.range.start.line + 1}"
        if first.rel is not None
        else f"{first.external_uri} (outside the workspace)"
    )
    return (f"LSP resolves this declaration to {where}",)
