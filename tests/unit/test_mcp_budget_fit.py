"""予算の掃引を本物のレンダラ（compact・json）とパイプラインの詰め（``mcp/fit.pack_and_render``）に通す性質テスト
（仕様書 §5.6-4・§5.6-5・§5.7、設計書 §7.2.2。BUG-0110）。

パッカー（``Packer``）だけ・json だけを見る試験では、compact の既定の形式で起きていた次の不具合が見えなかった:
全体より少し小さい ``max_tokens`` で 1 件も返らず、警告は呼んだ値より **小さい** ``max_tokens`` を勧めていた
（要約の取り分が先頭の 1 件とカーソルを押し出し、目安は ``# more:`` 行を数えていなかった）。

乱数のレコード列（大きさ・行数・日本語・group・縮退・途中で切れる版・精度の混在・具体化していない続き・ツールの要約・
``head_kv``・警告・``E_TIMEOUT`` の部分結果）を作り、``max_tokens`` を小さい値から全体の大きさまで上げながら描いて、
各応答と隣どうしで次を確かめる。

1. レコードを含む応答の全文は ``max_tokens`` 以下で、``used_tokens_est`` は全文の数え直しに一致する（json は ``timing_ms`` を
   最大の桁数で数えるので最大 5 トークン上。0 件の応答も申告は数え直しに一致する）。
2. ``next_cursor`` は「続きがあり 1 件以上入った」ときだけ出る。
3. 予算に対して単調（BUG-0067）: 件数は減らず、どのレコードも小さくならない。
4. 前置き（0 件のページを描いた全文）+ 先頭のレコード + ``# more:`` 行（カーソル。続きがあるとき）が入る予算なら、
   少なくとも 1 件が返る（要約は SHOULD なので、要約のために 0 件にしない）。
5. 1 件も入らない応答の「raise max_tokens to ~N」は、呼んだ ``max_tokens`` より大きく、N で呼び直すと警告なしに収まり
   1 件以上が返る（レコードが元から無い前置きだけの超過でも、N で応答が収まる）。
"""

from __future__ import annotations

import dataclasses
import random
import re
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from test_mcp_budget import SUMMARIZERS

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput, split_offset
from lucianke.core.tokens import TokenEstimator
from lucianke.mcp.budget import CursorCodec, Packer, trailer_lines
from lucianke.mcp.envelope import build_meta
from lucianke.mcp.fit import pack_and_render
from lucianke.mcp.pipeline import default_renderers
from lucianke.mcp.render import json_out
from lucianke.mcp.types import CallInfo, Page, RenderInput

EST = TokenEstimator().count
CODEC = CursorCodec()
RENDERERS = default_renderers()
RAISE = re.compile(r"raise max_tokens to ~(\d+) or more")
TOOL = "probe_tool"
WS = "ws1"
JSON_TIMING_SLACK = (
    5  # json は ``timing_ms`` を最大の桁数（5 桁）で数える（tests/unit/test_mcp_response_budget.py と同じ）
)

# --- 部品 -----------------------------------------------------------------------------------

_ALPHABET = "abcdefgh  ()._:/=日本語の関数"


def _text(rng: random.Random, chars: int) -> str:
    return "".join(rng.choices(_ALPHABET, k=chars)).strip() or "x"


def _prefix_shrink(base: Record) -> Callable[[int], Record | None]:
    """行の本文（終端の改行なし）が ``ask`` 以下に収まる最長の接頭辞の版（``data`` も同じ本文にする。json でも縮む）。"""
    full = "\n".join(base.lines)

    def shrink(ask: int) -> Record | None:
        lo, hi = 0, len(full) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if EST(full[:mid]) <= ask:
                lo = mid
            else:
                hi = mid - 1
        if lo <= 0:
            return None
        body = full[:lo]
        return dataclasses.replace(
            base, lines=tuple(body.split("\n")), data={**base.data, "text": body}, shrink=None
        )

    return shrink


