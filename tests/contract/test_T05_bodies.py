"""T-05 ``get_symbol_bodies`` の契約テスト（仕様書 §11 T-05 の事後条件 1・2・5 ほか）。

パイプライン経由でツールを呼ぶ。ルートは ``tests/fixtures/poly``。構文木だけ（``structural``）の縦切りの試験で、
``cell:<n>``（事後条件 3）と ``lexical_fallback`` の窓（事後条件 4）は BUG-0049 で結線した。セレクタ ``xpath:`` など
はツールが未対応（``E_UNSUPPORTED``）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.app import build_services
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import register_tools

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAY_PY = "src/shop/services/payment.py"
PAY_TS = "web/src/payment.ts"
WS = "poly"


@pytest.fixture
def pipeline() -> Pipeline:
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=build_services(lambda: [str(POLY)]))


async def call(pipeline: Pipeline, fmt: str = "compact", **args: Any):
    return await pipeline.call_tool("get_symbol_bodies", {"format": fmt, **args})


NOTEBOOK = "notebooks/eda.ipynb"
COBOL = "legacy/calc.cob"


def source_lines(path: str) -> list[str]:
    return (POLY / path).read_text(encoding="utf-8").splitlines()


def _strings(node: Any) -> list[str]:
    """JSON の値にある文字列（キーは除く）をすべて集める。"""
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    return []


async def test_tools_list_has_t05(pipeline: Pipeline) -> None:
    assert "get_symbol_bodies" in [d.name for d in pipeline.registry.descriptors()]


@pytest.mark.parametrize(
    "ref",
    [
        {"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"},
        {"path": PAY_PY, "symbol_name": "process_charge"},  # 末尾一致
        {"path": PAY_PY, "line": 45},  # その行を含む最も内側のシンボル
        {"urn": f"lk://{WS}/{PAY_PY}#PaymentProcessor.process_charge"},
    ],
)
async def test_target_forms_resolve_to_the_same_method(pipeline: Pipeline, ref: dict[str, Any]) -> None:
    r = await call(pipeline, "json", targets=[ref])
    assert not r.is_error, r.text
    item = r.structured["data"]["items"][0]
    assert item["ok"] and item["symbol"]["qualified_name"] == "PaymentProcessor.process_charge"
    assert item["symbol"]["kind"] == "Method"


async def test_postcondition_1_code_lines_equal_file_lines(pipeline: Pipeline) -> None:
    """事後条件 1: code の各行は、range_returned.start_line〜end_line の対象ファイルの各行と順に一致する。"""
    targets = [
        {"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"},
        {"path": PAY_PY, "symbol_name": "PaymentRequest"},
        {"path": PAY_TS, "symbol_name": "PaymentClient"},
    ]
    r = await call(pipeline, "json", targets=targets)
    assert not r.is_error
    for t, item in zip(targets, r.structured["data"]["items"], strict=True):
        rr = item["range_returned"]
        expected = source_lines(t["path"])[rr["start_line"] - 1 : rr["end_line"]]
        assert item["code"].split("\n") == expected, t
        assert len(item["file_sha256"]) == 64 and item["confidence_level"] == "structural"


async def test_postcondition_2_line_numbers_are_consecutive(pipeline: Pipeline) -> None:
    """事後条件 2: with_line_numbers: true のとき、各行の @L<n> は range_returned 内の連番。"""
    r = await call(pipeline, targets=[{"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"}])
    assert g.validate(r.text) == []
    numbers = [int(m.group(1)) for m in re.finditer(r"^@L(\d+): ", r.text, re.M)]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers))) and len(numbers) >= 10
    assert "  sha: " in r.text
    j = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"}])
    rr = j.structured["data"]["items"][0]["range_returned"]
    assert (numbers[0], numbers[-1]) == (rr["start_line"], rr["end_line"])


async def test_without_line_numbers(pipeline: Pipeline) -> None:
    r = await call(pipeline, targets=[{"path": PAY_PY, "symbol_name": "PaymentRequest"}], with_line_numbers=False)
    assert "@L" in r.text.splitlines()[1] and not re.search(r"^@L\d+: ", r.text, re.M)
    assert g.validate(r.text) == []


async def test_decorators_and_context_lines(pipeline: Pipeline) -> None:
    """範囲は宣言ノード + デコレータ・直前のコメント + context_lines。context_lines=0 なら前置きなし。"""
    base = {"path": PAY_PY, "symbol_name": "PaymentRequest"}
    r0 = await call(pipeline, "json", targets=[base], context_lines=0)
    r2 = await call(pipeline, "json", targets=[base], context_lines=2)
    i0, i2 = (x.structured["data"]["items"][0] for x in (r0, r2))
    assert i0["context_before_lines"] == 0
    assert i2["range_returned"]["end_line"] == i0["range_returned"]["end_line"]
    assert i2["range_returned"]["start_line"] == i0["range_returned"]["start_line"] - i2["context_before_lines"]
    assert i0["code"].lstrip().startswith("@dataclass")  # デコレータを含む


async def test_line_range_is_clamped_to_the_body(pipeline: Pipeline) -> None:
    ref = {"path": PAY_PY, "symbol_name": "PaymentProcessor.process_charge"}
    full = (await call(pipeline, "json", targets=[ref], context_lines=0)).structured["data"]["items"][0]
    start = full["range_returned"]["start_line"]
    part = await call(pipeline, "json", targets=[ref], line_range={"start": 1, "end": start + 2})
    item = part.structured["data"]["items"][0]
    assert item["range_returned"] == {"start_line": start, "end_line": start + 2}
    assert len(item["code"].split("\n")) == 3


async def test_ambiguous_symbol_name_lists_candidates(pipeline: Pipeline) -> None:
    """§5.3: 末尾一致の候補が 2 件以上なら E_TARGET_AMBIGUOUS（候補つき）。TS の charge は 3 つのオーバーロード。"""
    r = await call(pipeline, "json", targets=[{"path": PAY_TS, "symbol_name": "charge"}])
    assert r.is_error
    err = r.structured["error"]
    assert err["code"] == "E_TARGET_AMBIGUOUS"
    assert len(err["candidates"]) == 3 and all(c["urn"].startswith("lk://") for c in err["candidates"])


async def test_partial_failure(pipeline: Pipeline) -> None:
    """§5.4.4: 1 件以上成功すれば ok。失敗項目は `! <target>: <CODE> <message>`。"""
    r = await call(
        pipeline,
        targets=[
            {"path": PAY_PY, "symbol_name": "PaymentRequest"},
            {"path": PAY_PY, "symbol_name": "NoSuchThing"},
            {"path": "missing.py", "symbol_name": "x"},
            {"path": PAY_PY, "symbol_name": "xpath:/a/b"},
            {"path": PAY_PY},
        ],
    )
    assert not r.is_error and g.validate(r.text) == []
    assert f"! {PAY_PY}#NoSuchThing: E_TARGET_NOT_FOUND" in r.text
    assert "! missing.py#x: E_TARGET_NOT_FOUND" in r.text
    assert f"! {PAY_PY}#xpath:/a/b: E_UNSUPPORTED" in r.text
    assert "E_INVALID_ARGUMENT" in r.text


async def test_all_failed_is_an_error_with_the_first_item(pipeline: Pipeline) -> None:
    r = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "Nope"}, {"path": "m.py", "line": 1}])
    assert r.is_error
    err = r.structured["error"]
    assert err["code"] == "E_TARGET_NOT_FOUND"
    assert [i["error"]["code"] for i in err["details"]["items"]] == ["E_TARGET_NOT_FOUND"] * 2


@pytest.mark.parametrize("n", [0, 21])
async def test_target_count_bounds(pipeline: Pipeline, n: int) -> None:
    r = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "PaymentRequest"}] * n)
    assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


@pytest.mark.parametrize("bad", [{"context_lines": 21}, {"line_range": {"start": 5, "end": 2}}])
async def test_invalid_arguments(pipeline: Pipeline, bad: dict[str, Any]) -> None:
    r = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "PaymentRequest"}], **bad)
    assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT"


@pytest.mark.parametrize("max_tokens", [200, 300, 3000])
async def test_postcondition_5_budget_is_respected(pipeline: Pipeline, max_tokens: int) -> None:
    targets = [
        {"path": PAY_PY, "symbol_name": "PaymentProcessor"},
        {"path": PAY_TS, "symbol_name": "PaymentClient"},
        {"path": PAY_PY, "symbol_name": "PaymentRequest"},
    ]
    r = await call(pipeline, "json", targets=targets, max_tokens=max_tokens)
    assert not r.is_error
    assert r.structured["budget"]["used_tokens_est"] <= max_tokens
    items = r.structured["data"]["items"]
    for item in items:
        if item.get("truncated"):  # 切ったときは続きの位置を記録する
            assert item["truncated_at_line"] > item["range_returned"]["end_line"]


async def test_truncated_body_is_a_prefix_and_tells_where(pipeline: Pipeline) -> None:
    ref = {"path": PAY_PY, "symbol_name": "PaymentProcessor"}
    full = (await call(pipeline, "json", targets=[ref])).structured["data"]["items"][0]
    # json は封筒（_resolution_meta など）と data の全部を数えるので、compact の 300 より大きい予算で切る（BUG-0017）
    small = await call(pipeline, "json", targets=[ref], max_tokens=600)
    items = small.structured["data"]["items"]
    assert items, small.text
    item = items[0]
    assert item["truncated"] is True
    assert full["code"].startswith(item["code"])
    assert item["truncated_at_line"] == item["range_returned"]["end_line"] + 1
    text = (await call(pipeline, targets=[ref], max_tokens=300)).text
    assert g.validate(text) == []
    assert "truncated at line" in text


async def test_revision_on_a_workspace_without_git_is_revision_not_found(pipeline: Pipeline) -> None:
    """poly は Git のワーキングコピーではない。過去断面の読み取りは tests/contract/test_revision_reads.py（BUG-0018）。"""
    r = await call(pipeline, "json", targets=[{"path": PAY_PY, "symbol_name": "PaymentRequest"}], revision="HEAD")
    assert r.is_error and r.structured["error"]["code"] == "E_REVISION_NOT_FOUND"


async def test_partial_failure_counts_failed_items_in_warnings(pipeline: Pipeline) -> None:
    """§5.4.4: 失敗項目の件数を warnings[] に書く（compact は `! ` 行）。全件成功なら warnings は空。"""
    targets = [{"path": PAY_PY, "symbol_name": "PaymentRequest"}, {"path": PAY_PY, "symbol_name": "NoSuchThing"}]
    j = await call(pipeline, "json", targets=targets)
    assert not j.is_error and j.structured["ok"] is True
    assert len(j.structured["warnings"]) == 1 and j.structured["warnings"][0].startswith("1 of 2 items failed")
    r = await call(pipeline, targets=targets)
    assert g.validate(r.text) == [] and any(ln.startswith("! 1 of 2 items failed") for ln in r.text.splitlines()[:3])
    assert (await call(pipeline, "json", targets=targets[:1])).structured["warnings"] == []


async def test_header_shows_the_resolved_workspace_id_when_omitted(pipeline: Pipeline) -> None:
    """§5.4.2・§5.2: workspace_id 省略でも、ヘッダの ws= は解決後の ID（`-` ではない）。"""
    r = await call(pipeline, targets=[{"path": PAY_PY, "symbol_name": "PaymentRequest"}])
    header = r.text.splitlines()[0]
    assert " ws=poly " in header and " ws=- " not in header


# --- 予算に収まらない本体の続き（仕様書 §11 T-05 規約 7、BUG-0016） -------------------------------------


async def _drain(pipeline: Pipeline, targets: list[dict[str, Any]], max_tokens: int, **extra: Any) -> list[list[dict[str, Any]]]:
    """``next_cursor`` を辿って全ページの items を集める。各ページで予算・カーソルと truncated の整合を検める。"""
    pages: list[list[dict[str, Any]]] = []
    cursor: str | None = None
    for _ in range(120):
        args: dict[str, Any] = {"targets": targets, "max_tokens": max_tokens, **extra}
        if cursor is not None:
            args["cursor"] = cursor
        r = await call(pipeline, "json", **args)
        assert not r.is_error, r.text
        s = r.structured
        assert s["budget"]["used_tokens_est"] <= max_tokens
        items = s["data"]["items"]
        assert items, "進まないページ"
        pages.append(items)
        cursor = s["page"]["next_cursor"]
        assert (cursor is not None) is s["budget"]["truncated"], "続きがあるのに budget.truncated / next_cursor が揃わない"
        if items[-1].get("truncated"):
            assert cursor is not None, "本体を切ったのに next_cursor が無い"
        if cursor is None:
            return pages
    raise AssertionError("ページが終わらない")


async def _full(pipeline: Pipeline, ref: dict[str, Any], **extra: Any) -> dict[str, Any]:
    r = await call(pipeline, "json", targets=[ref], max_tokens=20000, **extra)
    assert not r.is_error, r.text
    item = r.structured["data"]["items"][0]
    assert item["truncated"] is False
    return item


def _assert_contiguous(parts: list[dict[str, Any]], full: dict[str, Any]) -> None:
    """切れ切れの項目が、重複も隙間もなく本体全体になる（行番号が連続し、連結が全体とバイト一致する）。"""
    assert parts[0]["range_returned"]["start_line"] == full["range_returned"]["start_line"]
    for prev, nxt in zip(parts, parts[1:], strict=False):
        assert prev["truncated"] is True
        assert prev["truncated_at_line"] == prev["range_returned"]["end_line"] + 1
        assert nxt["range_returned"]["start_line"] == prev["range_returned"]["end_line"] + 1
        assert nxt["context_before_lines"] == 0  # 続きに前置きは付かない
    assert parts[-1]["truncated"] is False
    assert parts[-1]["range_returned"]["end_line"] == full["range_returned"]["end_line"]
    assert "\n".join(p["code"] for p in parts) == full["code"]


@pytest.mark.parametrize("context_lines", [0, 2])
# json は封筒（約 150 トークン）も data の全項目（1 件あたり約 280 トークンの固定分）も数える。全文は約 670 トークンで、
# 本体を 1 行も入れられない予算（約 480 未満）と全文が入る予算（約 670 以上）の間で切り詰めが起きる（BUG-0017）
@pytest.mark.parametrize("max_tokens", [520, 580, 640])
async def test_cursor_continues_a_truncated_body_without_overlap_or_gap(
    pipeline: Pipeline, max_tokens: int, context_lines: int
) -> None:
    """予算を変えて切り詰め → next_cursor で続きを取る → 連結すると本体全体とバイト一致（重複も隙間もない）。"""
    ref = {"path": PAY_PY, "symbol_name": "PaymentProcessor"}
    full = await _full(pipeline, ref, context_lines=context_lines)
    pages = await _drain(pipeline, [ref], max_tokens, context_lines=context_lines)
    assert len(pages) >= 2, "この予算では切り詰めが起きない（テストの前提）"
    assert all(len(items) == 1 for items in pages)
    _assert_contiguous([items[0] for items in pages], full)


async def test_truncation_never_lands_inside_the_context_lines(tmp_path: Path) -> None:
    """前置き（context_lines）の途中では切らない: 切った版には本体の先頭の行が必ず含まれる（続きに隙間ができない）。"""
    src = [f"CONST_{i} = {i}" for i in range(20)] + ["def big(x):"]  # 前置きの 20 行（空行でない）が本体の直前に付く
    src += [f"    value_{i} = x + {i}  # 本体の {i} 行目" for i in range(60)] + ["    return x"]
    (tmp_path / "m.py").write_text("\n".join(src) + "\n", encoding="utf-8")
    registry = ToolRegistry()
    register_tools(registry)
    pipeline = Pipeline(registry, services=build_services(lambda: [str(tmp_path)]))
    ref = {"path": "m.py", "symbol_name": "big"}
    full = await _full(pipeline, ref, context_lines=20)
    assert full["context_before_lines"] == 20 and full["range_returned"]["start_line"] == 1
    checked = 0
    for max_tokens in range(430, 750, 17):  # 切り口が前置きの中に落ちうる小さな予算まで（json の封筒の分だけ大きい）
        r = await call(pipeline, "json", targets=[ref], max_tokens=max_tokens, context_lines=20)
        assert not r.is_error, r.text
        items = r.structured["data"]["items"]
        if not items:  # 1 行も入らない予算（BUG-0015 の領分）
            continue
        if items[0]["truncated"]:
            checked += 1
            assert items[0]["range_returned"]["end_line"] >= 21, f"本体の先頭の行が無い: max_tokens={max_tokens}"
            pages = await _drain(pipeline, [ref], max_tokens, context_lines=20)
            _assert_contiguous([p[0] for p in pages], full)
    assert checked >= 5


async def test_continue_with_line_range_gets_the_rest_and_the_note_says_how(pipeline: Pipeline) -> None:
    """続きの取り方が応答にある: json は continue_with.line_range、compact は注記と `# more: cursor=`。それで続きが取れる。"""
    ref = {"path": PAY_PY, "symbol_name": "PaymentProcessor"}
    full = await _full(pipeline, ref, context_lines=0)
    first = (await call(pipeline, "json", targets=[ref], max_tokens=600, context_lines=0)).structured["data"]["items"][0]
    assert first["truncated"] is True
    hint = first["continue_with"]["line_range"]
    assert hint == {"start": first["truncated_at_line"], "end": full["range_returned"]["end_line"]}
    rest = (await call(pipeline, "json", targets=[ref], max_tokens=20000, line_range=hint)).structured["data"]["items"][0]
    assert rest["truncated"] is False and rest["range_returned"]["start_line"] == hint["start"]
    assert first["code"] + "\n" + rest["code"] == full["code"]
    text = (await call(pipeline, targets=[ref], max_tokens=300, context_lines=0)).text  # compact は行で数えるので、切り口は json と違う
    assert g.validate(text) == []
    m = re.search(r"truncated at line (\d+) of (\d+): continue with the cursor, or line_range (\d+)-(\d+)", text)
    assert m and m.group(1) == m.group(3) and int(m.group(2)) == int(m.group(4)) == hint["end"], text
    assert "# more: cursor=" in text


async def test_truncated_target_is_not_followed_by_the_next_target_until_it_is_complete(pipeline: Pipeline) -> None:
    """複数対象: 切った対象の後ろに次の対象を詰めない。続きを辿ると対象ごとに本体全体になる。"""
    refs = [
        {"path": PAY_PY, "symbol_name": "PaymentProcessor"},
        {"path": PAY_PY, "symbol_name": "PaymentRequest"},
        {"path": PAY_TS, "symbol_name": "PaymentClient"},
    ]
    fulls = [await _full(pipeline, ref, context_lines=0) for ref in refs]
    pages = await _drain(pipeline, refs, 580, context_lines=0)  # json の最小の 1 ページ（封筒 + 1 件の固定分 + 本体 1 行）が入る予算
    flat = [item for items in pages for item in items]
    for items in pages:
        assert all(not it["truncated"] for it in items[:-1]), "切った対象の後ろに別の対象が詰まっている"
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for item in flat:
        by_symbol.setdefault(item["symbol"]["qualified_name"], []).append(item)
    assert list(by_symbol) == [f["symbol"]["qualified_name"] for f in fulls]  # 対象の順を保つ
    for parts, full in zip(by_symbol.values(), fulls, strict=True):
        _assert_contiguous(parts, full)


# --- セレクタ・lexical_fallback（事後条件 3・4。BUG-0049 で結線） ------------------------------------------


async def test_t05_postcondition_3_cell_item_is_the_cell_source_without_outputs(pipeline: Pipeline) -> None:
    """PC:T-05#3 ``cell:<n>`` の項目の ``code`` は ``cells[n].source`` の連結と一致し、``outputs`` 内の文字列を含まない。

    ``cell:3`` は 1 始まりで ``cells[2]``（Markdown も数える）。期待値は Notebook JSON から取る。
    """
    doc = json.loads((POLY / NOTEBOOK).read_text(encoding="utf-8"))
    outputs = {
        part.strip()
        for cell in doc["cells"]
        for s in _strings(cell.get("outputs", []))
        for part in s.splitlines()
        if len(part.strip()) >= 8
    }
    assert len(outputs) >= 3
    r = await call(pipeline, "json", targets=[{"path": NOTEBOOK, "symbol_name": "cell:3"}])
    assert not r.is_error, r.text
    item = r.structured["data"]["items"][0]
    assert item["ok"] is True
    assert item["code"] == "".join(doc["cells"][2]["source"])
    assert [s for s in outputs if s in item["code"]] == []


async def test_t05_postcondition_4_lexical_fallback_windows_are_at_most_31_lines_and_degraded(
    pipeline: Pipeline,
) -> None:
    """PC:T-05#4 ``lexical_fallback`` の各窓は ``end_line - start_line + 1 ≤ 31`` で、応答は ``is_degraded: true``。"""
    r = await call(pipeline, "json", targets=[{"path": COBOL, "symbol_name": "COMPUTE-TAX"}])
    assert not r.is_error, r.text
    assert r.structured["_resolution_meta"]["is_degraded"] is True
    item = r.structured["data"]["items"][0]
    assert item["ok"] is True and item["confidence_level"] == "lexical_fallback"
    windows = item["windows"]
    assert 1 <= len(windows) <= 3
    assert all(w["end_line"] - w["start_line"] + 1 <= 31 for w in windows)


