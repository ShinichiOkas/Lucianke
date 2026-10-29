"""パッカーと遅延列（``ToolOutput.total_estimate`` が ``len(records)`` より大きいとき）のカーソル（BUG-0008）。

ツールが ``limit`` などで途中までしかレコードを渡さない（具体化しない）とき、``total_estimate`` が実際の総数である。
渡さなかった分も「続き」なので、全部入っても ``next_cursor`` と ``remaining`` を出さなければならない。
次のページはツールが ``offset + limit`` 件までを渡し直し、パッカーが ``offset`` から詰める（T-03 の方式）。
"""

from __future__ import annotations

import random
from collections.abc import Sequence

import pytest

from lucianke.core.limits import MIN_MAX_TOKENS
from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput
from lucianke.mcp.budget import CursorCodec, Packer, TokenEstimator, default_summary
from lucianke.mcp.types import Page

EST = TokenEstimator()
CODEC = CursorCodec()
ARGS = {"q": "pay"}


def rec(key: str, *, group: str | None = None, size: int = 1) -> Record:
    """1 行のレコード。``size`` が大きいほどトークンが増える。"""
    return Record(
        kind="symbol",
        key=key,
        priority=0,
        lines=(f"[Method] src/{key}.py#{key} " + "x " * (size - 1),),
        data={"k": key},
        confidence=Confidence.STRUCTURAL,
        group=group,
    )


def make_out(records: Sequence[Record], total: int | None) -> ToolOutput:
    return ToolOutput(
        records=records,
        max_confidence=Confidence.STRUCTURAL,
        engine=EngineChoice(engine="tree-sitter", confidence=Confidence.STRUCTURAL),
        page_unit="symbol",
        total_estimate=total,
    )


def factory(offset: int) -> str:
    return CODEC.encode(tool="t03", args=ARGS, offset=offset, data_version=3, workspace_id="ws")


def offset_of(token: str) -> int:
    return CODEC.decode(token, tool="t03", args=ARGS, data_version=3, workspace_id="ws").offset


def tool_window(everything: Sequence[Record], offset: int, limit: int) -> ToolOutput:
    """T-03 の方式: 先頭から ``offset + limit`` 件までを渡し、総数を ``total_estimate`` に入れる。"""
    return make_out(everything[: offset + limit], len(everything))


def pack(out: ToolOutput, *, max_tokens: int = 4000, offset: int = 0, reserved: int = 0) -> Page:
    return Packer().pack(
        out, max_tokens=max_tokens, offset=offset, reserved_tokens=reserved, cursor_factory=factory
    )


# --- 単一ページ -----------------------------------------------------------------------------


def test_lazy_window_with_larger_total_issues_cursor_even_when_all_records_fit() -> None:
    recs = [rec(f"r{i}") for i in range(5)]
    page = pack(make_out(recs, 12))
    assert [r.key for r in page.records] == [f"r{i}" for i in range(5)]  # 渡された分は全部入る
    assert page.next_cursor is not None
    assert offset_of(page.next_cursor) == 5  # 次は具体化していない先頭
    assert page.remaining == 7
    assert page.truncated is True
    assert page.total_estimate == 12
    assert page.used_tokens_est <= 4000


def test_lazy_window_cursor_resumes_after_the_budget_cut_not_after_the_window() -> None:
    recs = [rec(f"r{i}", size=20) for i in range(10)]
    page = pack(make_out(recs, 50), max_tokens=120)
    assert 0 < len(page.records) < 10  # 予算で切れた
    assert page.next_cursor is not None
    assert offset_of(page.next_cursor) == len(page.records)
    assert page.remaining == 50 - len(page.records)
    assert page.used_tokens_est <= 120


def test_lazy_window_total_equal_to_records_has_no_cursor() -> None:
    recs = [rec(f"r{i}") for i in range(5)]
    page = pack(make_out(recs, 5))
    assert page.next_cursor is None and page.remaining == 0 and page.truncated is False
    assert page.total_estimate == 5


def test_total_estimate_smaller_than_records_is_ignored() -> None:
    recs = [rec(f"r{i}") for i in range(5)]
    page = pack(make_out(recs, 2))
    assert len(page.records) == 5 and page.next_cursor is None and page.remaining == 0
    assert page.total_estimate == 5


def test_without_total_estimate_nothing_changes() -> None:
    recs = [rec(f"r{i}") for i in range(5)]
    page = pack(make_out(recs, None))
    assert len(page.records) == 5 and page.next_cursor is None and page.remaining == 0
    assert page.total_estimate == 5