def _rows_record(key: str, rows: int, conf: Confidence, group: str | None) -> Record:
    """1 行ずつの本文。収まらなければ先頭から詰め、切った行を ``resume`` に持つ（T-05 の形）。"""
    all_rows = [f"{i + 1:4d}  value_{i} = compute({i})" for i in range(rows)]

    def body(upto: int, resume: int | None) -> Record:
        return Record(
            kind="code", key=key, priority=0, lines=tuple(all_rows[:upto]),
            data={"key": key, "text": "\n".join(all_rows[:upto]), "resume": resume}, confidence=conf, group=group,
            shrink=shrink if resume is None else None, resume=resume,
        )  # fmt: skip

    def shrink(ask: int) -> Record | None:
        best = None
        for upto in range(1, rows):
            cand = body(upto, upto)
            if EST("\n".join(cand.lines)) > ask:
                break
            best = cand
        return best

    return body(rows, None)


def _records(rng: random.Random, n: int) -> list[Record]:
    groups = ["src/a.py", "src/b.py", "lib/日本語.py", None]
    confs = [Confidence.EXACT, Confidence.EXACT, Confidence.STRUCTURAL, Confidence.HEURISTIC]
    out: list[Record] = []
    for i in range(n):
        conf, group = rng.choice(confs), rng.choice(groups)
        if rng.random() < 0.1:
            out.append(_rows_record(f"r{i}", rng.randint(2, 30), conf, group))
            continue
        big = rng.random() < 0.15
        lines = tuple(_text(rng, rng.randint(4, 400 if big else 60)) for _ in range(rng.randint(1, 3)))
        r = Record(kind="ref", key=f"r{i}", priority=i, lines=lines, data={"key": f"r{i}", "text": "\n".join(lines)},
                   confidence=conf, group=group)  # fmt: skip
        if rng.random() < 0.3:
            r = dataclasses.replace(r, shrink=_prefix_shrink(r))
        out.append(r)
    return out


def _output(rng: random.Random, n: int) -> ToolOutput:
    records = _records(rng, n)
    extras = rng.random() < 0.4
    return ToolOutput(
        records=records,
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="pyright+tree-sitter", confidence=Confidence.EXACT, lsp_status="ready"),
        page_unit="1 record",
        head_kv=[("target", "lk://ws1/src/a.py#Service"), ("rows", str(n))] if extras else (),
        data_static={"target": "lk://ws1/src/a.py#Service", "totals": {"items": n}} if extras else {},
        summarize=rng.choice(SUMMARIZERS),
        total_estimate=rng.choice([None, None, n + rng.randint(1, 40)]),
        warnings=("引数 x は無視されました",) if extras and rng.random() < 0.5 else (),
    )


@dataclasses.dataclass(frozen=True)
class Case:
    fmt: str
    out: ToolOutput
    offset: int
    error: LuciankeError | None
    seed: int

    def factory(self) -> Callable[[int], str]:
        args = {"seed": self.seed}
        return lambda offset: CODEC.encode(
            tool=TOOL, args=args, offset=offset, data_version=3, workspace_id=WS
        )

    def call(self, max_tokens: int) -> CallInfo:
        return CallInfo(
            tool=TOOL, revision="WORKING_TREE", workspace_id=WS, format=self.fmt, max_tokens=max_tokens
        )


@dataclasses.dataclass(frozen=True)
class Reply:
    max_tokens: int
    text: str
    page: Page
    recount: int

    @property
    def raise_to(self) -> int | None:
        m = RAISE.search(self.text)
        return int(m.group(1)) if m else None


def _fit(case: Case, max_tokens: int) -> Reply:
    fitted = pack_and_render(
        packer=Packer(), renderer=RENDERERS[case.fmt], fmt=case.fmt, out=case.out, call=case.call(max_tokens),
        offset=case.offset, cursor_factory=case.factory(), warnings=(), records_key="items", timing_ms=lambda: 7,
        error=case.error,
    )  # fmt: skip
    return Reply(max_tokens, fitted.rendered.text, fitted.page, EST(fitted.rendered.text))


def _preamble(case: Case, max_tokens: int) -> int:
    """0 件のページを同じレンダラで描いた全文（``used_tokens_est`` は ``max_tokens``・``timing_ms`` は最大の桁数）。"""
    total = max(case.out.total_estimate or 0, len(case.out.records))
    page = Page(records=(), next_cursor=None, summary=None, total_estimate=total, remaining=0,
                used_tokens_est=max_tokens, max_tokens=max_tokens, truncated=False)  # fmt: skip
    call = dataclasses.replace(case.call(max_tokens), timing_ms=99_999)
    inp = RenderInput(call=call, meta=build_meta(case.out, None), out=case.out, page=page, error=case.error,
                      warnings=(), records_key="items")  # fmt: skip
    return EST(RENDERERS[case.fmt](inp).text)


