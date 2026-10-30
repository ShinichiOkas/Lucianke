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
RESUME_RETRIES: Final = 3  # 途中で切れた縮退版の続きのトレーラが見込みより長いとき、見込みを上げて縮め直す回数の上限
#: ``# summary:`` 行に取り分ける上限（レコードに使える予算の 1/N）。要約がこれより長ければ「…」で切って収める。上限が予算に
#: 比例する（予算 1 トークンにつき高々 1 トークンしか増えない）ので、予算を上げてもレコードの取り分は減らない（BUG-0067）
SUMMARY_SHARE_DIVISOR: Final = 4


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


class Packer:
    """レコード列を予算内に詰める（設計書 §7.2.2、仕様書 §5.6）。

    詰め方（仕様書 §5.6-1・BUG-0067）: 先頭から丸ごと詰め、丸ごと入らない最初のレコードだけを縮退させて（``Record.shrink``）
    そのページの最後の 1 件にする。縮退版・途中で切れた版（``Record.resume``）の後ろには詰めない。ページは、予算に依らない
    固定の鎖「1 件目の縮退版（浅い段ほど後）→ 1 件目を丸ごと → 2 件目の縮退版 → 2 件目を丸ごと → …」のうち、トレーラ込みで
    予算に収まる最も先の形である。鎖が予算に依らないので、``max_tokens`` を上げても返るレコードは減らず、どのレコードも
    小さくならない（単調。``tests/unit/test_mcp_budget_monotone.py``）。以前は縮退版の後ろにも詰めていたため、予算を上げて
    先頭が丸ごと入ると後ろが次ページに回り、返る件数が減った。

    要約（``# summary:``。仕様書 §5.4.2）は ``# more:`` 行と一緒に取り分けるが、取り分けはレコードに使える予算の
    1/``SUMMARY_SHARE_DIVISOR`` までで、それより長い要約は「…」で切って収める（ふつうの予算では要約は丸ごと入る）。
    取り分けの上限が予算に比例するので、予算を上げてもレコードの取り分は減らない。以前は要約の分を丸ごと先に取り分け、
    1 件も入らないときだけ要約を省いていたので、要約つきで 1 件入る予算に上げると、要約なしで入っていた後ろのレコードを
    要約が押し出し、件数が減った（max_tokens を 2 上げて 11 件 → 1 件）。

    契約: ``reserved_tokens``（ヘッダ・``head_kv``・呼び手が出す警告の分）は呼び手が数えて渡す。
    ``reserved_tokens`` とトレーラの最小形だけで ``max_tokens`` を超える（前置きだけで予算を使い切る小さすぎる
    ``max_tokens`` など）場合は保証できず、レコード 0 件で返す（``used_tokens_est`` は超過した実測値）。レコードが元から
    0 件でも、``warnings`` に呼び直しに要る ``max_tokens`` を名指しする（BUG-0064）。

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
        form: str = "compact",
    ) -> Page:
        """``form`` は応答の形式（``"json"`` なら縮退に ``Record.shrink_json`` を先に使う。``record_text`` は呼び手が合わせる）。"""
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
            if reserved_tokens <= max_tokens:
                return self._page((), reserved_tokens, max_tokens, total, 0, None, None, ())
            # 前置きだけで予算を超える（BUG-0064）。レコードが元から無くても、1 件も入らないときと同じく呼び直しに
            # 要る ``max_tokens`` を名指しする（固定の下限は無いので、下限はこの実測値で呼び出しごとに伝える。BUG-0063）
            warning = (
                f"the response preamble alone needs ~{reserved_tokens} tokens, over max_tokens={max_tokens};"
                f" raise max_tokens to ~{reserved_tokens} or more and call again with the same arguments"
            )
            used = reserved_tokens + self.estimator.count("! " + warning + "\n")
            return self._page((), used, max_tokens, total, 0, None, None, (warning,))

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

        # 要約の取り分の上限（予算 1 トークンにつき高々 1 トークンしか増えない）。要約を数えない形式（json）では使わない
        share = max(0, avail) // SUMMARY_SHARE_DIVISOR
        summary_counts = tt("x", None, 1) != tt(None, None, 1)

        def tail_of(m: int, resume: int | None) -> tuple[int, int, int]:
            """先頭 ``m`` 件（最後の 1 件の再開位置が ``resume``）を入れたときの ``(次のオフセット, 残り件数, トレーラ)``。

            トレーラは ``# more:`` 行（json はカーソルの文字列）と、要約の行の ``share`` までの分。続きが無ければ残り 0・トレーラ 0。
            """
            last = start + m - 1
            if resume is not None:  # 途中で切れた版の続きは同じレコードの再開位置から（最後の 1 件でも続きがある）
                nxt, remaining = join_offset(last, resume), n - last + unseen
            else:
                nxt, remaining = last + 1, n - last - 1 + unseen
            if remaining <= 0:
                return nxt, 0, 0
            cursor = cursor_factory(nxt) if cursor_factory is not None else None
            bare = est(tt(None, cursor, remaining))
            if not summary_counts:
                return nxt, remaining, bare
            rest = records[last if resume is not None else last + 1 :]
            full = est(tt(self._summary(out, rest, unseen), cursor, remaining))
            return nxt, remaining, bare + min(max(0, full - bare), share)

        def shrunk_last(m: int, room: int, tail: int) -> tuple[Record, int, int, int, int] | None:
            """``m`` 件目を縮退させて最後の 1 件にする: 残り ``room`` にトレーラ込みで入る最も大きい縮退版。

            ``(縮退版, そのトークン数, 次のオフセット, 残り件数, トレーラ)``。途中で切れた版は続きのカーソルが再開位置の分だけ
            長いので、実際の形で数え直し、入らなければ見込みを上げて縮め直す（最大 ``RESUME_RETRIES`` 回）。
            """
            rec = records[start + m - 1]
            if rec.shrink is None:
                return None
            for _ in range(RESUME_RETRIES):
                # 縮退は行の本文（終端の改行なし）で測って収める。終端の改行は最大 1 トークンなので、その分を引いて渡す
                ask = room - tail - 1
                if ask < 1:
                    return None
                small = self._shrink(rec, ask, rt, form)
                if small is None:
                    return None
                size = est(rt(small))
                nxt, remaining, real = tail_of(m, small.resume)
                if size + real <= room:
                    return small, size, nxt, remaining, real
                if small.resume is None:
                    return None  # 渡した予算に収まらない縮退版（``Record.shrink`` の約束違反）。縮め直さずに止める
                tail = max(real, tail + 1)
            return None

        # 2. 鎖の上で予算に収まる最も先の形を探す。先頭 m - 1 件を丸ごと入れた本文が予算を超えない最大の m から下へ見て、
        #    最初に収まる m（丸ごと、無理なら最も大きい縮退版）を採る。本文の和は m について増えるので、それより先は収まらない
        prefix = [0]  # prefix[k] = 先頭 k 件を丸ごと入れた本文の和
        m_top = 0
        for m in range(1, n - start + 1):
            if prefix[-1] > avail:
                break
            m_top = m
            prefix.append(prefix[-1] + cost_of(start + m - 1))
        chosen: tuple[int, Record, int, int, int, int] | None = None  # (件数, 最後の版, そのトークン数, 次のオフセット, 残り, トレーラ)
        for m in range(m_top, 0, -1):
            i = start + m - 1
            room = avail - prefix[m - 1]
            nxt, remaining, tail = tail_of(m, None)
            if cost_of(i) + tail <= room:
                chosen = (m, records[i], cost_of(i), nxt, remaining, tail)
                break
            small = shrunk_last(m, room, tail)
            if small is not None:
                chosen = (m, *small)
                break

        if chosen is None:
            # 1 件も入らない。カーソルは出さない（BUG-0015）: オフセットが進まないカーソルは、同じ呼び出しをもう一度
            # するだけで、辿るクライアントが止まらない。カーソルは ``max_tokens`` を含まない引数で結ばれているので、
            # 呼び出しに付けたカーソルはそのまま使える。``max_tokens`` を上げて同じ引数で呼び直させる。
            # トレーラ（要約・``# more:``）も描かれないので数えない。
            need_first = cost_of(start)
            warning = (
                f"no record fits in max_tokens={max_tokens}: the next record needs ~{need_first} tokens;"
                f" raise max_tokens to ~{reserved_tokens + need_first} or more and call again with the same arguments"
            )
            used = reserved_tokens + est("! " + warning + "\n")
            return self._page((), used, max_tokens, total, n - start + unseen, None, None, (warning,))

        m, last, last_size, nxt, remaining, _ = chosen
        kept = (*(records[j].with_tokens(cost_of(j)) for j in range(start, start + m - 1)), last.with_tokens(last_size))
        body = reserved_tokens + prefix[m - 1] + last_size
        if not remaining:  # 最後のレコードまで入った（最後の 1 件は縮退版でもよい）。トレーラは要らない
            return self._page(kept, body, max_tokens, total, 0, None, None, ())
        # 3. 続きがある。カーソル（MUST）と要約の取り分は収まっている。要約は残りに入るだけ（長ければ「…」で切って）付ける
        cursor = cursor_factory(nxt) if cursor_factory is not None else None
        rest = records[start + m - 1 if last.resume is not None else start + m :]
        summary = self._fit_summary(self._summary(out, rest, unseen), cursor, remaining, max_tokens - body, tt)
        used = body + est(tt(summary, cursor, remaining))
        return self._page(kept, used, max_tokens, total, remaining, cursor, summary, ())

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

    def _shrink(self, rec: Record, ask: int, record_text: RecordText, form: str = "compact") -> Record | None:
        """``rec.shrink(ask)``（``ask`` は縮退版が収まるべき行の本文のトークン数）。

        json（``form == "json"``）で ``rec.shrink_json`` があれば、先にそれで ``ask + 1`` に収まる版を求め、実際に数えて
        収まればそれを返す（版ごとに JSON を作って数える二分探索を省く。BUG-0078）。``None``（最も小さい版も収まらない）なら
        ``None``。返った版が実際には収まらなければ（見積もりの外れ）下の探索に戻る。

        ``Record.shrink`` は compact の行の大きさで収める約束である。compact（既定）はそのまま 1 回呼ぶ。別の形式で数えるときは
        （json は ``Record.data`` の JSON）、返った縮退版をその形式で数えて ``ask + 1`` に収まればそのまま返す。収まらなければ、
        json の大きさは compact の行に比例しない（記号・メタ・エスケープの固定分がある）ので、``ask`` を二分探索して収まる最大の
        縮退版を探す（縮退版の大きさは ``ask`` に対して単調。最大 ``SHRINK_RETRIES`` 回呼ぶ）。収まるものが無ければ ``None``。
        """
        if form == "json" and rec.shrink_json is not None:
            fast = rec.shrink_json(ask + 1)
            if fast is None:
                return None  # 最も小さい版も json では収まらない（``Record.shrink_json`` の約束）
            if self.estimator.count(record_text(fast)) <= ask + 1:
                return fast
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

    def _fit_summary(
        self, summary: str, cursor: str | None, remaining: int, room: int, trailer_text: TrailerText
    ) -> str | None:
        """``room``（レコードを詰めた残り）にトレーラ込みで入る要約。丸ごと入らなければ末尾を「…」で切った最長の形。
        それも入らなければ ``None``（``# more:`` 行だけは取り分けてあるので必ず入る）。"""
        est = self.estimator.count
        if est(trailer_text(summary, cursor, remaining)) <= room:
            return summary
        lo, hi, best = 1, len(summary) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = summary[:mid].rstrip() + "…"
            if est(trailer_text(cand, cursor, remaining)) <= room:
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
