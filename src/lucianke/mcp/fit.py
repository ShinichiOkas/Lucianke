"""応答全体を ``max_tokens`` に収める（仕様書 §5.6-4・§5.7、設計書 §7.2.2、ADR-4）。

パッカー（``budget.Packer``）が詰めるのはレコードとトレーラだけである。ヘッダ・警告行・``head_kv``・降格の警告行
（compact）や、封筒の全キー・``_resolution_meta``・``data_static``（json）は、呼び手が数えて ``reserved_tokens`` で渡す契約
（``Packer`` の docstring）。パイプラインがそれを渡していなかったため、ヘッダの ``~used`` が数え直しより 1 行ぶん
（約 28〜30 トークン）小さく、``max_tokens`` を超える応答が出た（BUG-0014）。json はさらにレコードを ``Record.lines`` で
数えていたため、実際に描かれる ``Record.data`` の約 1/7 に過小だった（BUG-0017）。

この関数は次の順で「描画後の全文が ``max_tokens`` 以下」で「``used_tokens_est`` が全文の数え直しに一致」する応答を作る。

1. **前置きを数える**: レコード 0 件のページで同じレンダラを実際に走らせた全文（``used_tokens_est`` と ``timing_ms`` は
   桁数が最大の値）を数え、``reserved_tokens`` にする。降格の警告行・ヘッダの ``conf=``・json の ``_resolution_meta`` は
   ページに入ったレコードの精度で出るので、精度の集計が変わる位置で段に分け、段ごとにその段の最後の形の集計で数えた値
   （前の段までの最大）を、先頭から m 件のページの前置き ``reserved_for(m)`` として渡す（BUG-0116。以前は全レコードの集計で
   1 つだけ数え、そのページに出ない注意文の分まで取り分けていた）。
2. **詰める**: ``reserved_tokens`` ・``reserved_for`` を渡してパッカーを呼ぶ。json は ``Record.data`` の JSON で数える
   （``json_out.record_text`` / ``trailer_text``）。
3. **全文を数え直す**: 実際に描いた全文を ``TokenEstimator`` で数える。``max_tokens`` を超えていれば（前置きの見積もりが
   足りなかった。``returned`` の桁数など）超過分を前置き（``reserved_tokens`` と ``reserved_for`` の全部の形）に足して 2 に戻る。
4. **``used_tokens_est`` を数え直しに合わせる**: ヘッダの ``~used``・json の ``budget.used_tokens_est`` の桁数は全文の数に効く
   （数字は 1 文字ずつ数える）ので、数え直した値で描き直して、値が動かなくなるまで繰り返す（桁数が変わらなければ 1 回で収まる）。
   数えるときの ``timing_ms`` は最大の桁数に固定する（実際の全文以上の上限。同じ入力なら呼び出しごとに同じ値になる）。
5. **呼び直しの目安を確かめる**: 1 件も入らない（前置きだけで超える）ときは、警告の「raise max_tokens to ~N」の N で 1〜4 を
   やり直し、警告なしに収まる（レコードがあれば 1 件以上入る）まで N を上げてから、その N で警告を書く（BUG-0110。N では
   ``~used/max`` の桁数などで前置きが大きくなりうる。言われたとおりに呼び直してまた 0 件にならないように）。

dot / mermaid は BUG-0053 で追加（FITTED_FORMATS）。``Packer.pack`` を通す（records=0 なので実質 no-op）が、probe で
前置きを数える。``page.max_tokens`` をレンダラ（``graph._render``）に伝え、レンダラがグラフのノード / エッジ数を二分探索で
削減して全文を ``max_tokens`` 以下に収める。
"""

from __future__ import annotations

import bisect
import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from lucianke.core.errors import LuciankeError
from lucianke.core.model import Confidence
from lucianke.core.records import ResolutionMeta, ToolOutput, split_offset
from lucianke.core.tokens import TokenEstimator
from lucianke.mcp.envelope import build_meta
from lucianke.mcp.render import json_out
from lucianke.mcp.types import CallInfo, Page, Rendered, RenderInput

__all__ = ["FITTED_FORMATS", "Fitted", "pack_and_render"]