def _first_page_need(case: Case) -> int:
    """1 件以上を返すのに要る本文（前置きを除く）: 先頭のレコードを丸ごとと ``# more:`` 行（続きがあるとき。要約なし）か、
    具体化していない続きが無ければ残りの全部（トレーラなし）の小さいほう。パッカーと同じ形式の数え方。"""
    records = case.out.records
    start, _ = split_offset(case.offset)
    unseen = max(0, (case.out.total_estimate or 0) - len(records))
    remaining = len(records) - start - 1 + unseen
    cursor = case.factory()(start + 1)
    if case.fmt == "json":
        body: Callable[[Record], int] = lambda r: EST(json_out.record_text(r))  # noqa: E731
        more = EST(json_out.trailer_text(None, cursor, remaining)) if remaining else 0
    else:
        body = lambda r: EST("\n".join(r.lines) + "\n")  # noqa: E731
        more = EST("\n".join(trailer_lines(None, cursor, remaining)) + "\n") if remaining else 0
    need = body(records[start]) + more
    return need if unseen else min(need, sum(body(r) for r in records[start:]))


def _used(reply: Reply) -> int:
    return reply.page.used_tokens_est


def _check_reply(case: Case, r: Reply, ctx: Any) -> None:
    """1・2・5 の 1 応答ぶん。"""
    slack = JSON_TIMING_SLACK if case.fmt == "json" else 0
    assert r.recount <= _used(r) <= r.recount + slack, (ctx, _used(r), r.recount)
    if r.page.records:
        assert _used(r) <= r.max_tokens, (ctx, _used(r))
        assert r.raise_to is None, ctx
    assert (r.page.next_cursor is not None) is (r.page.truncated and bool(r.page.records)), ctx
    if r.page.next_cursor is not None:
        assert f"cursor={r.page.next_cursor}" in r.text or f'"{r.page.next_cursor}"' in r.text, ctx
    need = r.raise_to  # 呼ぶ側の AI が読む目安（描いた全文から読む。``Page.need_max_tokens`` との一致は test_mcp_budget で見る）
    if need is None:
        return
    # 1 件も入らない（前置きだけで超える）: 目安は呼んだ値より大きく、その値で呼び直せば警告なしに収まる
    assert not r.page.records and r.page.next_cursor is None, ctx
    assert need > r.max_tokens, (ctx, need, r.text)
    again = _fit(case, need)
    assert again.raise_to is None, (ctx, need, again.text)
    assert again.recount <= need, (ctx, need, again.recount)
    start, _ = split_offset(case.offset)
    if start < len(case.out.records):
        assert again.page.records, (ctx, need, again.text)


def _assert_no_less_information(lower: Reply, upper: Reply, ctx: Any) -> None:
    """3: ``upper``（大きい予算）は ``lower`` のレコードを全部、同じか大きい版で含む。"""
    a_recs, b_recs = lower.page.records, upper.page.records
    assert len(b_recs) >= len(a_recs), (ctx, lower.max_tokens, upper.max_tokens, len(a_recs), len(b_recs))
    for a, b in zip(a_recs, b_recs, strict=False):
        assert a.key == b.key, ctx
        assert (b.tokens or 0) >= (a.tokens or 0), (ctx, a.key, lower.max_tokens, a.tokens, b.tokens)
        if a.resume is not None and b.resume is not None:
            assert b.resume >= a.resume, (ctx, a.key, a.resume, b.resume)


def _sweep(case: Case, budgets: Sequence[int], ctx: Any) -> list[Reply]:
    replies: list[Reply] = []
    start, _ = split_offset(case.offset)
    first_need = _first_page_need(case) if start < len(case.out.records) else None
    for b in budgets:
        r = _fit(case, b)
        _check_reply(case, r, (ctx, b))
        if first_need is not None and b >= _preamble(case, b) + first_need:
            # 4: 前置き + 先頭の 1 件 + ``# more:`` 行が入る予算なら 1 件以上（要約のために 0 件にしない）
            assert r.page.records, (ctx, b, _preamble(case, b), first_need, r.text)
        if replies:
            _assert_no_less_information(replies[-1], r, (ctx, b))
        replies.append(r)
    return replies


