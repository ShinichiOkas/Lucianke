"""予算を増やしたときのパッカーの単調性（仕様書 §5.6-5、設計書 §7.2.2、BUG-0067）。

``max_tokens`` を上げた応答は、小さい予算の応答より情報が減らない: 2 つの予算 ``B < B'`` について

* 返るレコードの件数は ``B'`` のほうが少なくない（同じオフセットから先頭順に詰めるので、``B`` のレコードは ``B'`` にも入る）。
* ``B`` で入った各レコードは、``B'`` でも同じか大きい版で入る（縮退版が丸ごとに、浅い段に、切れた本文が長くなる。小さくならない）。

パッカーは丸ごと入らない最初のレコードだけを縮退させてページの最後にし、要約（SHOULD）はレコードを詰めた残りにだけ付ける。
以前は縮退版の後ろにも詰め、要約の分を先に取り分けていたので、予算を上げると件数が減った（BUG-0067 の実測:
F-PY の T-02 json で 6000 → 2 件、8000 → 1 件）。
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Callable, Sequence

from test_mcp_budget import (
    EST,
    SUMMARIZERS,
    _random_records,
    cursor_factory,
    decode_offset,
    make_out,
    make_shrink,
    rec,
    words,
)

from lucianke.core.records import Record, join_offset, split_offset
from lucianke.mcp.budget import Packer, trailer_lines
from lucianke.mcp.types import Page

# --- 部品 -----------------------------------------------------------------------------------


def _levels_shrink(base: Record, sizes: Sequence[int]) -> Callable[[int], Record | None]:
    """骨格（``syntax/skeleton.py``）と同じ形の段階的な縮退: 行の本文が ``ask`` に収まる最も浅い段を返す。

    ``sizes`` は浅い段から深い段への各段の大きさ（``words`` のトークン数。狭義減少）。どの段も入らなければ ``None``。
    """
    levels = [dataclasses.replace(base, lines=(words(s),), shrink=None) for s in sizes]
    costs = [EST.count("\n".join(lv.lines)) for lv in levels]

    def shrink(ask: int) -> Record | None:
        for lv, c in zip(levels, costs, strict=True):
            if c <= ask:
                return lv
        return None

    return shrink


ROWS = [f"row {i:03d} " + "x" * 20 for i in range(80)]


def _rows_record(key: str, rows: int) -> Record:
    """``ROWS[:rows]`` を 1 行ずつ持つレコード。収まらなければ先頭から詰め、切った行を ``resume`` に持つ（T-05 の形）。"""

    def body(upto: int, resume: int | None) -> Record:
        return Record(
            kind="code", key=key, priority=0, lines=tuple(ROWS[:upto]), data={"k": key},
            confidence=rec("x").confidence, group="g", shrink=shrink if resume is None else None, resume=resume,
        )  # fmt: skip

    def shrink(ask: int) -> Record | None:
        best = None
        for upto in range(1, rows):
            cand = body(upto, upto)
            if EST.count("\n".join(cand.lines)) <= ask:
                best = cand
            else:
                break
        return best

    return body(rows, None)


def _mixed_records(rng: random.Random, n: int) -> list[Record]:
    """縮退の無いもの・段階的な縮退（骨格の形）・接頭辞の縮退・途中で切れる版（``resume``）を混ぜたレコード列。"""
    recs: list[Record] = []
    groups = ["src/a.py", "src/b.py", "lib/日本語.py", None]
    for i in range(n):
        kind = rng.random()
        if kind < 0.1:
            recs.append(_rows_record(f"r{i}", rng.randint(2, 60)))
            continue
        full = rng.randint(2, 400)
        r = rec(f"r{i}", full, group=rng.choice(groups))
        if kind < 0.6 and full > 4:
            sizes = sorted({rng.randint(2, full - 1) for _ in range(rng.randint(1, 4))}, reverse=True)
            r = dataclasses.replace(r, shrink=_levels_shrink(r, sizes))
        elif kind < 0.75:
            r = dataclasses.replace(r, shrink=make_shrink(r))
        recs.append(r)
    return recs


def _json_like_record(rec_: Record) -> str:
    """compact の行に比例しない別の形式（json の ``data`` のように固定分と倍率がある）。縮退の二分探索の経路を通す。"""
    body = "\n".join(rec_.lines)
    return '{"k":"' + rec_.key + '","body":"' + body + " " + body + '"},'


def _json_like_trailer(summary: str | None, cursor: str | None, remaining: int) -> str:
    """json のトレーラ（``render/json_out.trailer_text`` と同じ形: 要約は無く、増えるのはカーソルの文字列だけ）。"""
    return f'"{cursor}"' if cursor else ""


def _sizes(page: Page) -> list[int]:
    return [r.tokens if r.tokens is not None else 0 for r in page.records]


def _assert_no_less_information(lower: Page, upper: Page, context: object) -> None:
    """``upper``（大きい予算）は ``lower`` のレコードを全部、同じか大きい版で含む。"""
    assert len(upper.records) >= len(lower.records), (context, _sizes(lower), _sizes(upper))
    for a, b in zip(lower.records, upper.records, strict=False):
        assert a.key == b.key, context
        assert (b.tokens or 0) >= (a.tokens or 0), (context, a.key, _sizes(lower), _sizes(upper))
        if a.resume is None and a.lines == b.lines:
            continue
        if a.resume is not None and b.resume is not None:
            assert b.resume >= a.resume, (context, a.key, a.resume, b.resume)  # 切れた本文は長くなる


def _scan(
    records: list[Record],
    *,
    budgets: range,
    reserved: int,
    offset: int = 0,
    summarize: Callable[[Sequence[Record]], str] | None = None,
    total_estimate: int | None = None,
    record_text: Callable[[Record], str] | None = None,
    trailer_text: Callable[[str | None, str | None, int], str] | None = None,
    form: str = "compact",
    context: object = None,
) -> list[Page]:
    """予算を ``budgets`` の順に上げて詰め、隣どうしで情報が減らないことと、各ページの不変条件を確かめる。"""
    packer = Packer()
    out = make_out(records, summarize=summarize, total_estimate=total_estimate)
    args = {"case": str(context)}
    factory = cursor_factory(args=args)
    pages: list[Page] = []
    for b in budgets:
        page = packer.pack(
            out, max_tokens=b, offset=offset, reserved_tokens=reserved, cursor_factory=factory,
            record_text=record_text, trailer_text=trailer_text, form=form,
        )  # fmt: skip
        if page.records:
            assert page.used_tokens_est <= b, (context, b, page.used_tokens_est)
        assert (page.next_cursor is not None) is (page.truncated and bool(page.records)), (context, b)
        if page.next_cursor is not None:  # カーソルを返すページは進む（BUG-0015）
            index, resume = split_offset(decode_offset(page.next_cursor, args=args))
            last = page.records[-1]
            assert (index, resume) == ((offset + len(page.records) - 1, last.resume) if last.resume is not None
                                       else (offset + len(page.records), None)), (context, b)  # fmt: skip
        for j, r in enumerate(page.records[:-1]):  # 縮退版・切れた版はページの最後の 1 件だけ
            orig = records[offset + j]
            assert r.lines == orig.lines and r.resume is None, (context, b, r.key)
        if pages:
            _assert_no_less_information(pages[-1], page, (context, b))
        pages.append(page)
    return pages


# --- 単調性 ---------------------------------------------------------------------------------


def test_larger_budget_never_returns_fewer_or_smaller_records_random() -> None:
    """予算を上げても、件数は減らず、どのレコードも小さくならない（縮退の段・接頭辞・切れる版、compact・別の形式、要約あり）。"""
    rng = random.Random(20261031)
    grew_to_whole = 0
    gained_records = 0
    for case in range(45):
        n = rng.randint(1, 8)
        records = _mixed_records(rng, n) if case % 3 else _random_records(rng, n, 60)
        lo = rng.randint(60, 300)
        fmt = rng.choice(["compact", "compact", "json"])
        pages = _scan(
            records,
            budgets=range(lo, lo + rng.randint(120, 360), rng.choice([1, 2, 3])),
            reserved=rng.randint(0, 80),
            offset=rng.randint(0, max(0, n - 1)),
            summarize=rng.choice(SUMMARIZERS),
            total_estimate=rng.choice([None, None, n + rng.randint(1, 5)]),
            record_text=_json_like_record if fmt == "json" else None,
            trailer_text=_json_like_trailer if fmt == "json" else None,
            context=case,
        )
        for a, b in zip(pages, pages[1:], strict=False):
            gained_records += len(b.records) > len(a.records)
            grew_to_whole += any((y.tokens or 0) > (x.tokens or 0) for x, y in zip(a.records, b.records, strict=False))
    assert grew_to_whole > 0  # 縮退版が大きくなる経路を通っている
    assert gained_records > 0


def test_record_count_is_monotone_with_many_small_records_and_long_summaries() -> None:
    """要約（SHOULD）がレコードを押し出さない。以前は要約つきで 1 件入る予算に上げると、要約なしで入っていた後ろの
    レコードが消えた（max_tokens を 2 上げて 11 件 → 1 件）。"""
    rng = random.Random(67)
    for case in range(40):
        n = rng.randint(1, 30)
        records = [rec(f"r{i}", rng.randint(1, 60), group=rng.choice(["a", "b", None])) for i in range(n)]
        lo = rng.randint(60, 300)
        _scan(
            records,
            budgets=range(lo, lo + 450, 3),
            reserved=rng.randint(0, 60),
            offset=rng.randint(0, n - 1),
            summarize=rng.choice(SUMMARIZERS),
            total_estimate=rng.choice([None, n + 3]),
            context=case,
        )


def test_bug_0067_shape_a_larger_budget_keeps_both_the_count_and_the_detail() -> None:
    """BUG-0067 の形: 1 件目は縮退すると小さく（入れ子を落とすと最上位だけ）、2 件目は縮めにくい。

    以前は、小さい予算で 1 件目が縮退して 2 件目も入り（2 件）、1 件目が丸ごと入る予算では 2 件目が次ページに回った（1 件）。
    今は縮退版でページを閉じるので、小さい予算は 1 件目の縮退版とカーソル、大きい予算は 1 件目を丸ごととカーソル、
    両方入る予算で 2 件。どの予算でも、それより小さい予算の応答の情報を失わない。
    """
    first = rec("urls", 660)
    first = dataclasses.replace(first, shrink=_levels_shrink(first, [630, 44]))
    second = rec("api", 210)
    second = dataclasses.replace(second, shrink=_levels_shrink(second, [203]))
    out = make_out([first, second])
    factory = cursor_factory()
    small = Packer().pack(out, max_tokens=600, reserved_tokens=12, cursor_factory=factory)
    large = Packer().pack(out, max_tokens=800, reserved_tokens=12, cursor_factory=factory)
    both = Packer().pack(out, max_tokens=1000, reserved_tokens=12, cursor_factory=factory)
    assert [r.key for r in small.records] == ["urls"] and small.next_cursor is not None
    assert small.records[0].tokens is not None and small.records[0].tokens < 660  # 縮退版でページを閉じる
    assert decode_offset(small.next_cursor) == 1  # 2 件目は次ページで丸ごと返る
    assert [r.key for r in large.records] == ["urls"] and large.records[0].tokens == 660
    assert [r.key for r in both.records] == ["urls", "api"] and both.next_cursor is None
    _scan([first, second], budgets=range(100, 1200), reserved=12, context="bug-0067")


def test_a_long_summary_is_cut_to_its_share_and_never_pushes_records_out() -> None:
    """長い要約（日本語 400 文字 ≈ 380 トークン）は、レコードに使える予算の 1/4 までに「…」で切って付く。
    予算を上げてもレコードは減らず、要約は消えない（以前は要約つきで 1 件入る予算で、後ろのレコードが消えた）。"""
    long_text = "要約" * 200
    recs = [rec(f"r{i}", 20, group=f"file{i % 3}.py") for i in range(60)]
    pages = _scan(recs, budgets=range(150, 900), reserved=30, summarize=lambda rest: long_text, context="long")
    cut = 0
    for page in pages:
        assert page.records and page.truncated
        assert page.summary is not None  # 取り分があるので、少なくとも切った形が入る
        assert page.summary == long_text or page.summary.endswith("…")
        cut += page.summary != long_text
    assert cut > 0
    short = Packer().pack(make_out(recs), max_tokens=500, reserved_tokens=30, cursor_factory=cursor_factory())
    assert short.summary is not None and not short.summary.endswith("…")  # ふつうの要約はふつうの予算で丸ごと入る


def test_summary_share_never_pushes_out_the_smallest_shrunk_first_record() -> None:
    """BUG-0110: 先頭が縮退できるとき、要約（SHOULD）の取り分は先頭の最も小さい縮退版と ``# more:`` 行を押し出さない。
    その本文が入る予算からは必ず 1 件以上が返る（以前は 1/4 の取り分のために、その上の帯でも 0 件だった）。縮退版で
    ページを閉じるときの要約（BUG-0067 の取り分）はこれまでどおり残る。予算に対して単調（``_scan``）。"""
    first = rec("big", 400, group="a.py")
    first = dataclasses.replace(first, shrink=_levels_shrink(first, [300, 120]))
    recs = [first] + [rec(f"r{i}", 20, group=f"f{i}.py") for i in range(6)]
    context = "smallest-first"
    factory = cursor_factory(args={"case": context})
    more_hi = EST.count("\n".join(trailer_lines(None, factory(join_offset(0, (1 << 31) - 1)), 7)) + "\n")
    threshold = 20 + EST.count(words(120)) + 1 + more_hi  # 予約 + 最も小さい段（ask）+ 改行 + # more: 行（上限）
    budgets = range(threshold - 40, threshold + 260)
    pages = _scan(recs, budgets=budgets, reserved=20, summarize=lambda rest: "要約" * 60, context=context)
    shrunk_with_summary = 0
    for b, page in zip(budgets, pages, strict=True):
        if b >= threshold:
            assert page.records, (b, threshold)
        if page.records and (page.records[0].tokens or 0) < 400 and page.summary:
            shrunk_with_summary += 1
    assert any(not p.records for p in pages)  # 境目の下も通っている
    assert shrunk_with_summary > 0


# --- T-02 の骨格レコード（BUG-0078） ------------------------------------------------------------------


def _skeleton_records() -> list[Record]:
    """T-02 の骨格レコード 2 件（クラス 3 つ・doc・非公開・入れ子の関数・デコレータ。2 件目は小さい）。"""
    import hashlib

    from lucianke.syntax.parsers import parse_source
    from lucianke.syntax.skeleton import build_file_record
    from lucianke.syntax.support import SUPPORT
    from lucianke.syntax.symbols import extract, load_query_source
    from lucianke.syntax.types import SkeletonOptions

    def method(c: int, m: int) -> str:
        doc = f'        """Method {m} of class {c}."""\n' if m % 2 == 0 else ""
        name = f"_helper_{m}" if m % 5 == 4 else f"method_{m}"
        return f"    @property\n    def {name}(self, a: int, b: str = 'x') -> int:\n{doc}        return a\n\n"

    big = "".join(
        f'class Klass{c}:\n    """Class {c}."""\n\n' + "".join(method(c, m) for m in range(4 + 3 * c)) for c in range(3)
    )
    big += 'def outer(x: int) -> int:\n    """Outer."""\n    def inner(y: int) -> int:\n        return y\n    return inner(x)\n'
    small = 'def tiny(a: int) -> int:\n    """Tiny."""\n    return a\n'
    records = []
    for i, (rel, text) in enumerate((("pkg/big.py", big), ("pkg/small.py", small))):
        source = text.encode("utf-8")
        parsed, _ = parse_source(source, "python", hashlib.sha256(source).hexdigest())
        symbols = extract(
            parsed, language_id="python", language=SUPPORT["python"].grammar(),
            query_source=load_query_source("python"), workspace_id="ws", rel=rel,  # type: ignore[arg-type]
        )  # fmt: skip
        records.append(build_file_record(parsed, symbols, rel=rel, opts=SkeletonOptions(), priority=i))
    return records


def _symbol_names(rec: Record) -> set[str]:
    out: set[str] = set()
    stack = list(rec.data["symbols"])
    while stack:
        s = stack.pop()
        out.add(s["qualified_name"])
        stack.extend(s["children"])
    return out


def test_t02_skeleton_pages_are_monotone_and_fill_the_budget() -> None:
    """T-02 の骨格（BUG-0078）: 予算を上げても各ファイルの中身（シンボル・doc: 行）は減らず、縮退したファイルでページを
    閉じるときは予算をほぼ使い切る（1 段は doc: 行 1 件かメンバー 1 件ぶん）。compact と json（``shrink_json``）の両方。
    以前は入れ子を一度に落とす段しか無く、F-PY の ``_urls.py`` は予算 6000 の json で 618 トークンしか使わなかった。"""
    from lucianke.mcp.render import json_out

    records = _skeleton_records()
    for form, kw, step in (
        ("compact", {}, 40),
        ("json", {"record_text": json_out.record_text, "trailer_text": json_out.trailer_text}, 260),
    ):
        measure = kw.get("record_text") or (lambda r: "\n".join(r.lines) + "\n")
        lo = 120 if form == "compact" else 400
        hi = sum(EST.count(measure(r)) for r in records) + 60
        budgets = range(lo, hi, 7)
        pages = _scan(records, budgets=budgets, reserved=30, form=form, context=form, **kw)  # type: ignore[arg-type]
        filled = 0
        for b, a, page in zip(budgets[1:], pages, pages[1:], strict=False):
            for x, y in zip(a.records, page.records, strict=False):
                assert _symbol_names(y) >= _symbol_names(x), (form, b, x.key)
                assert {ln for ln in y.lines if "doc: " in ln} >= {ln for ln in x.lines if "doc: " in ln}, (form, b)
            last = page.records[-1] if page.records else None
            if last is not None and last.lines != records[len(page.records) - 1].lines:  # 縮退版でページを閉じた
                assert page.max_tokens - page.used_tokens_est < step, (form, b, page.used_tokens_est)
                filled += 1
        assert filled > 20, form
