"""LSP が ``initialize`` を済ませた後に、参照元になりうるファイルを背景でまとめて開かせる（BUG-0134。設計書 §7.5.6）。

T-06 の参照検索と T-07 の incoming は、聞く前に、LSP が開かれるまで探さないファイル（tsserver 系は ``tsconfig.json`` の外、
basedpyright は解析の対象の外）のうち対象の名前を含むものを開かせる（``_lsp_exact.prime_set``。BUG-0047・BUG-0132）。tsserver は
新しく開いたファイルで推定プロジェクトを組み直し、次の ``references`` で型の検査器を作り直すので、その回だけ 0.6〜1.5 秒かかって
いた（どの対象の呼び出しで起きるかは、それまでに開いたファイルで決まる）。

ここでは ``prime_set`` がどれかの名前で選びうるファイルの和（``_lsp_exact.preopen_set``）を、呼び出しの経路の外で 1 回にまとめて
開かせる。以後の ``prime_set`` は開くファイルが無くなるので、組み直しは背景のこの 1 回だけになる（BUG-0127 で型サーバーを
走査と並べて先に起こしたのと同じ考え: どの呼び出しにも時間を乗せず、呼ぶ側の AI にとって最初の参照が速くなる）。

* **いつ**: tsserver 系は ``initialize`` の直後（``indexing`` の間。``EARLY_LANGUAGES``）。tsserver は文書を開かれるまでプロジェクトを
  読まず、typescript-language-server は開かれるまで ``$/progress`` も診断も出さない（Lucianke の ``ready`` は ``ready_fallback_s``
  の 15 秒を待っていた）。直後に開かせれば、プロジェクトの読み込みと検査器の作成は ``indexing`` の間（呼び出しは降格応答で答え、
  LSP に要求を送らない）に済み、``ready`` は本物の合図（進捗の終わり・最初の診断）で来る。``ready`` の後に開かせると、その重い
  1 回が ``ready`` 直後の呼び出しの要求とぶつかった（ky で 2.3 秒）。basedpyright は ``ready`` を待ってから（起動直後の解析の
  合図を変えない）。
* **呼び出しを待たせない**: 合図（``LspPool.on_initialized``）はタスクを投げるだけ。候補は索引から引き、索引の走査の途中なら
  走査の列挙（``Indexer.last_listing``）から同じ規則で選ぶ（走査が終わるのを待たない。ky で ``initialize`` から 2.3〜2.6 秒
  待っていた）。候補の選び方と本文の読み込みはスレッドで、背景の印（``core.foreground.background``）を付けて走らせる（前景の
  呼び出しが忙しい間は GIL を譲る）。送る前に、前景の呼び出しが落ち着くのを ``Foreground.apause`` の上限まで待つ。
* **ready の直後に残る尾**: tsserver はプロジェクトを読んだ後、開いた文書の検査を一巡する（typescript-language-server の
  ``geterr``。1 ファイルずつ、``references`` はその切れ目を待つ）。``ready`` の直後の数秒に来た冷えた要求は、その一巡と
  ぶつかって 0.5〜1 秒かかることがある（LSP の側のもの）。一巡が済むまで ``ready`` にしない手は採らない（その間の呼び出しが
  exact の代わりに構文木の答えになり、遅い時間を計測の外へ動かすだけになる）。
* **組み直しを 1 回に**: ``didOpen`` は 1 回のイベントループの番でまとめて送る（``features.preload_documents``。途中で ``await``
  しないので、他の要求が間に入らない）。
* **答えを変えない**: 先開きは「開いておく」だけ。先開きの途中に来た T-06・T-07 は、今までどおり自分の ``prime_set`` で足りない
  ファイルを開かせてから聞く（開いている文書は ``is_open`` で飛ばすので二重に開かない）。exact の答えは LSP が見たものそのまま。
* 開いておける上限（``LSP_OPEN_DOCS_MAX`` = 200）に収まらないワークスペースは、上限の中で関係の多いファイルから選ぶ
  （``preopen_set``）。前景が開いた文書は追い出さない。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from lucianke.core.foreground import FOREGROUND, Foreground, background
from lucianke.core.model import RelPath
from lucianke.lsp import features
from lucianke.lsp.pool import LspHandle, LspState
from lucianke.tools import _lsp_exact as lx

_log = logging.getLogger("lucianke.tools.lsp_preopen")

__all__ = ["EARLY_LANGUAGES", "POLL_S", "PreopenReport", "preopen", "wants_preopen"]

POLL_S = 0.25
"""索引の走査が終わるのを・LSP が ``ready`` になるのを見に行く間隔（秒）。"""

EARLY_LANGUAGES = lx.TS_FAMILY
"""``initialize`` の直後（``indexing`` の間）に先開きを送る言語。ほかは ``ready`` を待つ。"""

_LIVE = (LspState.INDEXING, LspState.READY)


@dataclass(frozen=True, slots=True)
class PreopenReport:
    """先開きの結果（ログと試験用）。``reason`` は開かなかった理由（開いたら ``None``）。"""

    opened: int = 0
    candidates: int = 0
    skipped: int = 0
    elapsed_ms: float = 0.0
    reason: str | None = None


def wants_preopen(handle: Any) -> bool:
    """先開きの対象のハンドルか（型解決のサーバーで、参照元を先に開かせる言語。``_lsp_exact.needs_priming``）。"""
    return getattr(handle, "role", None) == "types" and lx.needs_priming(str(getattr(handle, "language_id", "")))


def _alive(handle: LspHandle, client: Any) -> bool:
    return handle.state in _LIVE and handle.client is client and client is not None and client.is_connected


async def preopen(
    handle: LspHandle,
    readers: Any,
    *,
    building: Callable[[], object | None] = lambda: None,
    listing: Callable[[], Sequence[RelPath] | None] = lambda: None,
    foreground: Foreground = FOREGROUND,
    poll_s: float = POLL_S,
) -> PreopenReport:
    """``handle`` に、参照元になりうるファイルを背景でまとめて開かせる（``initialize`` を済ませたときの 1 回）。

    ``readers`` は索引の ``ReaderPool``（``reader()`` で接続を貸す）、``building`` は索引の走査の途中なら真を返す
    （``Indexer.building``）、``listing`` は走査の列挙（``Indexer.last_listing``。列挙し終える前は ``None``）。候補は索引から
    引くが、走査の途中なら列挙から同じ規則で選ぶ（走査が終わるのを待たない: tsserver の読み込みと検査を早く始めるほど、
    ``ready`` の後に残る検査の一巡が短い）。列挙もまだなら列挙し終えるまで待つ。ハンドルが起動し直した・止まったら何もせずに
    戻る（起動し直した後にまた走る）。
    """
    client = handle.client
    t0 = time.monotonic()
    early = handle.language_id in EARLY_LANGUAGES
    with background():
        # ``ready`` を待つ言語は ``ready`` まで待つ。走査の途中で列挙もまだなら、列挙し終えるまで待つ
        while True:
            if not _alive(handle, client):
                return PreopenReport(reason="restarted")
            listed = listing() if building() is not None else None
            if (early or handle.state is LspState.READY) and (listed is not None or building() is None):
                break
            await asyncio.sleep(poll_s)
        documents = handle.documents
        open_now = set(documents.open_paths())

        def plan() -> lx.PrimeSet:
            with readers.reader() as conn:
                return lx.preopen_set(
                    conn,
                    handle.ws,
                    handle.language_id,
                    is_open=open_now.__contains__,
                    open_count=len(open_now),
                    max_open=documents.max_open,
                    scope=getattr(handle, "analysis_scope", None),
                    listing=listed,
                )

        chosen = await asyncio.to_thread(plan)
        if not chosen.files:
            return PreopenReport(skipped=chosen.skipped, reason="nothing_to_open")
        docs = await asyncio.to_thread(features.read_documents, handle.ws, chosen.files)
        await foreground.apause()  # 前景の呼び出しが落ち着くのを待つ（上限 MAX_YIELD_S。続くなら待たずに送る）
        if not _alive(handle, client):
            return PreopenReport(candidates=len(chosen.files), skipped=chosen.skipped, reason="restarted")
        state = handle.state.value
        opened = features.preload_documents(handle, docs, client)
    report = PreopenReport(
        opened=opened,
        candidates=len(chosen.files),
        skipped=chosen.skipped,
        elapsed_ms=round((time.monotonic() - t0) * 1000, 1),
    )
    _log.info(
        "pre-opened %d of %d file(s) for the %s LSP while %s (%d over the limit) in %.0f ms",
        report.opened, report.candidates, handle.language_id, state, report.skipped, report.elapsed_ms,
        extra={"event": "lsp_preopen"},
    )  # fmt: skip
    return report