#: 前置きを数えて全文で収める形式。compact・json はレコードを詰める。dot・mermaid はグラフを切り詰める（BUG-0053）。
FITTED_FORMATS: Final = ("compact", "json", "dot", "mermaid")
_PROBE_TIMING_MS: Final = 99_999  # 前置きの見積もりに使う ``timing_ms``（json の桁数を最大幅にする）
_MAX_PACK_ROUNDS: Final = 8  # 前置きの見積もりが足りなかったときの詰め直しの上限
_MAX_USED_ROUNDS: Final = 5  # ``used_tokens_est`` の桁数が変わるときの描き直しの上限
#: 1 件も入らないときの呼び直しの目安を、描いた全文で確かめて上げる回数の上限（BUG-0110）
_MAX_NEED_ROUNDS: Final = 6

#: 形式ごとに、パッカーが応答の本文を見積もるときの関数。compact は ``Packer`` の既定（``Record.lines`` ・トレーラの 2 行）。
#: dot / mermaid は records=0 なので実質不使用だが、共通ループで呼ばれるため空 dict を登録する（BUG-0053）。
_PACKER_KWARGS: Final[Mapping[str, Mapping[str, Any]]] = {
    "compact": {},
    "json": {"record_text": json_out.record_text, "trailer_text": json_out.trailer_text, "form": "json"},
    "dot": {},
    "mermaid": {},
}


@dataclass(frozen=True, slots=True)
class Fitted:
    """詰めて描いた結果。``page.used_tokens_est`` は描いた全文の数え直しの値（dot / mermaid はパッカーの値のまま）。"""

    rendered: Rendered
    page: Page
    meta: ResolutionMeta