# --- BUG-0049: cell: の周辺・Tier 4 の窓の周辺 -------------------------------------------------------------


def _tmp_pipeline(root: Path) -> Pipeline:
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=build_services(lambda: [str(root)]))


async def test_cell_selector_errors_are_per_item(pipeline: Pipeline) -> None:
    """構文の誤りは E_INVALID_ARGUMENT、範囲外は E_TARGET_NOT_FOUND、.ipynb 以外への cell: は E_UNSUPPORTED。"""
    targets = [
        {"path": NOTEBOOK, "symbol_name": "cell:0"},
        {"path": NOTEBOOK, "symbol_name": "cell:x"},
        {"path": NOTEBOOK, "symbol_name": "cell:99"},
        {"path": PAY_PY, "symbol_name": "cell:1"},
        {"path": COBOL, "symbol_name": "cell:1"},
        {"path": NOTEBOOK, "symbol_name": "cell:4"},
    ]
    r = await call(pipeline, "json", targets=targets)
    assert not r.is_error, r.text
    items = r.structured["data"]["items"]
    codes = [None if it["ok"] else it["error"]["code"] for it in items]
    assert codes == [
        "E_INVALID_ARGUMENT",
        "E_INVALID_ARGUMENT",
        "E_TARGET_NOT_FOUND",
        "E_UNSUPPORTED",
        "E_UNSUPPORTED",
        None,
    ]
    assert items[5]["code"] == "len(frame)"


