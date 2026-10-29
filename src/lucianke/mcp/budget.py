"""予算・ページネーション・カーソル（設計書 ADR-4・ADR-8、§7.2.1〜7.2.3、仕様書 §5.6・§5.7）。

* ``TokenEstimator``: 文字種別の係数による ``o200k_base`` 互換の近似。実体は ``core/tokens.py``（``syntax`` の骨格と
  共有するため ``core`` に置いた）。ここから同じ名前で再公開する（``TokenEstimator`` ・``TOKENS_PER_CHAR``）。
* ``Packer``: 優先順位つきのレコード列を ``max_tokens`` の中に詰める。レコードは途中で切らない。
  ``used_tokens_est <= max_tokens`` を保証するのはここである（ADR-4 の帰結）。
* ``CursorCodec``: ``next_cursor`` の符号化と復号（ADR-8）。署名は付けない（不透明だが改ざん防止ではない）。
* ``trailer_lines``: ``# summary:`` と ``# more:`` の組み立て。パッカーが見積もる文字列とレンダラが出す文字列を
  一致させるため、レンダラはこの関数を使うこと。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.records import Record, ToolOutput, join_offset, split_offset
from lucianke.core.tokens import TOKENS_PER_CHAR as TOKENS_PER_CHAR  # 再公開（bench/tokens が使う）
from lucianke.core.tokens import TokenEstimator
from lucianke.mcp.types import Page

#: 応答の本文の見積もりの単位。パッカーは形式ごとに差し替えて数える（compact は既定。json は ``render/json_out.py``）。
#: ``RecordText``: 1 レコードが描かれるときの本文。``TrailerText``: 続きがあるときに増える本文（要約・カーソル・残り件数）。
RecordText = Callable[[Record], str]
TrailerText = Callable[[str | None, str | None, int], str]

# --- トレーラ ---------------------------------------------------------------------------------

SUMMARY_MAX_CHARS: Final = (
    400  # ``# summary:`` 本文の最大文字数（超過は「…」）。予算の保証のため、ツールの要約にも適用する
)
SUMMARY_MAX_GROUPS: Final = 5  # 既定の要約に載せる group の最大数。残りは ``others`` にまとめる
SHRINK_RETRIES: Final = 16  # 別の形式（json）で数えて縮退版が収まらないとき、``Record.shrink`` の ``ask`` を二分探索する回数の上限


def trailer_lines(summary: str | None, cursor: str | None, remaining: int) -> tuple[str, ...]:
    """compact 形式のトレーラ行（仕様書 §5.4.2）。続きがあるときだけ使う。

    ``# summary: <summary>`` と ``# more: cursor=<cursor> remaining≈<N>`` の 2 行。``summary`` が空なら前者を省く。
    ``cursor`` が無ければ ``cursor=`` を省く。パッカーはこの行を数えて予算に入れる。
    """
    lines: list[str] = []
    if summary:
        lines.append(f"# summary: {summary}")
    more = "# more:"
    if cursor:
        more += f" cursor={cursor}"
    lines.append(f"{more} remaining≈{remaining}")
    return tuple(lines)


def default_summary(rest: Sequence[Record], unseen: int = 0) -> str:
    """入らなかったレコードの既定の要約。``group`` ごとの件数（出現順、最大 ``SUMMARY_MAX_GROUPS`` 個）。

    例 ``38 more (src/a.py: 20, src/b.py: 18)``。``group`` を持つレコードが無ければ ``38 more``。
    ``unseen`` はツールが具体化せずに残した件数（``ToolOutput.total_estimate`` が ``len(records)`` を超える分）。
    どの ``group`` かは分からないので ``others`` に数える。
    """
    counts: dict[str, int] = {}
    ungrouped = unseen
    for r in rest:
        if r.group is None:
            ungrouped += 1
        else:
            counts[r.group] = counts.get(r.group, 0) + 1
    total = len(rest) + unseen
    if not counts:
        return f"{total} more"
    items = list(counts.items())
    parts = [f"{g}: {c}" for g, c in items[:SUMMARY_MAX_GROUPS]]
    others = sum(c for _, c in items[SUMMARY_MAX_GROUPS:]) + ungrouped
    if others:
        parts.append(f"others: {others}")
    return f"{total} more ({', '.join(parts)})"


def _one_line(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- カーソル -------------------------------------------------------------------------------

_CURSOR_EXCLUDED_ARGS: Final = frozenset({"cursor", "max_tokens", "format"})


@dataclass(frozen=True, slots=True, kw_only=True)
class CursorState:
    tool: str
    args_hash: str
    offset: int
    data_version: int
    workspace_id: str | None


def _invalid_cursor(reason: str) -> LuciankeError:
    return LuciankeError(
        ErrorCode.E_INVALID_ARGUMENT, f"cursor is invalid: {reason}", details={"param": "cursor"}
    )


class CursorCodec:
    """``next_cursor`` の符号化と復号（設計書 §7.2.3、ADR-8）。

    ``base64url(json({"t": tool, "a": sha1(args)[:12], "o": offset, "v": data_version, "w": workspace_id}))``
    （パディングなし）。署名は付けない。再エンコードした JSON は通る。守るのは「別の呼び出しに使い回す」誤用であり、
    悪意ある改ざんではない。
    """

    @staticmethod
    def hash_args(args: Mapping[str, Any]) -> str:
        """``cursor`` ・ ``max_tokens`` ・ ``format`` を除いた引数の正規化 JSON の SHA-1 先頭 12 桁。"""
        norm = {k: v for k, v in args.items() if k not in _CURSOR_EXCLUDED_ARGS}
        text = json.dumps(norm, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

    def encode(
        self, *, tool: str, args: Mapping[str, Any], offset: int, data_version: int, workspace_id: str | None
    ) -> str:
        payload = {"t": tool, "a": self.hash_args(args), "o": offset, "v": data_version, "w": workspace_id}
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def decode(
        self,
        token: str,
        *,
        tool: str,
        args: Mapping[str, Any],
        data_version: int,
        workspace_id: str | None,
    ) -> CursorState:
        """復号して検査する。

        壊れた文字列、``t`` / ``a`` / ``w`` の不一致は ``E_INVALID_ARGUMENT``（``details.param == "cursor"``）。
        ``v`` が現在の ``data_version`` と違えば ``E_CURSOR_EXPIRED``。前者を先に検査する
        （別ツールのカーソルが「失効」と誤診されないように）。
        """
        try:
            padded = token + "=" * (-len(token) % 4)
            obj = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8"))
        except (ValueError, TypeError, binascii.Error):  # UnicodeDecodeError と JSONDecodeError は ValueError
            raise _invalid_cursor("malformed token") from None
        if not isinstance(obj, dict):
            raise _invalid_cursor("malformed token")
        t, a, o, v, w = (obj.get(k) for k in ("t", "a", "o", "v", "w"))
        if (
            not isinstance(t, str)
            or not isinstance(a, str)
            or type(o) is not int
            or o < 0
            or type(v) is not int
            or not (w is None or isinstance(w, str))
        ):
            raise _invalid_cursor("malformed token")
        if t != tool or a != self.hash_args(args) or w != workspace_id:
            raise _invalid_cursor("the cursor was issued for a different tool, arguments or workspace")
        if v != data_version:
            raise LuciankeError(
                ErrorCode.E_CURSOR_EXPIRED,
                "the data changed since the cursor was issued; call again from the first page",
            )
        return CursorState(tool=t, args_hash=a, offset=o, data_version=v, workspace_id=w)


# --- パッカー -------------------------------------------------------------------------------


def _record_text(rec: Record) -> str:
    """レコードが描かれるときの本文。各行の終端の改行も含める（改行は 1 文字 0.6 トークンで、レコードごとの数を足した和が
    応答全体の推定以上になるようにする。行が無いレコードは何も描かれない）。"""
    return "\n".join(rec.lines) + "\n" if rec.lines else ""


def _trailer_text(summary: str | None, cursor: str | None, remaining: int) -> str:
    """トレーラの本文（終端の改行を含む。``_record_text`` と同じ理由）。"""
    return "\n".join(trailer_lines(summary, cursor, remaining)) + "\n"


@dataclass(frozen=True, slots=True)
class _Fit:
    """トレーラを含めて予算に収まった（または収められなかった）結果。"""

    m: int  # 入れるレコード数
    summary: str | None
    cursor: str | None
    warnings: tuple[str, ...]
    used: int
    over: bool  # m = 0 まで減らしても ``max_tokens`` を超える（``reserved_tokens`` が大きすぎる呼び出し側の誤り）
    remaining: int | None = None  # 最後の 1 件が途中で切れている（``Record.resume``）ときの残り。None なら添字から数える


class Packer:
    """レコード列を予算内に詰める（設計書 §7.2.2、仕様書 §5.6）。

    契約: ``reserved_tokens``（ヘッダ・``head_kv``・呼び手が出す警告の分）は呼び手が数えて渡す。
    ``reserved_tokens`` とトレーラの最小形だけで ``max_tokens`` を超える（``MIN_MAX_TOKENS`` 未満の指定など）場合は
    保証できず、レコード 0 件で返す（``used_tokens_est`` は超過した実測値）。

    レコードが 1 件も入らないとき（仕様書 §5.6-4）は、レコード 0 件・``warnings`` に理由と必要な ``max_tokens`` の目安の
    成功応答で、``next_cursor`` は出さない（BUG-0015）。オフセットが進まないカーソルは同じ呼び出しの繰り返しで、辿る
    クライアントが止まらない。カーソルは ``max_tokens`` を含まない引数で結ばれているので、呼び出しに付けたカーソルは
    ``max_tokens`` を上げた呼び直しにそのまま使える。``next_cursor`` を返すページは必ず 1 件以上を含み、オフセットが進む。
    """

    def __init__(self, estimator: TokenEstimator | None = None) -> None:
        self.estimator = estimator or TokenEstimator()

    # -- 公開 -----------------------------------------------------------------------------

    def pack(
        self,
        out: ToolOutput,
        *,
        max_tokens: int,
        offset: int = 0,
        reserved_tokens: int = 0,
        cursor_factory: Callable[[int], str] | None = None,
        record_text: RecordText | None = None,
        trailer_text: TrailerText | None = None,
    ) -> Page:
        est = self.estimator.count
        # 形式ごとの本文の見積もり（既定は compact の行。json は ``Record.data`` の JSON。BUG-0017）
        rt = record_text or _record_text
        tt = trailer_text or _trailer_text
        records = out.records
        n = len(records)
        start, _ = split_offset(max(offset, 0))  # 上位ビットは同じレコードの再開位置（ツールが読む。``Record.resume``）
        # ``total_estimate`` が ``len(records)`` を超えるとき、ツールは先頭の ``n`` 件だけを渡し（``limit`` で打ち切った遅延列）、
        # 残り ``unseen`` 件を具体化していない。それも「続き」なので、渡された分が全部入ってもカーソルとトレーラが要る
        # （BUG-0008）。``total_estimate`` が ``len(records)`` 以下なら無視する。
        if out.total_estimate is None:
            unseen = 0
            total = max(start, n)
        else:
            unseen = max(0, out.total_estimate - n)
            total = n + unseen
        avail = max_tokens - reserved_tokens

        if start >= n:  # 詰めるものが無い（続きも無い）
            return self._page((), reserved_tokens, max_tokens, total, 0, None, None, ())

        tok: dict[int, int] = {}

        def cost_of(i: int) -> int:
            if i not in tok:
                tok[i] = est(rt(records[i]))
            return tok[i]

        # 1. トレーラ無しで全部入るか（入るなら縮退もトレーラも不要）。具体化していない続きがあるときはトレーラが要る
        if not unseen:
            running = 0
            for i in range(start, n):
                running += cost_of(i)
                if running > avail:
                    break
            else:
                kept = tuple(records[i].with_tokens(tok[i]) for i in range(start, n))
                return self._page(kept, reserved_tokens + running, max_tokens, total, 0, None, None, ())

        # 2. 先頭から詰める。後ろに続きが残る間はトレーラの見込み分を差し引く。最後の 1 件はトレーラが要らない
        #    （具体化していない続きがあれば、最後の 1 件にも要る）
        shrunk: dict[int, Record | None] = {}  # 縮退は 1 レコードにつき一度だけ呼ぶ（2 周目は結果を使い回す）

        def greedy(reserve: int, *, with_summary: bool = True) -> tuple[list[Record], list[int]]:
            included: list[Record] = []
            costs: list[int] = []
            left = avail
            for i in range(start, n):
                rec = records[i]
                budget = left - (reserve if i < n - 1 or unseen else 0)
                t = cost_of(i)
                hold = 0  # 途中で切れた縮退のために取り分けるトレーラの見込み
                if t > budget and rec.shrink is not None:
                    if budget > 1 and i not in shrunk:
                        # 縮退は行の本文（終端の改行なし）で測って予算に収める。終端の改行は最大 1 トークンなので、
                        # その分を引いて渡せば ``_record_text`` で数え直しても収まる
                        shrunk[i] = self._shrink(rec, budget - 1, rt)
                    small = shrunk.get(i)
                    if small is not None and small.resume is not None:
                        # 途中で切れた縮退は、最後の 1 件でも続きのトレーラ（同じレコードの再開カーソルと要約）が要る。
                        # そのカーソルは再開位置の分だけ長いので、実際の形で見込み直して収まるまで詰め直す
                        hold = self._resume_reserve(out, records, i, small.resume, n, unseen, cursor_factory, with_summary, tt)
                        if est(rt(small)) > left - hold:
                            small = self._shrink(rec, left - hold - 1, rt) if left - hold > 1 else None
                            shrunk[i] = small
                    if small is not None:
                        rec, t = small, est(rt(small))
                        if rec.resume is not None:
                            budget = min(budget, left - hold)
                if t > budget:
                    break  # 飛ばして後ろを詰めない（優先順位の順を保つ）
                included.append(rec.with_tokens(t))
                costs.append(t)
                left -= t
                if rec.resume is not None:
                    break  # 途中で切れたレコードの後ろは詰めない（続きは同じレコードから。順を保つ）
            return included, costs

        included, costs = greedy(self._trailer_reserve(out, start, n, unseen, cursor_factory, trailer_text=tt))
        if start + len(included) >= n and not unseen and not (included and included[-1].resume is not None):  # 縮退で全部入った
            return self._page(
                tuple(included), reserved_tokens + sum(costs), max_tokens, total, 0, None, None, ()
            )

        # 3. 実際のトレーラ（カーソル・要約）を作って数え、収まらなければ末尾から外す
        need_first = cost_of(start)
        fit = self._fit(
            out,
            records,
            start,
            n,
            len(included),
            costs,
            reserved_tokens,
            max_tokens,
            cursor_factory,
            unseen=unseen,
            trailer_text=tt,
            with_summary=True,
            need_first=need_first,
            resumes=[r.resume for r in included],
        )
        if fit.m == 0:
            # 要約（SHOULD）を省けばレコードが入るかもしれない。要約の見込みを引かずに詰め直す
            included, costs = greedy(
                self._trailer_reserve(out, start, n, unseen, cursor_factory, with_summary=False, trailer_text=tt), with_summary=False
            )
            if start + len(included) >= n and not unseen and not (included and included[-1].resume is not None):
                return self._page(
                    tuple(included), reserved_tokens + sum(costs), max_tokens, total, 0, None, None, ()
                )
            alt = self._fit(
                out,
                records,
                start,
                n,
                len(included),
                costs,
                reserved_tokens,
                max_tokens,
                cursor_factory,
                unseen=unseen,
                trailer_text=tt,
                with_summary=False,
                need_first=need_first,
                resumes=[r.resume for r in included],
            )
            if alt.m > fit.m or fit.over:
                fit = alt
        kept = tuple(included[: fit.m])
        return self._page(
            kept,
            fit.used,
            max_tokens,
            total,
            fit.remaining if fit.remaining is not None else n - (start + fit.m) + unseen,
            fit.cursor,
            fit.summary,
            fit.warnings,
        )

    # -- 内部 -----------------------------------------------------------------------------

    @staticmethod
    def _page(
        kept: tuple[Record, ...],
        used: int,
        max_tokens: int,
        total: int,
        remaining: int,
        cursor: str | None,
        summary: str | None,
        warnings: tuple[str, ...],
    ) -> Page:
        return Page(
            records=kept,
            next_cursor=cursor,
            summary=summary,
            total_estimate=total,
            remaining=remaining,
            used_tokens_est=used,
            max_tokens=max_tokens,
            truncated=remaining > 0,
            warnings=warnings,
        )

    def _shrink(self, rec: Record, ask: int, record_text: RecordText) -> Record | None:
        """``rec.shrink(ask)``（``ask`` は縮退版が収まるべき行の本文のトークン数）。

        ``Record.shrink`` は compact の行の大きさで収める約束である。compact（既定）はそのまま 1 回呼ぶ。別の形式で数えるときは
        （json は ``Record.data`` の JSON）、返った縮退版をその形式で数えて ``ask + 1`` に収まればそのまま返す。収まらなければ、
        json の大きさは compact の行に比例しない（記号・メタ・エスケープの固定分がある）ので、``ask`` を二分探索して収まる最大の
        縮退版を探す（縮退版の大きさは ``ask`` に対して単調。最大 ``SHRINK_RETRIES`` 回呼ぶ）。収まるものが無ければ ``None``。
        """
        small = rec.shrink(ask) if rec.shrink is not None else None
        if record_text is _record_text or small is None:
            return small  # compact は 1 回。compact の行でも収まらないなら、より小さい ``ask`` でも収まらない（単調）
        budget = ask + 1
        est = self.estimator.count
        if est(record_text(small)) <= budget:
            return small
        lo, hi, best = 1, ask - 1, None
        for _ in range(SHRINK_RETRIES):
            if lo > hi:
                break
            mid = (lo + hi) // 2
            cand = rec.shrink(mid)
            if cand is None:
                lo = mid + 1  # 小さすぎて何も入らない（compact の行でも最小の縮退が収まらない）。大きくする
            elif est(record_text(cand)) <= budget:
                best, lo = cand, mid + 1
            else:
                hi = mid - 1
        return best

    @staticmethod
    def _summary(out: ToolOutput, rest: Sequence[Record], unseen: int = 0) -> str:
        """``rest`` は具体化した残り、``unseen`` は具体化していない残りの件数。ツールの ``summarize`` は ``rest`` だけを見る
        （``rest`` が空で ``unseen`` だけあるときは既定の要約）。"""
        text = out.summarize(rest) if out.summarize is not None and (rest or not unseen) else ""
        return _one_line(text or default_summary(rest, unseen))

    def _trailer_reserve(
        self,
        out: ToolOutput,
        start: int,
        n: int,
        unseen: int,
        cursor_factory: Callable[[int], str] | None,
        *,
        with_summary: bool = True,
        trailer_text: TrailerText = _trailer_text,
    ) -> int:
        """詰める前のトレーラの見込み。実際の値は ``_fit`` が確かめるので、ここは大きめの近似でよい。"""
        summary = self._summary(out, out.records[start + 1 :], unseen) if with_summary else None
        cursor = cursor_factory(n - 1) if cursor_factory is not None else None
        return self.estimator.count(trailer_text(summary, cursor, n - start + unseen))

    def _resume_reserve(
        self,
        out: ToolOutput,
        records: Sequence[Record],
        i: int,
        resume: int,
        n: int,
        unseen: int,
        cursor_factory: Callable[[int], str] | None,
        with_summary: bool,
        trailer_text: TrailerText = _trailer_text,
    ) -> int:
        """``records[i]`` を途中で切って入れるとき（``Record.resume``）のトレーラの見込み。続きは同じレコードの再開位置から。"""
        rest = records[i:]
        summary = self._summary(out, rest, unseen) if with_summary else None
        cursor = cursor_factory(join_offset(i, resume)) if cursor_factory is not None else None
        return self.estimator.count(trailer_text(summary, cursor, len(rest) + unseen)) + 1  # 数え誤りの余裕 1 トークン

    def _fit(
        self,
        out: ToolOutput,
        records: Sequence[Record],
        start: int,
        n: int,
        m: int,
        costs: list[int],
        reserved: int,
        max_tokens: int,
        cursor_factory: Callable[[int], str] | None,
        *,
        unseen: int,
        with_summary: bool,
        need_first: int,
        resumes: Sequence[int | None] = (),
        trailer_text: TrailerText = _trailer_text,
    ) -> _Fit:
        """先頭 ``m`` 件を入れた形から、トレーラ込みで収まるまで末尾の 1 件ずつ（まとめて）外す。"""
        est = self.estimator.count
        while True:
            if m == 0:
                # 1 件も入らない。カーソルは出さない（BUG-0015）: オフセットが進まないカーソルは、同じ呼び出しをもう一度
                # するだけで、辿るクライアントが止まらない。カーソルは ``max_tokens`` を含まない引数で結ばれているので、
                # 呼び出しに付けたカーソルはそのまま使える。``max_tokens`` を上げて同じ引数で呼び直させる。
                # トレーラ（要約・``# more:``）も描かれないので数えない。
                warning = (
                    f"no record fits in max_tokens={max_tokens}: the next record needs ~{need_first} tokens;"
                    f" raise max_tokens to ~{reserved + need_first} or more and call again with the same arguments"
                )
                used = reserved + est("! " + warning + "\n")
                return _Fit(
                    m=0, summary=None, cursor=None, warnings=(warning,), used=used, over=used > max_tokens
                )
            # 最後に入れた 1 件が途中で切れているとき（``Record.resume``）、続きは同じレコードの再開位置から
            resume = resumes[m - 1] if m <= len(resumes) else None
            next_off = start + m - 1 if resume is not None else start + m
            rest = records[next_off:]
            summary = self._summary(out, rest, unseen) if with_summary else None
            cursor = cursor_factory(join_offset(next_off, resume)) if cursor_factory is not None else None
            used = reserved + sum(costs[:m]) + est(trailer_text(summary, cursor, len(rest) + unseen))
            if used <= max_tokens:
                return _Fit(m=m, summary=summary, cursor=cursor, warnings=(), used=used, over=False,
                            remaining=len(rest) + unseen if resume is not None else None)
            deficit = used - max_tokens
            freed = 0
            while m > 0 and freed < deficit:  # 少なくとも 1 件は外れる（deficit > 0）
                m -= 1
                freed += costs[m]
