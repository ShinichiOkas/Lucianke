"""パッカーの「途中で切れたレコードの続き」（``Record.resume``。仕様書 §11 T-05 規約 7、BUG-0016）。

縮退版が 1 レコードの途中で切れているとき、パッカーは ``next_cursor`` を「同じレコードの再開位置から」にし、
``remaining`` に残りを数え、``truncated`` を立て、そのレコードの後ろは詰めない。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence

from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput, join_offset, split_offset
from lucianke.mcp.budget import CursorCodec, Packer, TokenEstimator

EST = TokenEstimator()
CODEC = CursorCodec()
ROWS = [f"row {i:03d} " + "x" * 30 for i in range(100)]


def cursor_factory(offset: int) -> str:
    return CODEC.encode(tool="t", args={}, offset=offset, data_version=1, workspace_id="w")


def offset_of(cursor: str) -> int:
    return CODEC.decode(cursor, tool="t", args={}, data_version=1, workspace_id="w").offset


def rows_record(key: str, first: int = 0) -> Record:
    """``ROWS[first:]`` を 1 行ずつ持つレコード。収まらなければ先頭から詰め、切った行を ``resume`` に持つ。"""

    def body(upto: int, resume: int | None) -> Record:
        return Record(
            kind="code", key=key, priority=0, lines=tuple(ROWS[first:upto]), data={"k": key},
            confidence=Confidence.STRUCTURAL, group="g", shrink=shrink if resume is None else None, resume=resume,
        )  # fmt: skip

    def shrink(budget: int) -> Record | None:
        best = None
        for upto in range(first + 1, len(ROWS)):
            cand = body(upto, upto)  # upto 行目の手前まで返した。続きは upto 行目から
            if EST.count("\n".join(cand.lines)) <= budget:
                best = cand
            else:
                break
        return best

    return body(len(ROWS), None)


def make_out(records: Sequence[Record]) -> ToolOutput:
    return ToolOutput(
        records=records, max_confidence=Confidence.STRUCTURAL,
        engine=EngineChoice(engine="t", confidence=Confidence.STRUCTURAL), page_unit="1 record",
    )  # fmt: skip


def drain(records_for: Callable[[int | None], list[Record]], max_tokens: int, limit: int = 100) -> list[list[str]]:
    """カーソルを辿って全ページのレコード本文を集める。``records_for(resume)`` は再開位置から作り直したレコード列。"""
    pages: list[list[str]] = []
    offset = 0
    for _ in range(limit):
        index, resume = split_offset(offset)
        recs = records_for(resume if index == 0 else None)
        page = Packer().pack(make_out(recs), max_tokens=max_tokens, offset=offset, cursor_factory=cursor_factory)
        assert page.used_tokens_est <= max_tokens
        assert page.records, "進まないページ"
        pages.append([ln for r in page.records for ln in r.lines])
        if page.next_cursor is None:
            assert page.truncated is False and page.remaining == 0
            return pages
        assert page.truncated is True and page.remaining >= 1
        nxt = offset_of(page.next_cursor)
        assert nxt != offset, "カーソルが進まない"
        offset = nxt
    raise AssertionError("ページが終わらない")


def test_join_split_offset_roundtrip() -> None:
    assert split_offset(5) == (5, None)  # 再開位置の無い既存のオフセットは添字のまま
    assert split_offset(join_offset(7, None)) == (7, None)
    assert split_offset(join_offset(7, 123)) == (7, 123)
    assert split_offset(join_offset(0, 2)) == (0, 2)


def test_truncated_last_record_gets_a_cursor_to_the_same_record() -> None:
    page = Packer().pack(make_out([rows_record("a")]), max_tokens=200, cursor_factory=cursor_factory)
    (rec,) = page.records
    assert rec.resume is not None and 0 < rec.resume < len(ROWS)
    assert page.truncated is True and page.remaining == 1
    assert page.next_cursor is not None
    assert split_offset(offset_of(page.next_cursor)) == (0, rec.resume)  # 同じレコード（添字 0）の続きの行から
    assert page.used_tokens_est <= 200
    assert page.summary is not None


def test_nothing_is_packed_after_a_truncated_record() -> None:
    page = Packer().pack(
        make_out([rows_record("a"), dataclasses.replace(rows_record("b"), shrink=None)]),
        max_tokens=400, cursor_factory=cursor_factory,
    )  # fmt: skip
    assert [r.key for r in page.records] == ["a"]
    index, resume = split_offset(offset_of(page.next_cursor or ""))
    assert index == 0 and resume == page.records[0].resume
    assert page.remaining == 2  # a の残りと b


def test_drain_concatenates_to_the_whole_record_without_overlap_or_gap() -> None:
    for max_tokens in (150, 200, 333, 450):
        pages = drain(lambda resume: [rows_record("a", resume or 0)], max_tokens)
        assert len(pages) >= 2
        assert [ln for page in pages for ln in page] == ROWS, max_tokens


def test_a_record_that_fits_has_no_resume_and_no_cursor() -> None:
    small = dataclasses.replace(rows_record("a"), lines=tuple(ROWS[:3]), resume=None)
    page = Packer().pack(make_out([small]), max_tokens=500, cursor_factory=cursor_factory)
    assert page.next_cursor is None and page.truncated is False and page.remaining == 0
    assert page.records[0].resume is None