async def test_cell_markdown_cell_is_returned_whole_without_line_numbers(pipeline: Pipeline) -> None:
    """Markdown セルは仮想 Python ファイルの行を占めない（in_virtual_file が false）。全文を 1 レコードで返す。"""
    doc = json.loads((POLY / NOTEBOOK).read_text(encoding="utf-8"))
    r = await call(pipeline, "json", targets=[{"path": NOTEBOOK, "symbol_name": "cell:1"}])
    assert not r.is_error, r.text
    item = r.structured["data"]["items"][0]
    assert item["code"] == "".join(doc["cells"][0]["source"])
    assert item["cell"]["cell_type"] == "markdown" and item["cell"]["in_virtual_file"] is False
    assert item["symbol"]["kind"] == "Cell" and item["confidence_level"] == "structural"
    c = await call(pipeline, targets=[{"path": NOTEBOOK, "symbol_name": "cell:1"}])
    assert g.validate(c.text) == []
    assert "@L1:" not in c.text  # 行番号は付けない


async def test_cell_code_cell_compact_uses_virtual_lines(pipeline: Pipeline) -> None:
    c = await call(pipeline, targets=[{"path": NOTEBOOK, "symbol_name": "cell:3"}])
    assert not c.is_error and g.validate(c.text) == []
    assert f"[Cell] {NOTEBOOK}#cell:3 @L4-11" in c.text
    assert "@L4: def load_frame(path: str) -> list[dict]:" in c.text
    assert "OUTPUT_SENTINEL" not in c.text