def test_lazy_window_offset_page_counts_remaining_from_the_total() -> None:
    everything = [rec(f"r{i}") for i in range(10)]
    page = pack(tool_window(everything, 4, 3), offset=4)
    assert [r.key for r in page.records] == ["r4", "r5", "r6"]
    assert page.next_cursor is not None and offset_of(page.next_cursor) == 7
    assert page.remaining == 3
    assert page.total_estimate == 10


def test_lazy_window_last_page_has_no_cursor() -> None:
    everything = [rec(f"r{i}") for i in range(10)]
    page = pack(tool_window(everything, 9, 3), offset=9)
    assert [r.key for r in page.records] == ["r9"]
    assert page.next_cursor is None and page.remaining == 0 and page.truncated is False


def test_lazy_window_without_cursor_factory_is_truncated_without_cursor() -> None:
    page = Packer().pack(make_out([rec("a"), rec("b")], 9), max_tokens=4000)
    assert page.next_cursor is None and page.truncated is True and page.remaining == 7


def test_lazy_window_offset_past_the_window_is_empty_without_cursor() -> None:
    """ツールが offset まで具体化していない（ツール側の誤り）。進めないカーソルは出さない（無限ループを避ける）。"""
    page = pack(make_out([rec("a"), rec("b")], 9), offset=5)
    assert page.records == () and page.next_cursor is None and page.truncated is False


def test_lazy_window_summary_counts_the_unseen_records() -> None:
    recs = [rec(f"r{i}", group="a.py") for i in range(3)]
    page = pack(make_out(recs, 10))
    assert page.summary == "7 more"  # 渡されなかった分は group が分からない（入った 3 件は残りに数えない）
    page = pack(make_out(recs, 3))
    assert page.summary is None


def test_lazy_window_with_nothing_but_unseen_records_still_counts_them() -> None:
    page = pack(make_out([rec("a")], 2))
    assert page.summary == "1 more" and page.remaining == 1


def test_default_summary_adds_unseen_to_others() -> None:
    rest = [rec("a", group="x.py"), rec("b", group="x.py"), rec("c")]
    assert default_summary(rest) == "3 more (x.py: 2, others: 1)"  # 従来どおり
    assert default_summary(rest, unseen=4) == "7 more (x.py: 2, others: 5)"
    assert default_summary([], unseen=4) == "4 more"


def test_lazy_window_trailer_is_counted_even_when_the_records_fit_exactly() -> None:
    """トレーラ無しなら全部入る予算でも、続きがあればトレーラの分を空けて末尾を外す。"""
    recs = [rec(f"r{i}", size=6) for i in range(8)]
    full = pack(make_out(recs, 8), max_tokens=10_000).used_tokens_est  # 続き無し: 全部入る
    page = pack(make_out(recs, 20), max_tokens=full)  # 同じ予算で、続きあり
    assert page.used_tokens_est <= full
    assert page.next_cursor is not None
    assert len(page.records) < 8  # トレーラの分だけ減った
    assert offset_of(page.next_cursor) == len(page.records)


def test_lazy_window_budget_never_exceeded_across_budgets() -> None:
    recs = [rec(f"r{i}", size=3 + i % 5, group=f"f{i % 3}.py") for i in range(12)]
    for max_tokens in range(MIN_MAX_TOKENS, 400, 7):
        page = pack(make_out(recs, 40), max_tokens=max_tokens, reserved=20)
        assert page.used_tokens_est <= max_tokens, max_tokens
        assert page.next_cursor is not None
        assert page.remaining == 40 - len(page.records)
        assert offset_of(page.next_cursor) == len(page.records)


# --- ページを辿る ---------------------------------------------------------------------------


def walk(everything: Sequence[Record], *, limit: int, max_tokens: int, reserved: int = 30) -> list[Page]:
    pages: list[Page] = []
    offset = 0
    for _ in range(len(everything) * 2 + 5):
        page = pack(
            tool_window(everything, offset, limit), max_tokens=max_tokens, offset=offset, reserved=reserved
        )
        pages.append(page)
        assert page.used_tokens_est <= max_tokens
        assert len(page.records) <= limit  # 1 ページは limit 件まで（T-03 の事後条件）
        if page.next_cursor is None:
            return pages
        assert page.records, "進まないページは無い"
        offset = offset_of(page.next_cursor)
        assert offset == sum(len(p.records) for p in pages)
    pytest.fail("did not terminate")


def test_walk_pages_by_limit_has_no_duplicates_and_no_gaps() -> None:
    everything = [rec(f"r{i:03d}", group=f"f{i // 5}.py") for i in range(23)]
    pages = walk(everything, limit=5, max_tokens=4000)
    assert [len(p.records) for p in pages] == [5, 5, 5, 5, 3]
    assert [r.key for p in pages for r in p.records] == [r.key for r in everything]
    assert [p.remaining for p in pages] == [18, 13, 8, 3, 0]
    assert all(p.total_estimate == 23 for p in pages)


