"""T-02 ``get_file_skeleton`` のデータファイル・Notebook の契約テスト（仕様書 §11 T-02 処理規約 3〜5・9、§8.6 B2・B4）。

Tier 3（JSON・JSONL・YAML・TOML・XML）と ``.ipynb`` はデータ形式アダプタの骨格を返す。パイプライン経由で呼ぶ。
ルートは ``tests/fixtures/poly``（F-POLY）。値そのもの（``FAILURE_SENTINEL``）と Notebook の出力（``OUTPUT_SENTINEL``）は
骨格に出ない（MUST）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.app import build_services
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import register_tools

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
JUNIT = "reports/junit.xml"
SETTINGS = "conf/settings.json"
APP_YAML = "conf/app.yaml"
PYPROJECT = "pyproject.toml"
NOTEBOOK = "notebooks/eda.ipynb"
PAY_PY = "src/shop/services/payment.py"
ALL_DATA = [SETTINGS, APP_YAML, PYPROJECT, JUNIT, NOTEBOOK]

DATA = [
    (JUNIT, "xml", 3),
    (SETTINGS, "json", 3),
    (APP_YAML, "yaml", 3),
    (PYPROJECT, "toml", 3),
    (NOTEBOOK, "ipynb", 2),
]
SENTINELS = ("FAILURE_SENTINEL", "OUTPUT_SENTINEL", "SECRET_SENTINEL")


def make_pipeline(root: Path) -> Pipeline:
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=build_services(lambda: [str(root)]))


@pytest.fixture
def pipeline() -> Pipeline:
    return make_pipeline(POLY)


async def call(pipeline: Pipeline, fmt: str = "compact", **args: Any):
    return await pipeline.call_tool("get_file_skeleton", {"format": fmt, **args})


@pytest.mark.parametrize(("path", "language", "tier"), DATA)
async def test_data_file_skeleton_is_grammatical_and_leaks_no_values(
    pipeline: Pipeline, path: str, language: str, tier: int
) -> None:
    r = await call(pipeline, paths=[path])
    assert not r.is_error
    assert g.validate(r.text) == []
    lines = r.text.splitlines()
    assert lines[0].startswith("# get_file_skeleton ok conf=structural ")
    sha = hashlib.sha256((POLY / path).read_bytes()).hexdigest()
    assert any(ln.startswith(f"=== {path} (") and f"sha={sha[:12]}" in ln for ln in lines)
    assert not any(s in r.text for s in SENTINELS)
    j = await call(pipeline, "json", paths=[path])
    assert not any(s in json.dumps(j.structured, ensure_ascii=False) for s in SENTINELS)
    item = j.structured["data"]["items"][0]
    assert item["ok"] is True and item["path"] == path and item["language"] == language
    assert item["tier"] == tier and item["file_sha256"] == sha
    assert item["confidence_level"] == "structural"
    assert j.structured["_resolution_meta"]["confidence_level"] == "structural"
    assert j.structured["_resolution_meta"]["is_degraded"] is False


async def test_xml_skeleton_aggregates_siblings(pipeline: Pipeline) -> None:
    """処理規約 4: タグ階層を `[Tag]` で、同名の兄弟は `×N` で集約し、属性名一覧を `type:` に出す。"""
    text = (await call(pipeline, paths=[JUNIT])).text
    assert "[Tag] testsuites @L2-45" in text
    assert "[Tag] testcase ×40 @L4-43" in text
    assert "[Tag] failure ×2 @L11-27" in text
    assert "type: attrs=[name,tests,failures,errors,time]" in text


async def test_json_yaml_toml_skeletons_show_key_tree_and_array_types(pipeline: Pipeline) -> None:
    """処理規約 5: キー階層を `[Key]` で、配列は要素数と推論した要素の型を `type:` に出す。値は出さない。"""
    text = (await call(pipeline, paths=[SETTINGS, APP_YAML, PYPROJECT])).text
    assert "[Key] limits ×3 @L6-19" in text and "type: array<object{currency:string,max:int}>" in text
    assert "[Key] backoff_ms ×3 @L6" in text and "type: array<int>" in text
    assert "[Key] dependencies ×2 @L5-8" in text and "type: array<string>" in text
    assert "[Key] tool.ruff @L10-11" in text
    for value in ("JPY", "8080", "pydantic", "shop"):
        assert value not in text


async def test_notebook_skeleton_has_cells_and_never_outputs(pipeline: Pipeline) -> None:
    """処理規約 3: セルを `[Cell]` で並べ、code セルの宣言は入れ子の symbol-block。outputs と metadata は捨てる。"""
    r = await call(pipeline, paths=[NOTEBOOK])
    assert g.validate(r.text) == []
    assert "[Cell] 1 markdown @L" in r.text and "  doc: " in r.text
    assert "[Cell] 3 code (exec_count: 12) @L" in r.text
    assert "  [Function] pub load_frame(path: str) -> list[dict] @L" in r.text
    assert "base64" not in r.text and "image/png" not in r.text
    header = next(ln for ln in r.text.splitlines() if ln.startswith("=== "))
    assert ", 4 cells, skeleton: " in header
    j = await call(pipeline, "json", paths=[NOTEBOOK])
    item = j.structured["data"]["items"][0]
    assert item["cell_count"] == 4 and [c["number"] for c in item["cells"]] == [1, 2, 3, 4]


def _strings(node: Any) -> list[str]:
    """JSON の値にある文字列（キーは除く）をすべて集める。"""
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    return []


async def test_t02_postcondition_3_notebook_outputs_never_appear_in_the_response(pipeline: Pipeline) -> None:
    """PC:T-02#3 ``.ipynb`` の応答文字列に、その Notebook の ``outputs`` 配列の文字列が 1 つも現れない（compact・json の両方）。

    期待値は固定の合言葉ではなく、Notebook JSON の ``outputs`` から集めた文字列そのもの。
    """
    doc = json.loads((POLY / NOTEBOOK).read_text(encoding="utf-8"))
    pieces = {
        part.strip()
        for cell in doc["cells"]
        for s in _strings(cell.get("outputs", []))
        for part in s.splitlines()
        if len(part.strip()) >= 8
    }
    assert len(pieces) >= 3, pieces  # 空の突き合わせにしない
    for fmt in ("compact", "json"):
        r = await call(pipeline, fmt, paths=[NOTEBOOK])
        assert not r.is_error, r.text
        text = r.text if fmt == "compact" else json.dumps(r.structured, ensure_ascii=False)
        assert "load_frame" in text  # セルの宣言は出ている（出力だけが落ちている）
        leaked = sorted(s for s in pieces if s in text)
        assert leaked == [], (fmt, leaked)


async def test_max_depth_folds_deeper_nodes(pipeline: Pipeline) -> None:
    """処理規約 4: `max_depth` を超える階層は `{ ...N nodes omitted }` に畳む（XML の深さ超過）。"""
    r = await call(pipeline, paths=[JUNIT], max_depth=2)
    assert "[Tag] testsuite @L3-44 { ...2 nodes omitted }" in r.text
    assert "testcase" not in r.text and g.validate(r.text) == []
    full = await call(pipeline, paths=[JUNIT], max_depth=4)
    assert "nodes omitted" not in full.text
    one = await call(pipeline, paths=[SETTINGS], max_depth=1)
    assert "[Key] service @L2-5 { ...2 nodes omitted }" in one.text and g.validate(one.text) == []


@pytest.mark.parametrize("max_tokens", [200, 300, 600])
async def test_budget_is_respected_for_data_files(pipeline: Pipeline, max_tokens: int) -> None:
    """事後条件 5: 予算に収まらないときは深さを浅くして収め（`- truncated:`）、収まらない分は次のページへ。文法は保つ。"""
    r = await call(pipeline, "json", paths=ALL_DATA, max_tokens=max_tokens)
    assert not r.is_error
    assert r.structured["budget"]["used_tokens_est"] <= max_tokens
    c = await call(pipeline, paths=ALL_DATA, max_tokens=max_tokens)
    assert g.validate(c.text) == []


async def test_shrink_reduces_depth_and_says_so(pipeline: Pipeline) -> None:
    full = await call(pipeline, paths=[SETTINGS], max_tokens=2000)
    small = await call(
        pipeline, paths=ALL_DATA, max_tokens=160
    )  # 先頭の 1 ファイルが深さ 1 に縮む予算（140〜193。ヘッダ・警告の約 30 トークンも予算に数える。BUG-0014。カーソルが
    #    約 70 → 9 トークンになり〈BUG-0115〉、以前の 230 では丸ごと入る〈丸ごとは 194 から〉）
    assert not full.is_error and not small.is_error
    assert "[Key] max_attempts" in full.text and "truncated" not in full.text
    assert g.validate(small.text) == []
    assert (
        "- truncated: 5 members omitted: service 2, features 3; whole file: max_tokens>=400 with this path alone"
        in small.text
    )
    assert (
        "[Key] features @L20-25 { ...3 nodes omitted }" in small.text
        and "[Key] max_attempts" not in small.text
    )
    assert "# more: cursor=" in small.text  # 入らない分は次のページへ


async def test_batch_keeps_request_order_and_mixes_code_and_data(pipeline: Pipeline) -> None:
    r = await call(
        pipeline, "json", paths=[SETTINGS, PAY_PY, JUNIT], max_tokens=20000
    )  # json は data の全項目を数える（BUG-0017）
    assert [i["path"] for i in r.structured["data"]["items"]] == [SETTINGS, PAY_PY, JUNIT]
    assert r.structured["_resolution_meta"]["engine_used"] == "tree-sitter+data-adapter"
    assert r.structured["data"]["items"][1]["language"] == "python"
    c = await call(pipeline, paths=[SETTINGS, PAY_PY, JUNIT])
    heads = [ln for ln in c.text.splitlines() if ln.startswith("=== ")]
    assert [h.split(" ")[1] for h in heads] == [SETTINGS, PAY_PY, JUNIT]
    assert g.validate(c.text) == []


async def test_engine_used_by_kind_of_files(pipeline: Pipeline) -> None:
    data = await call(pipeline, "json", paths=[SETTINGS])
    code = await call(pipeline, "json", paths=[PAY_PY])
    assert data.structured["_resolution_meta"]["engine_used"] == "data-adapter"
    assert code.structured["_resolution_meta"]["engine_used"] == "tree-sitter"


async def test_unsupported_files_still_fail_per_item(pipeline: Pipeline) -> None:
    r = await call(pipeline, paths=[SETTINGS, "legacy/calc.cob", "conf/.env", "src/missing.json"])
    assert not r.is_error and g.validate(r.text) == []
    assert (
        "=== legacy/calc.cob" in r.text and "! legacy/calc.cob" not in r.text
    )  # Tier 4 は推定の骨格（BUG-0018）
    assert "! conf/.env: E_UNSUPPORTED" in r.text and "secret_file" in r.text
    assert "! src/missing.json: E_TARGET_NOT_FOUND" in r.text
    assert "SECRET_SENTINEL" not in r.text


# --- 作業ツリーに置く小さなファイル（JSONL・壊れたファイル・秘密・その他の拡張子） ---------------------------------------


@pytest.fixture
def tmp_pipeline(tmp_path: Path) -> Pipeline:
    (tmp_path / "events.jsonl").write_text('{"id": 1, "user": {"name": "a"}}\n{"id": 2}\n', encoding="utf-8")
    (tmp_path / "broken.json").write_text('{"a": ', encoding="utf-8")
    (tmp_path / "broken.yaml").write_text("a: [1, 2\n", encoding="utf-8")
    (tmp_path / "my_credentials.json").write_text('{"token": "SECRET_SENTINEL_x"}\n', encoding="utf-8")
    (tmp_path / "list.json").write_text('[{"id": 1}, {"id": 2}]\n', encoding="utf-8")
    (tmp_path / "icon.svg").write_text('<svg>\n  <g>\n    <path d="M0"/>\n  </g>\n</svg>\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "x"\n', encoding="utf-8")
    (tmp_path / "openapi.yaml").write_text("openapi: 3.0.0\ninfo:\n  title: t\n", encoding="utf-8")
    (tmp_path / "bad.ipynb").write_text('{"nope": 1}', encoding="utf-8")
    return make_pipeline(tmp_path)


async def test_jsonl_note_and_count(tmp_pipeline: Pipeline) -> None:
    r = await call(tmp_pipeline, paths=["events.jsonl"])
    assert g.validate(r.text) == []
    assert "- note: schema inferred from first 100 records" in r.text
    assert "[Key] $ ×2 @L1-2" in r.text
    j = await call(tmp_pipeline, "json", paths=["events.jsonl"])
    assert j.structured["data"]["items"][0]["language"] == "jsonl"


async def test_broken_data_files_fail_per_item_with_a_reason(tmp_pipeline: Pipeline) -> None:
    r = await call(tmp_pipeline, "json", paths=["list.json", "broken.json", "broken.yaml", "bad.ipynb"])
    assert not r.is_error
    items = r.structured["data"]["items"]
    assert [i["ok"] for i in items] == [True, False, False, False]
    for bad in items[1:]:
        assert bad["error"]["code"] == "E_UNSUPPORTED"
        assert bad["error"]["details"]["reason"] == "invalid_data_file"
    assert r.structured["warnings"][0].startswith("3 of 4 items failed")
    assert "[Key] $ ×2" in (await call(tmp_pipeline, paths=["list.json"])).text  # 根が配列


async def test_secret_named_data_file_is_not_read(tmp_pipeline: Pipeline) -> None:
    r = await call(tmp_pipeline, paths=["my_credentials.json", "list.json"])
    assert "! my_credentials.json: E_UNSUPPORTED" in r.text and "secret_file" in r.text
    assert "SECRET_SENTINEL" not in r.text


async def test_other_xml_toml_names_and_openapi_use_the_adapters(tmp_pipeline: Pipeline) -> None:
    r = await call(tmp_pipeline, "json", paths=["icon.svg", "uv.lock", "openapi.yaml"])
    assert not r.is_error
    items = r.structured["data"]["items"]
    assert [(i["path"], i["language"]) for i in items] == [
        ("icon.svg", "xml"),
        ("uv.lock", "toml"),
        ("openapi.yaml", "yaml"),
    ]
    assert g.validate((await call(tmp_pipeline, paths=["icon.svg", "uv.lock", "openapi.yaml"])).text) == []