def pack_and_render(
    *,
    packer: Any,
    renderer: Callable[[RenderInput], Rendered],
    fmt: str,
    out: ToolOutput,
    call: CallInfo,
    offset: int,
    cursor_factory: Callable[[int], str] | None,
    warnings: Sequence[str],
    records_key: str,
    timing_ms: Callable[[], int],
    error: LuciankeError | None = None,
) -> Fitted:
    """``out`` を ``call.max_tokens`` に詰めて ``renderer`` で描く（モジュールの docstring の 1〜4）。

    ``error`` を渡すと、失敗応答（``E_TIMEOUT`` の部分結果など）のヘッダ・メッセージ・候補も前置きに数える。
    ``timing_ms`` は描画ごとに呼ぶ（サーバー内の処理時間の現在値）。
    """

    def render(page: Page, meta: ResolutionMeta, *, timing: int, max_tokens: int | None = None) -> Rendered:
        at = call if max_tokens is None else dataclasses.replace(call, max_tokens=max_tokens)
        return renderer(
            RenderInput(
                call=dataclasses.replace(at, timing_ms=timing),
                meta=meta,
                out=out,
                page=page,
                error=error,
                warnings=tuple(warnings),
                records_key=records_key,
            )
        )

    if fmt not in FITTED_FORMATS:
        page = packer.pack(out, max_tokens=call.max_tokens, offset=offset, cursor_factory=cursor_factory)
        meta = build_meta(out, page)
        return Fitted(render(page, meta, timing=timing_ms()), page, meta)

    estimator: TokenEstimator = getattr(packer, "estimator", None) or TokenEstimator()
    est = estimator.count
    kwargs = _PACKER_KWARGS[fmt]
    total = max(out.total_estimate or 0, len(out.records))

    def settle(page: Page, meta: ResolutionMeta) -> tuple[Rendered, Page, int]:
        """4. ``used_tokens_est`` を全文の数え直しに合わせる。``(描いた結果, 描いたページ, 数え直した値)``。

        数え直しは ``timing_ms`` を最大の桁数にした全文で行う（実際の全文以上の上限になる。同じ入力なら呼び出しごとの
        処理時間にかかわらず同じ値になる。json の ``timing_ms`` は全文に入る）。
        """
        mt = page.max_tokens
        used = page.used_tokens_est
        for _ in range(_MAX_USED_ROUNDS):
            shown = dataclasses.replace(page, used_tokens_est=used)
            recount = est(render(shown, meta, timing=_PROBE_TIMING_MS, max_tokens=mt).text)
            if recount == used:
                break
            used = recount
        # 収束しなければ（桁数の境目）ヘッダの値は数え直しと数トークンずれうる
        return render(shown, meta, timing=timing_ms(), max_tokens=mt), shown, recount

    start, _ = split_offset(max(offset, 0))
    rest = [out.records[i] for i in range(start, len(out.records))]
    # 先頭から m 件のページの精度の集計（降格の注意文・ヘッダの conf=）が変わる位置。段 j は m が (ends[j-1], ends[j]] の形
    ends: list[int] = []
    level: Confidence | None = None
    for m, rec in enumerate(rest, 1):
        if rec.section is not None:  # 補足の欄は集計に数えない（``build_meta``。BUG-0107）
            continue
        lower = rec.confidence if level is None else min(level, rec.confidence)
        if lower != level and m > 1:
            ends.append(m - 1)
        level = lower
    ends.append(len(rest))

    def preambles(max_tokens: int) -> tuple[int, list[int]]:
        """1. 前置き（レコード以外の全部）を、同じレンダラで 0 件のページを描いて数える。``(0 件のページの前置き, 段ごとの前置き)``。

        降格の注意文とヘッダの ``conf=`` はページに入ったレコードの精度で出る（``build_meta``）ので、段ごとに、その段の最後の形
        （先頭から ``ends[j]`` 件。json の ``confidence_breakdown`` の件数も段の中で最大）の集計で描いて数え、前の段までの最大を
        取る（件数について減らない。予算に依らないので鎖の単調性を保つ）。以前は全レコードの集計で 1 つだけ数えていたので、
        後ろに精度の低いレコードが 1 つでもあると、そのページに出ない注意文の分（T-01 で約 76 トークン）を取り分けていた
        （BUG-0116）。0 件のページはレコード 0 件の集計（``out.engine.confidence``）で数える。
        """
        probe_page = Page(
            records=(),
            next_cursor=None,
            summary=None,
            total_estimate=total,
            remaining=0,
            used_tokens_est=max_tokens,
            max_tokens=max_tokens,
            truncated=False,
        )
        counted: dict[str, int] = {}

        def probe(records: Sequence[Any]) -> int:
            meta = build_meta(out, dataclasses.replace(probe_page, records=tuple(records)))
            key = repr(meta)
            if key not in counted:
                counted[key] = est(render(probe_page, meta, timing=_PROBE_TIMING_MS, max_tokens=max_tokens).text)
            return counted[key]

        steps: list[int] = []
        for end in ends:
            steps.append(max(probe(rest[:end]), steps[-1] if steps else 0))
        return probe(()), steps

    def fit_at(max_tokens: int, min_need: int) -> tuple[Rendered, Page, ResolutionMeta, int]:
        """1〜4 を ``max_tokens`` で行う。``(描いた結果, ページ, メタ, 数え直した値)``。``min_need`` はパッカーへ渡す
        （1 件も入らないときの呼び直しの目安の下限。5 で確かめた値）。"""
        reserved, steps = preambles(max_tokens)
        extra = 0  # 3 で前置きの見積もりが足りなかった分（全部の形に足す）

        def reserved_for(m: int) -> int:
            return steps[min(bisect.bisect_left(ends, m), len(steps) - 1)] + extra

        for _ in range(_MAX_PACK_ROUNDS):
            # 2. 詰める
            page = packer.pack(
                out,
                max_tokens=max_tokens,
                offset=offset,
                reserved_tokens=reserved + extra,
                reserved_for=reserved_for,
                cursor_factory=cursor_factory,
                min_need=min_need,
                **kwargs,
            )
            meta = build_meta(out, page)
            rendered, page, recount = settle(page, meta)
            # 3. 全文が予算に収まったか。1 件も入っていないなら、これ以上減らせない（前置きだけで超える呼び出し側の誤り）
            if recount <= max_tokens or not page.records:
                break
            extra += recount - max_tokens
        return rendered, page, meta, recount

    rendered, page, meta, _ = fit_at(call.max_tokens, 0)
    if page.need_max_tokens is not None:
        # 5. 1 件も入らない（前置きだけで超える）。警告の「raise max_tokens to ~N」を、N で描き直した全文で確かめる
        #    （N では ``~used/max`` の桁数が増える・json の封筒の数字が長くなるなど、前置きが今の予算で数えたより大きくなりうる。
        #    パッカーの見積もりのままだと、言われたとおりに呼び直してもまた 0 件のことがありえた。BUG-0110）。
        #    パッカーの見積もりから、N で警告なしに収まる（レコードがあれば 1 件以上入る）まで上げ、その値で警告を書き直す
        need = page.need_max_tokens
        for _ in range(_MAX_NEED_ROUNDS):
            _, at_need, _, recount = fit_at(need, 0)
            if at_need.need_max_tokens is None and recount <= need:
                break
            need = max(need + 1, at_need.need_max_tokens or 0, recount)
        rendered, page, meta, _ = fit_at(call.max_tokens, need)  # 最後に描くのは呼ばれた予算の応答
    return Fitted(rendered, page, meta)
