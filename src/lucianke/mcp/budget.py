"""予算・ページネーション・カーソル（設計書 ADR-4・ADR-8、§7.2.1〜7.2.3、仕様書 §5.6・§5.7）。

* ``TokenEstimator``: 文字種別の係数による ``o200k_base`` 互換の近似。実体は ``core/tokens.py``（``syntax`` の骨格と
  共有するため ``core`` に置いた）。ここから同じ名前で再公開する（``TokenEstimator`` ・``TOKENS_PER_CHAR``）。
* ``Packer``: 優先順位つきのレコード列を ``max_tokens`` の中に詰める。レコードは途中で切らない。
  ``used_tokens_est <= max_tokens`` を保証するのはここである（ADR-4 の帰結）。
* ``CursorCodec``: ``next_cursor`` の発行と照合（ADR-8）。カーソルは短い鍵で、中身はセッションのメモリの表に持つ
  （BUG-0115）。不透明だが改ざん防止ではない。
* ``trailer_lines``: ``# summary:`` と ``# more:`` の組み立て。パッカーが見積もる文字列とレンダラが出す文字列を
  一致させるため、レンダラはこの関数を使うこと。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
from collections import OrderedDict
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
#: 再開位置（``Record.resume``）の上限の見込み。続きのカーソルの長さの上限を数えるのに使う（BUG-0110。行番号は 31 ビットに収まる）
_RESUME_MAX: Final = (1 << 31) - 1


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


def _raise_hint(need: int) -> str:
    return f"raise max_tokens to ~{need} or more and call again with the same arguments"


def no_fit_warning(max_tokens: int, need_first: int, need: int) -> str:
    """1 件も入らないときの警告（仕様書 §5.6-4。BUG-0015）。``need`` は呼び直しで少なくとも 1 件が返る ``max_tokens``。"""
    return f"no record fits in max_tokens={max_tokens}: the next record needs ~{need_first} tokens; {_raise_hint(need)}"


def preamble_warning(max_tokens: int, preamble: int, need: int) -> str:
    """前置きだけで予算を超えるときの警告（BUG-0064）。``need`` は呼び直しで応答が収まる ``max_tokens``。"""
    return f"the response preamble alone needs ~{preamble} tokens, over max_tokens={max_tokens}; {_raise_hint(need)}"


def _one_line(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- カーソル -------------------------------------------------------------------------------

_CURSOR_EXCLUDED_ARGS: Final = frozenset({"cursor", "max_tokens", "format"})
#: カーソルの表に持つ件数の上限（セッションのメモリ。古く使われていないものから捨てる。捨てたカーソルは ``E_CURSOR_EXPIRED``）
CURSOR_TABLE_MAX: Final = 4096
#: カーソルの文字列: ``c`` + 10 進 9 桁（compact の ``cursor=`` の後ろで o200k の 3 トークン。``c`` は JSON で数値と取り違えないため）
_CURSOR_TOKEN: Final = re.compile(r"c[0-9]{9}")
_CURSOR_SPACE: Final = 10**9
_CURSOR_SALTS: Final = 32  # 鍵がぶつかったときに引き直す回数の上限


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
    """``next_cursor`` の発行と照合（設計書 §7.2.3、ADR-8・ADR-16。BUG-0115）。

    カーソルの文字列は短い鍵（``c`` + 10 進 9 桁）で、中身の ``CursorState``（ツール名・``max_tokens`` ・``cursor`` ・
    ``format`` を除いた引数のハッシュ・オフセット・``data_version`` ・ワークスペース）はこのオブジェクトの表（メモリ。
    ``CURSOR_TABLE_MAX`` 件まで。古く使われていないものから捨てる）に持つ。鍵は中身のハッシュから決める（同じ中身なら同じ鍵。
    プロセスをまたいでも同じ）。表の別の中身とぶつかったときだけ塩を足して引き直す。
    以前は中身を ``base64url(json(...))`` にして渡していたので、カーソル 1 つが約 70 トークン（112 文字）あり、
    ``# more:`` 行が参照 3〜4 行ぶんになっていた（件数の少ない答えは compact で分けて読めなかった）。表はセッション限定の
    軽い対応表（ADR-16）で、再起動や捨てたカーソルは ``E_CURSOR_EXPIRED``（先頭から呼び直す）になる。

    ``peek`` は表に入れずに鍵だけを返す（パッカーが見積もりで作るカーソル用）。``encode`` は表に入れて鍵を返す
    （応答に出すカーソル）。どちらも同じ中身に同じ鍵を返す。
    """

    def __init__(self, capacity: int = CURSOR_TABLE_MAX) -> None:
        self.capacity = capacity
        self._table: OrderedDict[str, CursorState] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """表に持っている（発行して捨てていない）カーソルの数。"""
        return len(self._table)

    @staticmethod
    def hash_args(args: Mapping[str, Any]) -> str:
        """``cursor`` ・ ``max_tokens`` ・ ``format`` を除いた引数の正規化 JSON の SHA-1 先頭 12 桁。"""
        norm = {k: v for k, v in args.items() if k not in _CURSOR_EXCLUDED_ARGS}
        text = json.dumps(norm, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

    def _state(
        self, *, tool: str, args: Mapping[str, Any], offset: int, data_version: int, workspace_id: str | None
    ) -> CursorState:
        return CursorState(
            tool=tool, args_hash=self.hash_args(args), offset=offset, data_version=data_version, workspace_id=workspace_id
        )

    def _token_of(self, state: CursorState) -> str:
        """``state`` の鍵（呼び手がロックを持つ）。表の別の中身とぶつかれば塩を足して引き直す（``_CURSOR_SALTS`` 回まで。
        鍵の空間 10 億に対して表は 4,096 件なので、ぶつかるのは 25 万回に 1 回ほどで、引き直しが尽きることは事実上無い。尽きたら
        最後の候補を返し、``encode`` が持ち主を上書きする）。"""
        token = ""
        for salt in range(_CURSOR_SALTS):
            text = f"{state.tool}\0{state.args_hash}\0{state.offset}\0{state.data_version}\0{state.workspace_id}\0{salt}"
            digest = hashlib.sha256(text.encode("utf-8"), usedforsecurity=False).digest()
            token = f"c{int.from_bytes(digest[:8], 'big') % _CURSOR_SPACE:09d}"
            held = self._table.get(token)
            if held is None or held == state:
                break
        return token

    def peek(
        self, *, tool: str, args: Mapping[str, Any], offset: int, data_version: int, workspace_id: str | None
    ) -> str:
        """表に入れずに鍵を返す（``encode`` と同じ鍵）。"""
        state = self._state(tool=tool, args=args, offset=offset, data_version=data_version, workspace_id=workspace_id)
        with self._lock:
            return self._token_of(state)

    def encode(
        self, *, tool: str, args: Mapping[str, Any], offset: int, data_version: int, workspace_id: str | None
    ) -> str:
        """カーソルを発行する: 表に入れて鍵を返す。表が上限を超えたら、最も長く使われていないものから捨てる。"""
        state = self._state(tool=tool, args=args, offset=offset, data_version=data_version, workspace_id=workspace_id)
        with self._lock:
            token = self._token_of(state)
            self._table[token] = state
            self._table.move_to_end(token)
            while len(self._table) > self.capacity:
                self._table.popitem(last=False)
        return token

    def decode(
        self,
        token: str,
        *,
        tool: str,
        args: Mapping[str, Any],
        data_version: int,
        workspace_id: str | None,
    ) -> CursorState:
        """表から引いて検査する。

        鍵の形でない文字列、``tool`` ・引数・ワークスペースの不一致は ``E_INVALID_ARGUMENT``（``details.param == "cursor"``）。
        表に無い鍵（再起動・捨てた・発行していない）と、``data_version`` が今と違うものは ``E_CURSOR_EXPIRED``
        （``details.reason`` が ``unknown_cursor`` / ``data_changed``）。不一致を失効より先に検査する（別ツールのカーソルが
        「失効」と誤診されないように）。引いたカーソルは使われたものとして表の新しい側へ移す。
        """
        if not isinstance(token, str) or not _CURSOR_TOKEN.fullmatch(token):
            raise _invalid_cursor("malformed token (pass next_cursor exactly as returned, or call again without cursor)")
        with self._lock:
            state = self._table.get(token)
            if state is not None:
                self._table.move_to_end(token)
        if state is None:
            raise LuciankeError(
                ErrorCode.E_CURSOR_EXPIRED,
                f"the cursor is unknown or expired (this server session keeps its last {self.capacity} cursors in memory); "
                "call again without cursor from the first page",
                details={"reason": "unknown_cursor"},
            )
        if state.tool != tool or state.args_hash != self.hash_args(args) or state.workspace_id != workspace_id:
            raise _invalid_cursor("the cursor was issued for a different tool, arguments or workspace")
        if state.data_version != data_version:
            raise LuciankeError(
                ErrorCode.E_CURSOR_EXPIRED,
                "the data changed since the cursor was issued; call again from the first page",
                details={"reason": "data_changed"},
            )
        return state


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
    要約が押し出し、件数が減った（max_tokens を 2 上げて 11 件 → 1 件）。取り分はさらに「先頭の 1 件の最も小さい形（縮退版。
    無ければ丸ごと）と ``# more:`` 行」を押し出さない分までに抑える（BUG-0110）。1/4 を必ず取り分けていた間は、先頭の 1 件と
    カーソルは入るのに要約の分で 0 件になる予算の帯があった（compact の既定の形式で、全体より少し小さい予算ではページが
    進められなかった）。

    契約: ``reserved_tokens``（ヘッダ・``head_kv``・呼び手が出す警告の分）は呼び手が数えて渡す。前置きがページに入った
    レコードで変わる（降格の注意文はそのページの精度で出る）ときは、先頭から ``m`` 件のページの前置きを ``reserved_for(m)``
    で形ごとに渡す（BUG-0116。``mcp/fit.py``）。
    ``reserved_tokens`` とトレーラの最小形だけで ``max_tokens`` を超える（前置きだけで予算を使い切る小さすぎる
    ``max_tokens`` など）場合は保証できず、レコード 0 件で返す（``used_tokens_est`` は超過した実測値）。レコードが元から
    0 件でも、``warnings`` に呼び直しに要る ``max_tokens`` を名指しする（BUG-0064）。

    レコードが 1 件も入らないとき（仕様書 §5.6-4）は、レコード 0 件・``warnings`` に理由と必要な ``max_tokens`` の目安の
    成功応答で、``next_cursor`` は出さない（BUG-0015）。目安は先頭の 1 件を丸ごとと ``# more:`` 行（カーソル）が入る予算で、
    ``Page.need_max_tokens`` にも入れる（BUG-0110。描いた全文で確かめて上げるのは ``mcp/fit.py``）。オフセットが進まない
    カーソルは同じ呼び出しの繰り返しで、辿るクライアントが止まらない。カーソルは ``max_tokens`` を含まない引数で結ばれているので、呼び出しに付けたカーソルは
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
        min_need: int = 0,
        reserved_for: Callable[[int], int] | None = None,
    ) -> Page:
        """``form`` は応答の形式（``"json"`` なら縮退に ``Record.shrink_json`` を先に使う。``record_text`` は呼び手が合わせる）。

        1 件も入らない（前置きだけで超える）ページは、警告に呼び直しの目安の ``max_tokens`` を書き、``Page.need_max_tokens`` にも
        入れる。``min_need`` はその目安の下限（呼び手が描いた全文で確かめた値。``mcp/fit.py``。BUG-0110）。

        ``reserved_for(m)`` は、ページが先頭から ``m`` 件（``m >= 1``）を持つときの前置き（``m`` について減らない・予算に依らない
        こと）。前置きが入ったレコードで変わる（降格の注意文は入ったレコードの精度で出る）とき、呼び手が形ごとに渡す（BUG-0116）。
        省略すれば全部の ``m`` で ``reserved_tokens``。``reserved_tokens`` は 0 件のページ（1 件も入らない・詰めるものが無い）の
        前置き。鎖の各形は自分の件数の前置きで「収まる」を判定するので、前置きが形に依らないときと同じく予算について単調。
        """
        est = self.estimator.count
        # 形式ごとの本文の見積もり（既定は compact の行。json は ``Record.data`` の JSON。BUG-0017）
        rt = record_text or _record_text
        tt = trailer_text or _trailer_text
        records = out.records
        n = len(records)
        start, _ = split_offset(max(offset, 0))  # 上位ビットは同じレコードの再開位置（ツールが読む。``Record.resume``）

        def pre(m: int) -> int:
            """先頭から ``m`` 件（``m >= 1``）を入れたページの前置き。"""
            return reserved_for(m) if reserved_for is not None else reserved_tokens

        def avail_of(m: int) -> int:
            """先頭から ``m`` 件を入れたページで、レコードとトレーラに使える予算。"""
            return max_tokens - pre(m)

        # ``total_estimate`` が ``len(records)`` を超えるとき、ツールは先頭の ``n`` 件だけを渡し（``limit`` で打ち切った遅延列）、
        # 残り ``unseen`` 件を具体化していない。それも「続き」なので、渡された分が全部入ってもカーソルとトレーラが要る
        # （BUG-0008）。``total_estimate`` が ``len(records)`` 以下なら無視する。
        if out.total_estimate is None:
            unseen = 0
            total = max(start, n)
        else:
            unseen = max(0, out.total_estimate - n)
            total = n + unseen

        if start >= n:  # 詰めるものが無い（続きも無い）
            if reserved_tokens <= max_tokens:
                return self._page((), reserved_tokens, max_tokens, total, 0, None, None, ())
            # 前置きだけで予算を超える（BUG-0064）。レコードが元から無くても、1 件も入らないときと同じく呼び直しに
            # 要る ``max_tokens`` を名指しする（固定の下限は無いので、下限はこの実測値で呼び出しごとに伝える。BUG-0063）
            need = max(reserved_tokens, min_need)
            warning = preamble_warning(max_tokens, reserved_tokens, need)
            used = reserved_tokens + self.estimator.count("! " + warning + "\n")
            page = self._page((), used, max_tokens, total, 0, None, None, (warning,))
            return dataclasses.replace(page, need_max_tokens=need)

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
                if running > avail_of(n - start):
                    break
            else:
                kept = tuple(records[i].with_tokens(tok[i]) for i in range(start, n))
                return self._page(kept, pre(n - start) + running, max_tokens, total, 0, None, None, ())

        # 要約を数えない形式（json）では要約の取り分を使わない
        summary_counts = tt("x", None, 1) != tt(None, None, 1)

        def trailer_of(m: int, resume: int | None) -> tuple[int, int, int, int]:
            """先頭 ``m`` 件（最後の 1 件の再開位置が ``resume``）を入れたときの ``(次のオフセット, 残り件数, 素のトレーラ, 要約の分)``。

            素のトレーラは ``# more:`` 行（json はカーソルの文字列）、要約の分は要約の行を足したときに増える分（要約を数えない形式は 0）。
            続きが無ければ残り 0・どちらも 0。
            """
            last = start + m - 1
            if resume is not None:  # 途中で切れた版の続きは同じレコードの再開位置から（最後の 1 件でも続きがある）
                nxt, remaining = join_offset(last, resume), n - last + unseen
            else:
                nxt, remaining = last + 1, n - last - 1 + unseen
            if remaining <= 0:
                return nxt, 0, 0, 0
            cursor = cursor_factory(nxt) if cursor_factory is not None else None
            bare = est(tt(None, cursor, remaining))
            if not summary_counts:
                return nxt, remaining, bare, 0
            rest = records[last if resume is not None else last + 1 :]
            full = est(tt(self._summary(out, rest, unseen), cursor, remaining))
            return nxt, remaining, bare, max(0, full - bare)

        # 先頭の 1 件を丸ごと入れて ``# more:`` 行を付けた本文（要約なし）。これが入る予算なら、少なくとも 1 件は必ず返る
        first_bare = trailer_of(1, None)[2]
        first_need = cost_of(start) + first_bare

        def first_share(avail: int) -> int:
            """要約の取り分 ``min(share_full, max(0, avail - F))``（BUG-0067・BUG-0110）。``avail`` はその形で使える予算
            （前置きが形の件数で変わるとき〈BUG-0116〉は形ごとに違う）、``share_full`` は ``avail`` の 1/4。

            F は「先頭の 1 件の最も小さい形と ``# more:`` 行」が入るのに要る本文で、予算に依らない（丸ごとなら ``first_need``、
            縮退版なら ``最小の ask + 1 + 再開位置が最大のときの # more: 行``。縮退版は ``ask`` に収まる最大の版を返すので、縮退版が
            返る最小の ``ask`` が最も小さい形）。取り分は 1/4 まで、かつ先頭の 1 件の最も小さい形と ``# more:`` 行を押し出さない
            分まで: 要約は SHOULD なので、1 件も返らなくなるくらいなら要約を削る（以前は 1/4 を必ず取り分けたので、先頭の 1 件と
            ``# more:`` 行は入るのに 0 件で返る予算の帯があった）。どちらの上限も予算 1 トークンにつき高々 1 トークンしか増えない
            ので ``avail - 取り分`` は予算について減らず、鎖の各形の「収まる」と縮退版の大きさは予算について単調のまま。
            F は取り分が 1/4 より小さくなる範囲でだけ求める（縮退の呼び出しはふつう 1 回。帯の中でだけ二分探索する）。
            """
            share_full = max(0, avail) // SUMMARY_SHARE_DIVISOR
            whole = min(share_full, max(0, avail - first_need))
            rec = records[start]
            if whole == share_full or rec.shrink is None:
                return whole
            far = cursor_factory(join_offset(start, _RESUME_MAX)) if cursor_factory is not None else None
            bare_hi = max(first_bare, est(tt(None, far, n - start + unseen)))
            lo = avail - share_full - bare_hi - 1  # 最小の ask がこれ以下なら F ≤ avail - share_full（取り分は 1/4 のまま）
            if lo >= 1 and self._shrink(rec, lo, rt, form) is not None:
                return share_full
            lo, hi = max(1, lo + 1), avail - bare_hi - 1  # hi より大きい最小の ask は avail に入らない（F は丸ごとの側）
            if hi < lo or self._shrink(rec, hi, rt, form) is None:
                return whole
            while lo < hi:  # 縮退版が返る最小の ask（縮退版は ask について単調）
                mid = (lo + hi) // 2
                if self._shrink(rec, mid, rt, form) is None:
                    lo = mid + 1
                else:
                    hi = mid
            return min(share_full, max(0, avail - min(first_need, lo + 1 + bare_hi)))

        # 要約の取り分（使える予算ごと。要約の分がある形を初めて数えるときに 1 度だけ求める。縮退の呼び出しを増やさない。
        # 前置きが形に依らなければ 1 つ、件数で変わるときも高々その段の数）
        shares: dict[int, int] = {}

        def tail_of(m: int, resume: int | None) -> tuple[int, int, int]:
            """先頭 ``m`` 件を入れたときの ``(次のオフセット, 残り件数, トレーラ)``。トレーラは素のトレーラと要約の取り分までの分。"""
            nxt, remaining, bare, extra = trailer_of(m, resume)
            if not extra:
                return nxt, remaining, bare
            avail = avail_of(m)
            if avail not in shares:
                shares[avail] = first_share(avail)
            return nxt, remaining, bare + min(extra, shares[avail])

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
        #    最初に収まる m（丸ごと、無理なら最も大きい縮退版）を採る。本文の和は m について増え、使える予算（前置きの残り）は
        #    m について増えないので、それより先は収まらない
        prefix = [0]  # prefix[k] = 先頭 k 件を丸ごと入れた本文の和
        m_top = 0
        for m in range(1, n - start + 1):
            if prefix[-1] > avail_of(m):
                break
            m_top = m
            prefix.append(prefix[-1] + cost_of(start + m - 1))
        chosen: tuple[int, Record, int, int, int, int] | None = None  # (件数, 最後の版, そのトークン数, 次のオフセット, 残り, トレーラ)
        for m in range(m_top, 0, -1):
            i = start + m - 1
            room = avail_of(m) - prefix[m - 1]
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
            # トレーラ（要約・``# more:``）も描かれないので数えない。呼び直しの目安は、先頭の 1 件を丸ごとと ``# more:`` 行
            # （続きがあれば）が入る予算（その予算では要約の取り分は 0 なので必ず入る。以前は ``# more:`` 行を数えず、
            # 呼んだ ``max_tokens`` より小さい値を勧めることがあった。BUG-0110）
            #    残りが全部（トレーラなしで）それより小さく入るなら、そちらが目安（件数の少ない答えでは「1 件 + カーソル」より
            #    「全部」のほうが小さいことがある）。前置きはそれぞれの形の件数のもの（BUG-0116）
            need = pre(1) + first_need
            if not unseen:
                whole = pre(n - start)
                for i in range(start, n):
                    whole += cost_of(i)
                    if whole >= need:
                        break
                need = min(need, whole)
            need = max(need, min_need)
            warning = no_fit_warning(max_tokens, cost_of(start), need)
            used = reserved_tokens + est("! " + warning + "\n")
            page = self._page((), used, max_tokens, total, n - start + unseen, None, None, (warning,))
            return dataclasses.replace(page, need_max_tokens=need)

        m, last, last_size, nxt, remaining, _ = chosen
        kept = (*(records[j].with_tokens(cost_of(j)) for j in range(start, start + m - 1)), last.with_tokens(last_size))
        body = pre(m) + prefix[m - 1] + last_size
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
