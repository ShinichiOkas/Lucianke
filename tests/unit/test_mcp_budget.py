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
    return "\n".join(r.lines)


def rec(
    key: str,
    tokens: int = 5,
    *,
    group: str | None = None,
    shrink: Callable[[int], Record | None] | None = None,
) -> Record:
    """推定がちょうど ``tokens`` になるレコード（ASCII 英数字 4 文字 = 1 トークン）。"""
    return Record(
        kind="ref",
        key=key,
        priority=0,
        lines=("x" * (tokens * 4),),
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
    """予算 ``b`` トークンに収まる先頭だけを残す縮退版（収まる長さが無ければ ``None``）。"""

    def shrink(budget: int) -> Record | None:
        if log is not None:
            log.append(budget)
        full = text_of(base)
        k = min(len(full) - 1, budget * 4)
        while k > 0 and EST.count(full[:k]) > budget:
            k -= 1
        if k <= 0:
            return None
        return dataclasses.replace(base, lines=(full[:k],), shrink=None)

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
        ("a" * 4, 1),  # ASCII 英数字 1/4
        ("a" * 5, 2),  # 端数は切り上げ
        ("a" * 400, 100),
        (" " * 8, 2),  # 空白も 1/4
        ("a b\nc\t", 2),  # 6 文字 = 1.5
        ("!" * 2, 1),  # ASCII 記号 1/2
        ("!" * 3, 2),
        ("{}();" * 10, 25),  # 50 文字 = 25
        ("_" * 4, 2),  # アンダースコアは記号
        ("あ" * 3, 2),  # かな 1/1.5
        ("あ" * 4, 3),  # 2.67 → 3
        ("漢" * 300, 200),
        ("ＡＢＣ", 2),  # 全角英字
        ("、。", 2),  # CJK の句読点（U+3001, U+3002）は全角扱い
        ("é" * 2, 1),  # その他 1/2
        ("\x00", 1),  # 制御文字はその他
        ("𠮷" * 3, 2),  # CJK 拡張 B も全角扱い
    ],
)
def test_estimator_char_classes(text: str, expected: int) -> None:
    assert EST.count(text) == expected


def test_estimator_mixed_text() -> None:
    # "ab"(2/4) + "!"(1/2) + "あ"(2/3) = 1.666… → 2
    assert EST.count("ab!あ") == 2
    # def f(x): (ASCII 8 英数字と空白 + 記号 3) + 日本語 6 文字
    assert EST.count("def f(x):" + "日本語のコメ") == -(-(6 * 3 + 3 * 6 + 6 * 8) // 12)


def test_estimator_is_subadditive_and_monotone() -> None:
    rng = random.Random(20261029)
    alphabet = "ab 0_!{}\n日本かなＡé😀\x01"
    for _ in range(300):
        a = "".join(rng.choices(alphabet, k=rng.randint(0, 40)))
        b = "".join(rng.choices(alphabet, k=rng.randint(0, 40)))
        assert EST.count(a + b) <= EST.count(a) + EST.count(b)
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
    assert best < 0.005  # 目安は 1 ms 未満。揺れないよう上限は緩くしてある


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
    trailer = EST.count("\n".join(trailer_lines(page.summary, page.next_cursor, page.remaining)))
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
    assert page.remaining == 10 - len(page.records)


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
    assert log == [50]  # 最後の 1 件はトレーラが要らないので、残りをそのまま渡す
    assert len(page.records) == 1
    assert page.records[0].tokens == 50
    assert len(page.records[0].lines[0]) == 200
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
    assert page.records[1].tokens == log[0]
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
    big = dataclasses.replace(big, shrink=lambda budget: rec("big", budget + 1))
    page = Packer().pack(make_out([rec("a", 5), big]), max_tokens=100, cursor_factory=cursor_factory())
    assert [r.key for r in page.records] == ["a"]
    assert page.used_tokens_est <= 100


def test_pack_nothing_fits_gives_zero_records_cursor_and_warning() -> None:
    recs = [rec("huge", 1000), rec("next", 5)]
    page = Packer().pack(make_out(recs), max_tokens=200, reserved_tokens=30, cursor_factory=cursor_factory())
    assert page.records == ()
    assert page.next_cursor is not None
    assert decode_offset(page.next_cursor) == 0
    assert page.truncated is True
    assert page.remaining == 2
    assert len(page.warnings) == 1
    assert "max_tokens" in page.warnings[0]
    assert "1000" in page.warnings[0]  # 必要な大きさを知らせる
    assert page.used_tokens_est <= 200


def test_pack_nothing_fits_keeps_offset_in_cursor() -> None:
    recs = [rec("a", 5), rec("b", 5), rec("huge", 1000)]
    page = Packer().pack(make_out(recs), max_tokens=200, offset=2, cursor_factory=cursor_factory())
    assert page.records == ()
    assert decode_offset(page.next_cursor or "") == 2
    assert page.warnings


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
    assert page.records[0].tokens == EST.count("あいう\nabcd")
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
        assert (page.next_cursor is not None) is page.truncated
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
        if page.truncated:
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
