"""mcp.budget のテスト（設計書 ADR-4・ADR-8、§7.2.1〜7.2.3、仕様書 §5.6・§5.7・§5.8）。"""

from __future__ import annotations

import base64
import dataclasses
import json
import random
import time
from collections.abc import Callable, Sequence

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import MIN_MAX_TOKENS
from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput
from lucianke.mcp.budget import (
    SUMMARY_MAX_CHARS,
    CursorCodec,
    CursorState,
    Packer,
    TokenEstimator,
    default_summary,
    trailer_lines,
)

EST = TokenEstimator()
CODEC = CursorCodec()


# --- 部品 -----------------------------------------------------------------------------------


def text_of(r: Record) -> str:
    """パッカーが数えるレコードの本文（各行の終端の改行を含む。``budget._record_text`` と同じ）。"""
    return "\n".join(r.lines) + "\n" if r.lines else ""


def words(tokens: int) -> str:
    """終端の改行と合わせて、推定がちょうど ``tokens`` トークンになる 1 行。

    英字 1 文字の語を空白で区切る。語 1 つ = 英字の連 1 個 = 0.85 トークン、改行 = 0.6 トークンなので、
    語の数は ``floor((tokens - 0.6) / 0.85)``。
    """
    return " ".join("a" * ((20 * tokens - 12) // 17))


def rec(
    key: str,
    tokens: int = 5,
    *,
    group: str | None = None,
    shrink: Callable[[int], Record | None] | None = None,
) -> Record:
    """パッカーの数えるコストがちょうど ``tokens`` になるレコード（``words`` 1 行）。"""
    return Record(
        kind="ref",
        key=key,
        priority=0,
        lines=(words(tokens),),
        data={"k": key},
        confidence=Confidence.EXACT,
        group=group,
        shrink=shrink,
    )


def make_out(records: Sequence[Record], **kw: object) -> ToolOutput:
    return ToolOutput(
        records=records,
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="treesitter", confidence=Confidence.EXACT),
        page_unit="1 record",
        **kw,  # type: ignore[arg-type]
    )


def make_shrink(base: Record, log: list[int] | None = None) -> Callable[[int], Record | None]:
    """予算 ``b`` トークンに収まる先頭だけを残す縮退版（収まる長さが無ければ ``None``）。

    縮退の契約: 行の本文（終端の改行なし）を ``EST`` で数えて ``budget`` 以下にする。パッカーは終端の改行の分
    （最大 1 トークン）を引いて ``budget`` を渡す。
    """

    def shrink(budget: int) -> Record | None:
        if log is not None:
            log.append(budget)
        full = "\n".join(base.lines)
        lo, hi = 0, len(full) - 1  # 収まる最長の接頭辞（二分探索。収まったときだけ lo を進める）
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if EST.count(full[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        if lo <= 0:
            return None
        return dataclasses.replace(base, lines=(full[:lo],), shrink=None)

    return shrink


def cursor_factory(tool: str = "t", args: dict | None = None, v: int = 7, ws: str | None = "ws1"):
    def factory(offset: int) -> str:
        return CODEC.encode(tool=tool, args=args or {}, offset=offset, data_version=v, workspace_id=ws)

    return factory


def decode_offset(token: str, tool: str = "t", args: dict | None = None, v: int = 7, ws: str | None = "ws1") -> int:
    return CODEC.decode(token, tool=tool, args=args or {}, data_version=v, workspace_id=ws).offset


# --- TokenEstimator -------------------------------------------------------------------------


def test_estimator_empty_is_zero() -> None:
    assert EST.count("") == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a" * 4, 1),  # 英字の連は長さに依らず 1 個 = 0.85
        ("a" * 39, 1),
        ("a" * 40, 26),  # 40 文字以上の英数字は塊で 1 文字 0.63（25.2）
        ("a b c d", 4),  # 連 4 個 = 3.4。単一の空白は 0
        (" " * 8, 1),  # 2 個以上の空白は連 1 個 = 0.92
        ("a b\nc\t", 4),  # 連 3 個 2.55 + 改行 0.6 = 3.15
        ("1234", 4),  # 数字は 1 文字 0.9 = 3.6
        ("!" * 3, 1),  # 記号の連 1 個 = 0.87
        ("{}();" * 10, 1),  # 記号が続けば 1 個の連
        ("_" * 4, 1),  # アンダースコアは記号
        ("あ" * 3, 3),  # かな 1 文字 0.95 = 2.85
        ("あ" * 4, 4),  # 3.8
        ("漢" * 300, 285),  # ちょうど 285.0
        ("ＡＢＣ", 3),  # 全角英字 = 2.85
        ("、。", 2),  # CJK の句読点（U+3001, U+3002）は全角扱い = 1.9
        ("é" * 2, 1),  # その他 1 文字 0.46 = 0.92
        (chr(0), 1),  # 制御文字はその他
        ("𠮷" * 3, 3),  # CJK 拡張 B も全角扱い
    ],
)
def test_estimator_char_classes(text: str, expected: int) -> None:
    assert EST.count(text) == expected


def test_estimator_mixed_text() -> None:
    # 英字の連 "ab"(0.85) + 記号 "!"(0.87) + "あ"(0.95) = 2.67 → 3
    assert EST.count("ab!あ") == 3
    # 英字の連 def・f・x の 3 個、記号の連 "("・"):" の 2 個、全角 6 文字（係数は 1/100 刻み、分母 100）
    assert EST.count("def f(x):" + "日本語のコメ") == -(-(3 * 85 + 2 * 87 + 6 * 95) // 100)


def test_estimator_is_nearly_subadditive_and_monotone() -> None:
    rng = random.Random(20261029)
    alphabet = "ab 0_!{}\n日本かなＡé😀\x01"
    for _ in range(300):
        a = "".join(rng.choices(alphabet, k=rng.randint(0, 40)))
        b = "".join(rng.choices(alphabet, k=rng.randint(0, 40)))
        # 連をまたぐ結合は推定を減らすだけだが、片側が単一の空白で終わり他方が空白で始まると、2 個以上の空白の連
        # （0.92）が新しくできる。切り上げと合わせて +1 まで
        assert EST.count(a + b) <= EST.count(a) + EST.count(b) + 1
        assert EST.count(a + b) >= EST.count(a)


def test_estimator_ten_thousand_chars_is_fast() -> None:
    rng = random.Random(1)
    alphabet = "abcdefghij klmnop(){};=_.\n日本語のテキスト"
    text = "".join(rng.choices(alphabet, k=10_000))
    best = float("inf")
    for _ in range(5):
        t0 = time.perf_counter()
        EST.count(text)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.02  # 目安は 2 ms 前後（1 MB で 150〜200 ms）。揺れないよう上限は緩くしてある


# --- トレーラ・既定の要約 ---------------------------------------------------------------------


def test_trailer_lines_format() -> None:
    assert trailer_lines("38 more", "abc", 38) == ("# summary: 38 more", "# more: cursor=abc remaining≈38")
    assert trailer_lines(None, "abc", 3) == ("# more: cursor=abc remaining≈3",)
    assert trailer_lines("", None, 3) == ("# more: remaining≈3",)


def test_default_summary_by_group() -> None:
    rest = [rec(f"a{i}", group="src/a.py") for i in range(20)] + [rec(f"b{i}", group="src/b.py") for i in range(18)]
    assert default_summary(rest) == "38 more (src/a.py: 20, src/b.py: 18)"


def test_default_summary_without_group() -> None:
    assert default_summary([rec(f"r{i}") for i in range(38)]) == "38 more"


def test_default_summary_caps_groups_and_counts_ungrouped_as_others() -> None:
    rest = [rec(f"g{i}", group=f"f{i}.py") for i in range(8)] + [rec("u")]
    s = default_summary(rest)
    assert s == "9 more (f0.py: 1, f1.py: 1, f2.py: 1, f3.py: 1, f4.py: 1, others: 4)"


# --- Packer ---------------------------------------------------------------------------------


def test_pack_everything_fits() -> None:
    recs = [rec(f"r{i}", 10) for i in range(5)]
    page = Packer().pack(make_out(recs), max_tokens=200, reserved_tokens=30, cursor_factory=cursor_factory())
    assert [r.key for r in page.records] == [f"r{i}" for i in range(5)]
    assert page.next_cursor is None
    assert page.summary is None
    assert page.truncated is False
    assert page.remaining == 0
    assert page.total_estimate == 5
    assert page.used_tokens_est == 30 + 50  # トレーラは出さない
    assert page.max_tokens == 200
    assert page.warnings == ()
    assert all(r.tokens == 10 for r in page.records)


def test_pack_exactly_full_without_trailer_is_not_truncated() -> None:
    recs = [rec(f"r{i}", 10) for i in range(4)]
    page = Packer().pack(make_out(recs), max_tokens=200, reserved_tokens=160, cursor_factory=cursor_factory())
    assert len(page.records) == 4
    assert page.used_tokens_est == 200
    assert page.truncated is False


def test_pack_empty_records() -> None:
    page = Packer().pack(make_out([]), max_tokens=200, reserved_tokens=20, cursor_factory=cursor_factory())
    assert page.records == ()
    assert page.next_cursor is None
    assert page.truncated is False
    assert page.warnings == ()
    assert page.used_tokens_est == 20
    assert page.total_estimate == 0


def test_pack_stops_when_full_and_never_cuts_a_record() -> None:
    recs = [rec(f"r{i}", 30, group="a.py" if i < 6 else "b.py") for i in range(10)]
    page = Packer().pack(make_out(recs), max_tokens=200, reserved_tokens=20, cursor_factory=cursor_factory())
    n = len(page.records)
    assert 0 < n < 10
    assert [r.key for r in page.records] == [f"r{i}" for i in range(n)]
    assert all(r.lines == recs[i].lines for i, r in enumerate(page.records))  # 途中で切れていない
    assert page.truncated is True
    assert page.remaining == 10 - n
    assert page.total_estimate == 10
    assert page.used_tokens_est <= 200
    assert page.next_cursor is not None
    assert decode_offset(page.next_cursor) == n
    assert page.summary == default_summary(recs[n:])


def test_pack_trailer_is_counted_in_used() -> None:
    recs = [rec(f"r{i}", 30) for i in range(10)]
    page = Packer().pack(make_out(recs), max_tokens=200, reserved_tokens=20, cursor_factory=cursor_factory())
    assert page.next_cursor is not None and page.summary is not None
    trailer = EST.count("\n".join(trailer_lines(page.summary, page.next_cursor, page.remaining)) + "\n")
    assert page.used_tokens_est == 20 + 30 * len(page.records) + trailer


def test_pack_without_cursor_factory_is_truncated_without_cursor() -> None:
    recs = [rec(f"r{i}", 30) for i in range(10)]
    page = Packer().pack(make_out(recs), max_tokens=200)
    assert page.truncated is True
    assert page.next_cursor is None
    assert page.remaining == 10 - len(page.records) > 0
    assert page.used_tokens_est <= 200


def test_pack_uses_tool_summary_and_caps_it() -> None:
    recs = [rec(f"r{i}", 30) for i in range(20)]
    page = Packer().pack(
        make_out(recs, summarize=lambda rest: f"custom {len(rest)}"),
        max_tokens=200,
        cursor_factory=cursor_factory(),
    )
    assert page.summary == f"custom {page.remaining}"
    long_page = Packer().pack(
        make_out(recs, summarize=lambda rest: "y" * 1000 + "\nz"), max_tokens=400, cursor_factory=cursor_factory()
    )
    assert long_page.summary is not None
    assert len(long_page.summary) == SUMMARY_MAX_CHARS
    assert long_page.summary.endswith("…")
    assert "\n" not in long_page.summary
    assert long_page.used_tokens_est <= 400


def test_pack_empty_tool_summary_falls_back_to_default() -> None:
    recs = [rec(f"r{i}", 30) for i in range(10)]
    page = Packer().pack(make_out(recs, summarize=lambda rest: ""), max_tokens=200, cursor_factory=cursor_factory())
    assert page.summary == default_summary(recs[len(page.records) :])


def test_pack_total_estimate_from_tool_wins() -> None:
    recs = [rec(f"r{i}", 30) for i in range(10)]
    page = Packer().pack(make_out(recs, total_estimate=5000), max_tokens=200, cursor_factory=cursor_factory())
    assert page.total_estimate == 5000
    # 渡した 10 件を超える分（4990 件）は具体化していない続き。入らなかった分と合わせて残りに数える（BUG-0008）
    assert page.remaining == 5000 - len(page.records)


def test_pack_total_estimate_default_counts_from_offset() -> None:
    recs = [rec(f"r{i}", 1) for i in range(10)]
    page = Packer().pack(make_out(recs), max_tokens=500, offset=4)
    assert page.total_estimate == 4 + 6
    assert [r.key for r in page.records] == [f"r{i}" for i in range(4, 10)]


def test_pack_offset_past_end_is_empty_without_cursor() -> None:
    recs = [rec(f"r{i}", 1) for i in range(3)]
    page = Packer().pack(make_out(recs), max_tokens=300, offset=3, reserved_tokens=10, cursor_factory=cursor_factory())
    assert page.records == ()
    assert page.next_cursor is None
    assert page.truncated is False
    assert page.warnings == ()
    assert page.used_tokens_est == 10


def test_pack_shrink_is_used_once_for_the_last_record() -> None:
    log: list[int] = []
    big = rec("big", 100)
    big = dataclasses.replace(big, shrink=make_shrink(big, log))
    page = Packer().pack(make_out([big]), max_tokens=200, reserved_tokens=150, cursor_factory=cursor_factory())
    assert log == [49]  # 最後の 1 件はトレーラが要らないので、残りから終端の改行の分（1）だけ引いて渡す
    assert len(page.records) == 1
    assert page.records[0].tokens == 50  # 本文 49 トークン（収まる最長）+ 終端の改行で 50
    assert EST.count(page.records[0].lines[0]) == 49
    assert page.truncated is False
    assert page.next_cursor is None
    assert page.used_tokens_est == 200


def test_pack_shrink_for_a_middle_record_reserves_the_trailer() -> None:
    log: list[int] = []
    big = rec("big", 100)
    big = dataclasses.replace(big, shrink=make_shrink(big, log))
    recs = [rec("a", 10), big, rec("c", 10), rec("d", 10)]
    page = Packer().pack(make_out(recs), max_tokens=200, reserved_tokens=100, cursor_factory=cursor_factory())
    assert len(log) == 1
    assert log[0] < 100 - 10  # a の後の残りから、トレーラの見込み分が引かれている
    assert [r.key for r in page.records] == ["a", "big"]
    assert EST.count(page.records[1].lines[0]) <= log[0]  # 縮退は渡された予算に本文を収める
    assert page.records[1].tokens == EST.count(text_of(page.records[1])) <= log[0] + 1  # 終端の改行の分だけ増える
    assert page.used_tokens_est <= 200
    assert page.next_cursor is not None
    assert decode_offset(page.next_cursor) == len(page.records)


def test_pack_shrink_returning_none_stops() -> None:
    log: list[int] = []

    def shrink(budget: int) -> Record | None:
        log.append(budget)
        return None

    big = dataclasses.replace(rec("big", 100), shrink=shrink)
    recs = [rec("a", 10), big, rec("c", 10)]
    page = Packer().pack(make_out(recs), max_tokens=100, cursor_factory=cursor_factory())
    assert [r.key for r in page.records] == ["a"]  # big で止まり、c は飛ばして詰めない
    assert len(log) == 1
    assert page.remaining == 2
    assert decode_offset(page.next_cursor or "") == 1


def test_pack_shrink_result_that_still_does_not_fit_stops() -> None:
    big = rec("big", 100)
    big = dataclasses.replace(big, shrink=lambda budget: rec("big", budget + 2))  # 渡された予算より 1 大きい（改行の分）
    page = Packer().pack(make_out([rec("a", 5), big]), max_tokens=100, cursor_factory=cursor_factory())
    assert [r.key for r in page.records] == ["a"]
    assert page.used_tokens_est <= 100


def test_pack_nothing_fits_gives_zero_records_and_warning_without_a_cursor() -> None:
    """BUG-0015: 1 件も入らないとき、進まないカーソル（同じオフセット）は出さない。理由と必要な大きさを警告で返す。"""
    recs = [rec("huge", 1000), rec("next", 5)]
    page = Packer().pack(make_out(recs), max_tokens=200, reserved_tokens=30, cursor_factory=cursor_factory())
    assert page.records == ()
    assert page.next_cursor is None
    assert page.summary is None
    assert page.truncated is True
    assert page.remaining == 2
    assert len(page.warnings) == 1
    assert "max_tokens" in page.warnings[0]
    assert "~1000 tokens" in page.warnings[0] and "~1030 or more" in page.warnings[0]  # 必要な大きさ（予約の分も）
    assert page.used_tokens_est == 30 + EST.count("! " + page.warnings[0] + "\n")  # トレーラは描かれないので数えない
    assert page.used_tokens_est <= 200


def test_pack_nothing_fits_at_an_offset_gives_no_cursor_either() -> None:
    recs = [rec("a", 5), rec("b", 5), rec("huge", 1000)]
    page = Packer().pack(make_out(recs), max_tokens=200, offset=2, cursor_factory=cursor_factory())
    assert page.records == ()
    assert page.next_cursor is None
    assert page.remaining == 1 and page.truncated is True
    assert page.warnings


def test_pack_nothing_fits_the_suggested_max_tokens_works() -> None:
    """警告の目安（予約 + 次のレコード）まで上げれば、同じ引数でそのレコードが入る。"""
    recs = [rec("huge", 300), rec("next", 300)]
    small = Packer().pack(make_out(recs), max_tokens=200, reserved_tokens=40, cursor_factory=cursor_factory())
    assert small.records == () and "~340 or more" in small.warnings[0]
    page = Packer().pack(make_out(recs), max_tokens=340 + 60, reserved_tokens=40, cursor_factory=cursor_factory())
    assert [r.key for r in page.records] == ["huge"]  # トレーラの分は余裕に含める
    assert page.next_cursor is not None and decode_offset(page.next_cursor) == 1


def test_pack_drops_summary_before_giving_up_on_records() -> None:
    # 続きが少ないと長い要約を返すツール（見込みより実際のトレーラが大きくなる）。
    # 要約（SHOULD）を省けばレコードが入る予算では、0 件にせず要約を省く
    def summarize(rest: Sequence[Record]) -> str:
        return "z" * 400 if len(rest) <= 8 else ""

    recs = [rec(f"r{i}", 20, group=f"file{i}.py") for i in range(10)]
    out = make_out(recs, summarize=summarize)
    dropped = 0
    for max_tokens in range(200, 330):
        page = Packer().pack(out, max_tokens=max_tokens, reserved_tokens=95, cursor_factory=cursor_factory())
        assert page.used_tokens_est <= max_tokens
        dropped += page.summary is None and bool(page.records) and page.truncated
    assert dropped > 0


def test_pack_pagination_has_no_duplicates_and_no_gaps() -> None:
    rng = random.Random(7)
    recs = [rec(f"r{i:03d}", rng.randint(1, 30), group=f"f{i // 7}.py") for i in range(120)]
    out = make_out(recs)
    args = {"q": "x"}
    factory = cursor_factory(args=args)
    seen: list[str] = []
    offset = 0
    pages = 0
    while True:
        page = Packer().pack(out, max_tokens=300, offset=offset, reserved_tokens=40, cursor_factory=factory)
        pages += 1
        assert page.used_tokens_est <= 300
        assert page.records or page.next_cursor is None  # 進まないページは無い
        seen += [r.key for r in page.records]
        if page.next_cursor is None:
            break
        offset = decode_offset(page.next_cursor, args=args)
        assert offset == len(seen)
    assert seen == [r.key for r in recs]
    assert pages > 3


def test_pack_fills_tokens_on_included_records() -> None:
    recs = [
        dataclasses.replace(rec("a", 3), lines=("あいう", "abcd")),
        dataclasses.replace(rec("b", 3), lines=()),
    ]
    page = Packer().pack(make_out(recs), max_tokens=300)
    assert page.records[0].tokens == EST.count("あいう\nabcd\n")
    assert page.records[1].tokens == 0


# --- 乱数での不変条件 -------------------------------------------------------------------------


def _random_records(rng: random.Random, n: int, max_tokens_each: int) -> list[Record]:
    groups = ["src/a.py", "src/b.py", "lib/日本語.py", None]
    recs: list[Record] = []
    for i in range(n):
        lines = tuple(
            "".join(rng.choices("abcdef (){}_.日本語", k=rng.randint(1, max_tokens_each * 3)))
            for _ in range(rng.randint(1, 3))
        )
        r = dataclasses.replace(rec(f"r{i}", 1, group=rng.choice(groups)), lines=lines)
        if rng.random() < 0.3:
            r = dataclasses.replace(r, shrink=make_shrink(r))
        recs.append(r)
    return recs


def _long_summary(width: int) -> Callable[[Sequence[Record]], str]:
    def summarize(rest: Sequence[Record]) -> str:
        return "要約" * width + str(len(rest))

    return summarize


def _summary_when_few(rest: Sequence[Record]) -> str:
    """続きが少ないほど長い（見込みより実際のトレーラが大きくなる）要約。"""
    return "z" * 400 if len(rest) <= 5 else ""


SUMMARIZERS: list[Callable[[Sequence[Record]], str] | None] = [
    None,
    None,
    _long_summary(10),
    _long_summary(200),
    _long_summary(900),
    _summary_when_few,
]


def test_used_tokens_never_exceed_max_tokens_random() -> None:
    rng = random.Random(20261029)
    packer = Packer()
    shrunk_pages = 0
    zero_pages = 0
    for case in range(200):
        n = rng.randint(0, 60)
        max_tokens = rng.randint(MIN_MAX_TOKENS, 1500)
        reserved = rng.randint(0, 100)
        recs = _random_records(rng, n, rng.choice([5, 30, 120]))
        out = make_out(recs, summarize=rng.choice(SUMMARIZERS))
        offset = rng.randint(0, n + 1)
        args = {"case": case}
        ws = rng.choice([None, "app", "ワークスペース-" * rng.randint(1, 5)])
        version = rng.randint(0, 10**6)
        factory = cursor_factory(args=args, v=version, ws=ws)
        page = packer.pack(
            out, max_tokens=max_tokens, offset=offset, reserved_tokens=reserved, cursor_factory=factory
        )

        assert page.used_tokens_est <= max_tokens, (case, page.used_tokens_est, max_tokens)
        tail = recs[offset:]
        assert len(page.records) + page.remaining == len(tail)
        assert page.truncated is (page.remaining > 0)
        # 続きがあって 1 件以上入ったときだけカーソルを出す。1 件も入らないページは進まないカーソルを出さない（BUG-0015）
        assert (page.next_cursor is not None) is (page.truncated and bool(page.records))
        if not page.truncated:
            assert page.summary is None
        for i, r in enumerate(page.records):
            orig = tail[i]
            assert r.key == orig.key
            assert r.lines == orig.lines or (orig.shrink is not None and len(text_of(r)) < len(text_of(orig)))
            assert r.tokens == EST.count(text_of(r))
            shrunk_pages += r.lines != orig.lines
        if page.truncated and not page.records:
            zero_pages += 1
            assert page.warnings
            assert page.next_cursor is None and page.summary is None
        if page.next_cursor is not None:
            state = CODEC.decode(
                page.next_cursor or "", tool="t", args=args, data_version=version, workspace_id=ws
            )
            assert state.offset == offset + len(page.records)
    assert shrunk_pages > 0  # 縮退の経路を通っている
    assert zero_pages > 0  # 0 件の経路を通っている


def test_pagination_concatenation_equals_original_random() -> None:
    rng = random.Random(99)
    packer = Packer()
    for case in range(60):
        n = rng.randint(0, 80)
        max_tokens = rng.randint(MIN_MAX_TOKENS, 700)
        recs = _random_records(rng, n, 12)
        out = make_out(recs, summarize=SUMMARIZERS[case % len(SUMMARIZERS)])
        args = {"case": case}
        factory = cursor_factory(args=args)
        keys: list[str] = []
        offset = 0
        for _ in range(n + 2):
            page = packer.pack(out, max_tokens=max_tokens, offset=offset, reserved_tokens=40, cursor_factory=factory)
            assert page.used_tokens_est <= max_tokens
            keys += [r.key for r in page.records]
            if page.next_cursor is None:
                break
            assert page.records, (case, offset)  # 進まないページは無い（この大きさのレコードなら）
            offset = decode_offset(page.next_cursor, args=args)
        else:
            pytest.fail(f"case {case}: did not terminate")
        assert keys == [r.key for r in recs]


# --- CursorCodec ----------------------------------------------------------------------------

ARGS = {"symbol_name": "login", "max_results": 10}


def _encode(**kw: object) -> str:
    base: dict[str, object] = {"tool": "find_references", "args": ARGS, "offset": 12, "data_version": 5, "workspace_id": "app"}
    base.update(kw)
    return CODEC.encode(**base)  # type: ignore[arg-type]


def _decode(token: str, **kw: object) -> CursorState:
    base: dict[str, object] = {"tool": "find_references", "args": ARGS, "data_version": 5, "workspace_id": "app"}
    base.update(kw)
    return CODEC.decode(token, **base)  # type: ignore[arg-type]


def test_cursor_roundtrip() -> None:
    token = _encode()
    state = _decode(token)
    assert state == CursorState(
        tool="find_references", args_hash=CODEC.hash_args(ARGS), offset=12, data_version=5, workspace_id="app"
    )


def test_cursor_is_base64url_without_padding_and_has_the_documented_payload() -> None:
    token = _encode(workspace_id=None)
    assert "=" not in token
    assert all(c.isalnum() or c in "-_" for c in token)
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert payload == {"t": "find_references", "a": CODEC.hash_args(ARGS), "o": 12, "v": 5, "w": None}
    assert len(payload["a"]) == 12
    assert _decode(token, workspace_id=None).workspace_id is None


def test_cursor_args_hash_ignores_cursor_max_tokens_format_and_key_order() -> None:
    token = _encode()
    same = {"max_results": 10, "symbol_name": "login", "cursor": token, "max_tokens": 9999, "format": "json"}
    assert _decode(token, args=same).offset == 12
    assert CODEC.hash_args(same) == CODEC.hash_args(ARGS)


def test_cursor_args_hash_handles_nested_and_non_ascii() -> None:
    a = {"paths": ["a", "日本"], "opt": {"y": 1, "x": [1, 2]}}
    b = {"opt": {"x": [1, 2], "y": 1}, "paths": ["a", "日本"]}
    assert CODEC.hash_args(a) == CODEC.hash_args(b)
    assert CODEC.hash_args(a) != CODEC.hash_args({"paths": ["a"], "opt": {}})


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(lambda t: t[: len(t) // 2], id="truncated"),
        pytest.param(lambda t: t[:-1] + "!", id="bad-char"),
        pytest.param(lambda t: "", id="empty"),
        pytest.param(lambda t: "!!!!", id="not-base64"),
        pytest.param(lambda t: t + "====", id="excess-padding"),
        pytest.param(lambda t: "日本語", id="non-ascii"),
        pytest.param(lambda t: base64.urlsafe_b64encode(b"not json").decode(), id="not-json"),
        pytest.param(lambda t: base64.urlsafe_b64encode(b"\xff\xfe").decode(), id="not-utf8"),
        pytest.param(lambda t: base64.urlsafe_b64encode(b"[1,2]").decode(), id="json-not-object"),
        pytest.param(lambda t: base64.urlsafe_b64encode(b'{"t":"find_references"}').decode(), id="missing-keys"),
    ],
)
def test_cursor_garbage_is_invalid_argument(bad: Callable[[str], str]) -> None:
    with pytest.raises(LuciankeError) as ei:
        _decode(bad(_encode()))
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details == {"param": "cursor"}


@pytest.mark.parametrize(
    "payload",
    [
        {"t": "find_references", "a": "x", "o": -1, "v": 5, "w": "app"},
        {"t": "find_references", "a": "x", "o": True, "v": 5, "w": "app"},
        {"t": "find_references", "a": "x", "o": "3", "v": 5, "w": "app"},
        {"t": "find_references", "a": "x", "o": 3, "v": "5", "w": "app"},
        {"t": 1, "a": "x", "o": 3, "v": 5, "w": "app"},
        {"t": "find_references", "a": "x", "o": 3, "v": 5, "w": 7},
    ],
)
def test_cursor_wrong_field_types_are_invalid_argument(payload: dict) -> None:
    token = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    with pytest.raises(LuciankeError) as ei:
        _decode(token)
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details == {"param": "cursor"}


def test_cursor_non_string_token_is_invalid_argument() -> None:
    with pytest.raises(LuciankeError) as ei:
        _decode(None)  # type: ignore[arg-type]
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT


@pytest.mark.parametrize(
    "mismatch",
    [
        {"tool": "get_file_skeleton"},
        {"args": {"symbol_name": "logout", "max_results": 10}},
        {"args": {"symbol_name": "login"}},
        {"workspace_id": "other"},
        {"workspace_id": None},
    ],
)
def test_cursor_mismatch_is_invalid_argument(mismatch: dict) -> None:
    with pytest.raises(LuciankeError) as ei:
        _decode(_encode(), **mismatch)
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details == {"param": "cursor"}


def test_cursor_data_version_mismatch_is_expired() -> None:
    with pytest.raises(LuciankeError) as ei:
        _decode(_encode(), data_version=6)
    assert ei.value.code is ErrorCode.E_CURSOR_EXPIRED


def test_cursor_mismatch_is_checked_before_expiry() -> None:
    # 別ツールのカーソルは、data_version が古くても「失効」ではなく「不正」
    with pytest.raises(LuciankeError) as ei:
        _decode(_encode(), tool="other", data_version=99)
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