def test_walk_pages_when_limit_exceeds_total_is_one_page() -> None:
    everything = [rec(f"r{i}") for i in range(4)]
    pages = walk(everything, limit=30, max_tokens=4000)
    assert len(pages) == 1 and pages[0].next_cursor is None and len(pages[0].records) == 4


def test_walk_pages_when_total_is_an_exact_multiple_of_limit() -> None:
    everything = [rec(f"r{i}") for i in range(10)]
    pages = walk(everything, limit=5, max_tokens=4000)
    assert [len(p.records) for p in pages] == [5, 5]
    assert pages[0].next_cursor is not None and pages[1].next_cursor is None


def test_walk_pages_with_both_limit_and_budget_cuts() -> None:
    rng = random.Random(8)
    everything = [rec(f"r{i:03d}", size=rng.randint(1, 25), group=f"f{i // 7}.py") for i in range(80)]
    pages = walk(everything, limit=15, max_tokens=300)
    assert any(len(p.records) < 15 and p.next_cursor for p in pages)  # 予算で切れたページがある
    assert [r.key for p in pages for r in p.records] == [r.key for r in everything]


def test_walk_pages_random() -> None:
    rng = random.Random(20261029)
    for case in range(80):
        n = rng.randint(0, 70)
        everything = [
            rec(f"c{case}r{i:03d}", size=rng.randint(1, 30), group=rng.choice(["a.py", "b.py", None]))
            for i in range(n)
        ]
        limit = rng.randint(1, 20)
        max_tokens = rng.randint(MIN_MAX_TOKENS, 900)
        pages = walk(everything, limit=limit, max_tokens=max_tokens, reserved=rng.randint(0, 60))
        keys = [r.key for p in pages for r in p.records]
        assert keys == [r.key for r in everything], case
        assert pages[-1].next_cursor is None and pages[-1].remaining == 0


# --- 1 件も入らないページはカーソルを出さない（BUG-0015） ------------------------------------------


def test_a_page_with_a_cursor_always_advances_the_offset_random() -> None:
    """どんな予算・予約・オフセットでも、next_cursor を返すページは 1 件以上を含み、オフセットが進む。"""
    rng = random.Random(15)
    zero = 0
    for case in range(300):
        n = rng.randint(1, 40)
        everything = [rec(f"c{case}r{i}", size=rng.choice([1, 5, 40, 300])) for i in range(n)]
        offset = rng.randint(0, n - 1)
        limit = rng.randint(1, 10)
        page = pack(
            tool_window(everything, offset, limit),
            max_tokens=rng.randint(MIN_MAX_TOKENS, 500),
            offset=offset,
            reserved=rng.randint(0, 150),
        )
        if page.next_cursor is not None:
            assert page.records, case
            assert offset_of(page.next_cursor) == offset + len(page.records), case
        elif page.truncated:
            zero += 1
            assert page.records == () and page.warnings and page.summary is None, case
    assert zero > 0  # 1 件も入らない経路を通っている


async def test_pipeline_zero_record_page_has_no_cursor_and_a_larger_max_tokens_succeeds() -> None:
    from lucianke.mcp.pipeline import Pipeline
    from lucianke.mcp.registry import CommonArgs, ToolRegistry, ToolSpec

    class Args(CommonArgs):
        pass

    big = rec("big", size=400)

    async def handler(ctx: object, args: Args) -> ToolOutput:
        return make_out([big, rec("small")], None)

    spec = ToolSpec(
        name="huge_tool",
        tid="T-XX",
        args_model=Args,
        handler=handler,
        default_max_tokens=1000,
        records_key="things",
    )
    pipeline = Pipeline(ToolRegistry([spec]))

    j = await pipeline.call_tool("huge_tool", {"format": "json", "max_tokens": 200})
    assert not j.is_error, j.text
    assert j.structured["page"]["next_cursor"] is None and j.structured["page"]["returned"] == 0
    assert j.structured["budget"]["truncated"] is True and j.structured["budget"]["used_tokens_est"] <= 200
    assert len(j.structured["warnings"]) == 1 and "raise max_tokens to ~" in j.structured["warnings"][0]
    c = await pipeline.call_tool("huge_tool", {"format": "compact", "max_tokens": 200})
    assert "# more:" not in c.text and "! no record fits in max_tokens=200" in c.text

    ok = await pipeline.call_tool("huge_tool", {"format": "json", "max_tokens": 1000})
    assert ok.structured["page"]["returned"] == 2 and ok.structured["page"]["next_cursor"] is None