def _case(rng: random.Random, fmt: str, seed: int) -> Case:
    n = rng.randint(1, 12)
    out = _output(rng, n)
    error = None
    if rng.random() < 0.15:
        error = LuciankeError(ErrorCode.E_TIMEOUT, "LSP did not answer in time", partial=out)
    offset = rng.choice([0, 0, rng.randint(0, n - 1)])
    return Case(fmt=fmt, out=out, offset=offset, error=error, seed=seed)


# --- 性質 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["compact", "json"])
def test_budget_sweep_through_the_renderers_keeps_every_invariant(fmt: str) -> None:
    """乱数のレコード列で ``max_tokens`` を小さい値から全体の大きさまで上げ、1〜5 を確かめる。"""
    rng = random.Random(20261031 + (fmt == "json"))
    paged = zero = 0
    for seed in range(14):
        case = _case(rng, fmt, seed)
        full = _fit(case, 200_000).recount
        lo = max(10, _preamble(case, 10) - 20)
        step = max(1, (full + 40 - lo) // 90)
        replies = _sweep(case, range(lo, full + 40, step), (fmt, seed))
        paged += any(r.page.next_cursor for r in replies)
        zero += any(r.raise_to is not None for r in replies)
    assert paged > 5 and zero > 5  # 続きのあるページと 1 件も入らない応答の両方を通っている


@pytest.mark.parametrize("fmt", ["compact", "json"])
def test_step_one_sweep_around_the_first_record_threshold(fmt: str) -> None:
    """先頭の 1 件が入りはじめる境目のまわりを 1 刻みで: 境目より下は 0 件で目安は境目以上、境目から上は 1 件以上。
    以前（compact）は境目の上に要約の取り分の幅だけ 0 件の帯があり、目安は ``# more:`` 行のぶん小さかった。"""
    rng = random.Random(110 + (fmt == "json"))
    for seed in range(10):
        case = dataclasses.replace(_case(rng, fmt, seed), offset=0, error=None)
        threshold = _preamble(case, 10) + _first_page_need(case)
        replies = _sweep(case, range(max(10, threshold - 40), threshold + 50), (fmt, seed))
        first = next(r.max_tokens for r in replies if r.page.records)
        assert first <= _preamble(case, first) + _first_page_need(case), (fmt, seed, first)
        for r in replies:
            if r.raise_to is not None:
                assert first <= r.raise_to, (fmt, seed, first, r.raise_to)


def test_ticket_shape_compact_pages_just_below_the_whole_response() -> None:
    """票 BUG-0110 の形: 全体の 1/2 ほどが前置き、レコードは小さく（約 20 トークン）、カーソルは長い（約 70 トークン）。
    全体より少し小さい予算では、0 件ではなく先頭の 1 件とカーソルが返る。以前は前置き + 22 から全体の手前まで 0 件で、
    「raise max_tokens to ~（前置き + 22）」と呼んだ値より小さい値を勧めていた。"""
    lines = [f"pkg/b.py:{7 + 4 * i}:21  [call] in caller_{i}  return packer.pack([{i}])" for i in range(4)]
    records = [Record(kind="ref", key=f"k{i}", priority=i, lines=(ln,), data={"line": ln}, confidence=Confidence.EXACT,
                      group="pkg/b.py") for i, ln in enumerate(lines)]  # fmt: skip
    out = ToolOutput(
        records=records,
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="tree-sitter", confidence=Confidence.EXACT, lsp_status="ready"),
        page_unit="1 reference site",
        head_kv=[("target", "lk://nm/pkg/a.py#Packer.pack"), ("references", "1 files, 4 sites (tests: 0)")]
        * 3,
    )
    case = Case(fmt="compact", out=out, offset=0, error=None, seed=0)
    full = _fit(case, 10_000)
    assert len(full.page.records) == 4
    replies = _sweep(case, range(full.recount - 120, full.recount + 1), "ticket")
    preamble = _preamble(case, full.recount)
    first = _first_page_need(case)
    for r in replies:
        if r.max_tokens >= preamble + first:
            assert r.page.records and (r.page.next_cursor or len(r.page.records) == 4), (r.max_tokens, r.text)
        else:
            assert r.raise_to is not None and r.raise_to > r.max_tokens, (r.max_tokens, r.text)
    assert any(r.page.next_cursor for r in replies)  # 全体の手前でカーソルつきのページが返っている