async def test_cell_truncated_cell_continues_with_the_cursor(tmp_path: Path) -> None:
    """大きいコードセルは続きを next_cursor で取れ、全ページの code を連結すると cells[n].source と一致する。"""
    src = [f"x{i} = {i}\n" for i in range(400)]
    src[-1] = src[-1].rstrip("\n")
    nb = {
        "cells": [{"cell_type": "code", "execution_count": 1, "metadata": {}, "outputs": [], "source": src}],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    (tmp_path / "big.ipynb").write_text(json.dumps(nb), encoding="utf-8")
    pipeline = _tmp_pipeline(tmp_path)
    parts: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(60):
        args: dict[str, Any] = {"targets": [{"path": "big.ipynb", "symbol_name": "cell:1"}], "max_tokens": 1500}
        if cursor:
            args["cursor"] = cursor
        r = await call(pipeline, "json", **args)
        assert not r.is_error, r.text
        parts.append(r.structured["data"]["items"][0])
        cursor = r.structured["page"]["next_cursor"]
        if not cursor:
            break
    assert len(parts) > 1 and all(p["truncated"] for p in parts[:-1]) and not parts[-1]["truncated"]
    assert "\n".join(p["code"] for p in parts) == "".join(src)
    for a, b in zip(parts, parts[1:], strict=False):
        assert b["range_returned"]["start_line"] == a["range_returned"]["end_line"] + 1


async def test_lexical_fallback_compact_is_degraded_and_marks_the_window(pipeline: Pipeline) -> None:
    c = await call(pipeline, targets=[{"path": COBOL, "symbol_name": "COMPUTE-TAX"}])
    assert not c.is_error and g.validate(c.text) == []
    lines = c.text.splitlines()
    assert lines[0].startswith("# get_symbol_bodies ok conf=lexical_fallback")
    assert lines[1].startswith("! DEGRADED: confidence=lexical_fallback engine=internal_ripgrep_word_boundary")
    assert "@L12:        COMPUTE-TAX." in c.text


async def test_lexical_fallback_line_and_lex_urn_give_one_window_around_the_line(pipeline: Pipeline) -> None:
    for ref in ({"path": COBOL, "line": 10}, {"urn": f"lk://{WS}/{COBOL}#lex:COMPUTE-TAX@L9"}):
        r = await call(pipeline, "json", targets=[ref])
        assert not r.is_error, r.text
        item = r.structured["data"]["items"][0]
        assert item["confidence_level"] == "lexical_fallback" and len(item["windows"]) == 1
        w = item["windows"][0]
        assert w["start_line"] <= w["match_line"] <= w["end_line"]
    r = await call(pipeline, "json", targets=[{"path": COBOL, "line": 999}])
    assert r.is_error and r.structured["error"]["code"] == "E_TARGET_NOT_FOUND"


async def test_lexical_fallback_windows_are_at_most_three_and_do_not_overlap(tmp_path: Path) -> None:
    """一致が離れて 5 か所あっても窓は最大 3 つ。各窓は 31 行以内で、前の窓に入る一致は別の窓にしない。"""
    rows = [f"line {i}" for i in range(1, 201)]
    for at in (10, 11, 60, 100, 140, 180):
        rows[at - 1] = f"CALL TARGET-{at % 7} target_name here"
    (tmp_path / "prog.xyz").write_text("\n".join(rows) + "\n", encoding="utf-8")
    pipeline = _tmp_pipeline(tmp_path)
    r = await call(pipeline, "json", targets=[{"path": "prog.xyz", "symbol_name": "target_name"}])
    assert not r.is_error, r.text
    item = r.structured["data"]["items"][0]
    windows = item["windows"]
    assert len(windows) == 3 and item["match_count"] == 6
    assert all(w["end_line"] - w["start_line"] + 1 <= 31 for w in windows)
    assert [w["match_line"] for w in windows] == [10, 60, 100]  # 11 行目は 10 行目の窓に入っている
    assert r.structured["_resolution_meta"]["is_degraded"] is True
