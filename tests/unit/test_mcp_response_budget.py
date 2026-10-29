"""応答全体のトークン計上（仕様書 §5.6-4・§5.7、設計書 §7.2.2、ADR-4。BUG-0014・BUG-0017）。

パイプラインに本物のパッカーとレンダラ（compact・json）を通し、**描画後の全文**を ``TokenEstimator`` で数え直した値で次を確かめる。

* 全文が ``max_tokens`` 以下（ヘッダ・警告・降格の警告行・``head_kv``・トレーラ・json の封筒もすべて予算に入る）
* ヘッダの ``~used`` ・json の ``budget.used_tokens_est`` が数え直しと一致する（誤差 0。``mcp/fit.py`` が数え直して合わせる）
* json は ``Record.lines`` ではなく ``Record.data`` の大きさで詰める（compact の約 1/7 に過小に数えない）
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, EngineChoice
from lucianke.core.records import Record, ToolOutput
from lucianke.core.tokens import TokenEstimator
from lucianke.mcp.budget import Packer
from lucianke.mcp.pipeline import Pipeline, default_renderers
from lucianke.mcp.registry import CommonArgs, ToolRegistry, ToolSpec
from lucianke.mcp.render import json_out

EST = TokenEstimator().count
HEADER_USED = re.compile(r"^# \S+ ok .* ~(\d+)/(\d+)tok$")


class Args(CommonArgs):
    n: int = 5
    variant: str = "plain"  # plain | extras（head_kv と警告）| degraded（降格の警告行）
    boom: bool = False  # E_TIMEOUT の部分結果を返す


def _record(i: int, conf: Confidence) -> Record:
    """compact は 2 行（約 30 トークン）、json は入れ子の ``data``（約 200 トークン）になるレコード。"""
    return Record(
        kind="symbol",
        key=f"s{i}",
        priority=i,
        group=f"src/pkg/mod_{i % 3}.py",
        confidence=conf,
        lines=(f"fn_{i}(a, b) -> int  # 結果を返す関数 {i}", f"  doc: request {i} を処理する"),
        data={
            "name": f"fn_{i}",
            "urn": f"lk://app/src/pkg/mod_{i % 3}.py#Service.fn_{i}",
            "range": {"start": {"line": i, "character": 4}, "end": {"line": i + 9, "character": 0}},
            "signature": f"def fn_{i}(a: int, b: int) -> int",
            "doc": f"request {i} を処理して結果を返す。失敗したら例外を投げる。",
            "children": [
                {"name": f"inner_{i}_{j}", "kind": "function", "line": i + j, "modifiers": ["pub", "async"]}
                for j in range(3)
            ],
        },
    )


def _output(a: Args) -> ToolOutput:
    degraded = a.variant == "degraded"
    conf = Confidence.STRUCTURAL if degraded else Confidence.EXACT
    return ToolOutput(
        records=[_record(i, conf) for i in range(a.n)],
        max_confidence=Confidence.EXACT,
        engine=EngineChoice(engine="tree-sitter" if degraded else "pyright+tree-sitter", confidence=conf,
                            lsp_status="unavailable" if degraded else "ready"),
        page_unit="symbol",
        head_kv=[("target", "lk://app/src/pkg/mod_0.py#Service"), ("rows", f"{a.n}")] if a.variant == "extras" else (),
        data_static={"target": "lk://app/src/pkg/mod_0.py#Service", "totals": {"symbols": a.n}},
        warnings=("引数 x は無視されました", "1 of 3 items failed (E_NOT_FOUND)") if a.variant == "extras" else (),
        tier=1,
        detected_language="python",
    )


async def _handler(ctx: Any, a: Args) -> ToolOutput:
    out = _output(a)
    if a.boom:
        raise LuciankeError(ErrorCode.E_TIMEOUT, "LSP did not answer in time", partial=out)
    return out


def _pipeline() -> Pipeline:
    spec = ToolSpec(name="probe_tool", tid="T-XX", args_model=Args, handler=_handler, default_max_tokens=2000,
                    records_key="symbols")  # fmt: skip
    return Pipeline(ToolRegistry([spec]), renderers=default_renderers())


class Reply:
    """1 回の応答。``text`` は ``content[0].text``。json は ``body`` に読み直した封筒を持つ。"""

    def __init__(self, rendered: Any, fmt: str) -> None:
        self.rendered = rendered
        self.text: str = rendered.text
        self.fmt = fmt
        self.recount = EST(self.text)
        self.body: dict[str, Any] | None = json.loads(self.text) if fmt == "json" else None

    def used_matches_recount(self) -> bool:
        """申告は数え直し以上で、数え直しに一致する。json は ``timing_ms`` を最大の桁数（5 桁）で数えるので、実際の全文より
        最大 4 トークン（桁数の差。切り上げで 5）多い。同じ入力なら処理時間にかかわらず同じ申告になる。"""
        return self.recount <= self.used <= self.recount + (5 if self.fmt == "json" else 0)

    @property
    def used(self) -> int:
        """応答が自分で申告する使用量（compact: ヘッダの ``~used``、json: ``budget.used_tokens_est``）。"""
        if self.body is not None:
            return int(self.body["budget"]["used_tokens_est"])
        m = HEADER_USED.match(self.text.splitlines()[0])
        assert m, self.text.splitlines()[0]
        return int(m.group(1))

    @property
    def returned(self) -> int:
        if self.body is not None:
            return len(self.body["data"]["symbols"])
        return len(re.findall(r"^fn_\d+\(", self.text, flags=re.MULTILINE))

    @property
    def next_cursor(self) -> str | None:
        if self.body is not None:
            return self.body["page"]["next_cursor"]
        m = re.search(r"^# more: cursor=(\S+)", self.text, flags=re.MULTILINE)
        return m.group(1) if m else None


async def _call(fmt: str, max_tokens: int, **args: Any) -> Reply:
    rendered = await _pipeline().call_tool("probe_tool", {"format": fmt, "max_tokens": max_tokens, **args})
    return Reply(rendered, fmt)


# --- 全文が予算に収まり、申告が数え直しに一致する --------------------------------------------------------------


@pytest.mark.parametrize("variant", ["plain", "extras", "degraded"])
@pytest.mark.parametrize("n", [0, 1, 40])
@pytest.mark.parametrize("max_tokens", [200, 400, 2000])
@pytest.mark.parametrize("fmt", ["compact", "json"])
async def test_whole_response_fits_max_tokens_and_used_matches_recount(fmt, max_tokens, n, variant):
    r = await _call(fmt, max_tokens, n=n, variant=variant)
    # 例外: json で降格（日本語の accuracy_warning）、最小の max_tokens=200、レコードが 1 件も入らない応答は、封筒だけで約 220 になる
    #（前置きだけで予算を超える。申告 = 数え直しは守る）
    envelope_only = fmt == "json" and max_tokens == 200 and variant == "degraded" and r.returned == 0 and n > 0
    assert envelope_only or r.recount <= max_tokens, f"全文 {r.recount} トークンが max_tokens={max_tokens} を超えた:\n{r.text[:600]}"
    assert r.used_matches_recount()  # ヘッダの ~used ・json の used_tokens_est は全文の数え直しに一致（BUG-0014・BUG-0017）
    if r.body is not None:
        assert r.rendered.structured["budget"] == r.body["budget"]
        assert r.body["budget"]["max_tokens"] == max_tokens
    else:
        assert HEADER_USED.match(r.text.splitlines()[0]).group(2) == str(max_tokens)


@pytest.mark.parametrize("fmt", ["compact", "json"])
async def test_every_input_cost_is_counted_header_warnings_head_kv_banner(fmt):
    """前置きだけで予算の大半を使うとき（降格の警告 + 警告 + head_kv）、その分だけレコードが減る。"""
    plain = await _call(fmt, 2000, n=120, variant="plain")
    heavy = await _call(fmt, 2000, n=120, variant="extras")
    assert heavy.returned <= plain.returned < 120  # どちらも予算で切れている。前置きが長い分、入る件数は減る（json は 1 件が大きい）
    assert fmt == "json" or heavy.returned < plain.returned
    assert heavy.recount <= 2000 and plain.recount <= 2000
    assert heavy.next_cursor and plain.next_cursor


# --- json はレコードの data で詰める ----------------------------------------------------------------------


async def test_json_counts_record_data_not_compact_lines():
    """同じ max_tokens で、json に入るレコード数は compact よりずっと少ない（1 件が data の大きさだから）。"""
    compact = await _call("compact", 2000, n=40)
    as_json = await _call("json", 2000, n=40)
    assert as_json.returned >= 1
    assert as_json.returned * 3 < compact.returned
    assert as_json.recount <= 2000
    # 入った分の data を JSON にした大きさは、申告した使用量のほぼ全部（封筒の分を除いて 1 割以内に収まる）
    data_cost = sum(EST(json_out.record_text(_record(i, Confidence.EXACT))) for i in range(as_json.returned))
    assert data_cost <= as_json.used
    assert data_cost >= as_json.used * 0.6


async def test_json_used_tokens_est_is_the_recount_of_the_json_text():
    """BUG-0017: 申告 1884 に対し JSON 本文が 14192（約 7 倍）だった。数え直しとの比は 1.0。"""
    r = await _call("json", 8000, n=40)
    assert r.used_matches_recount()
    assert r.recount > 3000  # 実際に大きな JSON が返っている（compact なら同じ件数で 1500 前後）
    assert r.recount <= 8000


# --- ページを辿ると重複も欠落も無く、どのページも予算内 ------------------------------------------------------


@pytest.mark.parametrize("fmt, max_tokens", [("compact", 400), ("compact", 1200), ("json", 600), ("json", 2000)])
async def test_paging_by_cursor_covers_all_records_and_every_page_fits(fmt, max_tokens):
    n = 60
    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        args: dict[str, Any] = {"n": n}
        if cursor:
            args["cursor"] = cursor
        r = await _call(fmt, max_tokens, **args)
        assert r.recount <= max_tokens and r.used_matches_recount()
        names = (
            [int(s["name"].removeprefix("fn_")) for s in r.body["data"]["symbols"]]
            if r.body is not None
            else [int(m) for m in re.findall(r"^fn_(\d+)\(", r.text, flags=re.MULTILINE)]
        )
        assert names, "1 件も進まないページ"
        seen += names
        pages += 1
        cursor = r.next_cursor
        if cursor is None:
            break
        assert pages < 80
    assert seen == list(range(n))
    assert pages > 1


# --- 失敗応答の部分結果（E_TIMEOUT）も同じ予算に入る -----------------------------------------------------------


@pytest.mark.parametrize("fmt", ["compact", "json"])
@pytest.mark.parametrize("max_tokens", [400, 2000])
async def test_partial_result_of_error_is_packed_within_budget(fmt, max_tokens):
    rendered = await _pipeline().call_tool("probe_tool", {"format": fmt, "max_tokens": max_tokens, "n": 40, "boom": True})
    assert rendered.is_error
    assert EST(rendered.text) <= max_tokens
    if fmt == "json":
        body = json.loads(rendered.text)
        assert body["error"]["code"] == "E_TIMEOUT" and body["data"] is not None
        assert EST(rendered.text) <= body["budget"]["used_tokens_est"] <= EST(rendered.text) + 5  # timing_ms は最大桁数で数える


# --- パッカーの形式フック（record_text / trailer_text） ---------------------------------------------------------


def test_packer_measures_records_with_the_given_record_text():
    recs = [_record(i, Confidence.EXACT) for i in range(300)]
    out = ToolOutput(records=recs, max_confidence=Confidence.EXACT,
                     engine=EngineChoice(engine="x", confidence=Confidence.EXACT), page_unit="symbol")  # fmt: skip
    default = Packer().pack(out, max_tokens=3000, cursor_factory=lambda off: f"c{off}")
    as_json = Packer().pack(out, max_tokens=3000, cursor_factory=lambda off: f"c{off}",
                            record_text=json_out.record_text, trailer_text=json_out.trailer_text)  # fmt: skip
    assert len(as_json.records) * 3 < len(default.records) < 300
    spent = sum(EST(json_out.record_text(r)) for r in as_json.records)
    assert spent <= as_json.used_tokens_est <= 3000
    assert as_json.next_cursor == f"c{len(as_json.records)}"
    assert as_json.remaining == 300 - len(as_json.records)
    assert as_json.summary is not None  # 要約は作る（json は描かないが、トレーラの見積もりは json の関数が決める）


def test_packer_shrink_is_rechecked_with_the_given_record_text():
    """``shrink`` は compact の行の大きさで収める約束。別の形式で数えて収まらなければ、``ask`` を二分探索して収まる最大の縮退版を探す。"""
    asked: list[int] = []

    def make(words: int) -> Record:
        # compact の行は data の 1/4 の語数（json は compact の約 4 倍）。縮退版は渡された budget に compact で収まる
        def shrink(budget: int) -> Record | None:
            asked.append(budget)
            return make(int(budget * 4.5))

        return Record(kind="file-block", key="f", priority=0, confidence=Confidence.EXACT, group="f",
                      lines=("word " * (words // 4),), data={"blob": "word " * words}, shrink=shrink)  # fmt: skip

    out = ToolOutput(records=[make(10_000)], max_confidence=Confidence.EXACT,
                     engine=EngineChoice(engine="x", confidence=Confidence.EXACT), page_unit="file")  # fmt: skip
    compact = Packer().pack(out, max_tokens=300)
    assert len(compact.records) == 1 and compact.used_tokens_est <= 300
    assert len(asked) == 1  # compact は従来どおり 1 回だけ呼ぶ

    asked.clear()
    page = Packer().pack(out, max_tokens=300, record_text=json_out.record_text, trailer_text=json_out.trailer_text)
    assert len(page.records) == 1
    assert EST(json_out.record_text(page.records[0])) <= 300
    assert page.used_tokens_est <= 300
    assert len(asked) > 1 and asked[0] == max(asked)  # 1 回目の縮退版は json で収まらず、``ask`` を縮めて呼び直した
    assert EST(json_out.record_text(page.records[0])) >= 250  # 収まる最大に近い（極端に小さい縮退版で済ませない）
