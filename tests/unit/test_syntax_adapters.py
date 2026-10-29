"""syntax/adapters（Tier 3 データファイル: JSON・JSONL・YAML・TOML・XML）の単体テスト。

仕様書 §8.6 B4・§11 T-02 処理規約 4・5・T-05 処理規約 3、設計書 §7.4.8。フィクスチャは ``tests/fixtures/poly``。
期待する行番号はフィクスチャを走査して導く（目視の数値を固定しない）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import yaml

from lucianke.syntax.adapters import (
    ADAPTERS,
    SelectorError,
    UnsupportedSelectorError,
    json_,
    toml_,
    xml_,
    yaml_,
)
from lucianke.syntax.types import Match, SkeletonOptions

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "contract")
)  # 文法検査器 compact_grammar を共有する
import compact_grammar as grammar  # noqa: E402

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
OPTS = SkeletonOptions()


@pytest.fixture(params=["libyaml", "pure"])
def yaml_loader(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """YAML の位置は libyaml（CSafeLoader）と純 Python の SafeLoader の両方で同じになる。"""
    if request.param == "pure":
        monkeypatch.setattr(yaml, "CSafeLoader", yaml.SafeLoader, raising=False)
    return request.param


def read(rel: str) -> bytes:
    return (POLY / rel).read_bytes()


def line_of(source: bytes, needle: str, nth: int = 1) -> int:
    """``needle`` を含む ``nth`` 番目の行の 0 始まりの行番号。"""
    hits = [i for i, ln in enumerate(source.decode("utf-8").splitlines()) if needle in ln]
    return hits[nth - 1]


def skel(mod, source: bytes, rel: str, opts: SkeletonOptions = OPTS) -> list[str]:
    rec = mod.skeleton(source, opts, rel=rel, sha256=hashlib.sha256(source).hexdigest())
    return list(rec.lines)


def starts(matches: list[Match]) -> list[int]:
    return [m.range.start.line for m in matches]


# --- 骨格: フィクスチャ ---------------------------------------------------------------------------


def test_json_skeleton_fixture() -> None:
    src = read("conf/settings.json")
    lines = skel(json_, src, "conf/settings.json")
    sha12 = hashlib.sha256(src).hexdigest()[:12]
    assert lines[0] == f"=== conf/settings.json (26 lines, skeleton: {len(lines) - 1} lines, sha={sha12}) ==="
    first = line_of(src, '"limits"') + 1  # "limits" の行から、配列を閉じる "  ]," の行まで
    last = line_of(src, "  ],") + 1
    assert f"[Key] limits ×3 @L{first}-{last}" in lines
    assert "  type: array<object{currency:string,max:int}>" in lines
    # 入れ子の階層はインデントで表す
    assert "[Key] service @L2-5" in lines
    assert "  [Key] name @L3" in lines
    assert "  [Key] retry @L22-24" in lines
    assert "    [Key] max_attempts @L23" in lines
    assert "[Key] features @L20-25" in lines


def test_yaml_skeleton_fixture(yaml_loader: str) -> None:
    src = read("conf/app.yaml")
    lines = skel(yaml_, src, "conf/app.yaml")
    assert lines[0].startswith("=== conf/app.yaml (6 lines, skeleton: 7 lines, sha=")
    assert lines[1:] == [
        "[Key] service @L1-3",
        "  [Key] name @L2",
        "  [Key] workers @L3",
        "[Key] retry @L4-6",
        "  [Key] max_attempts @L5",
        "  [Key] backoff_ms ×3 @L6",
        "    type: array<int>",
    ]


def test_toml_skeleton_fixture() -> None:
    src = read("pyproject.toml")
    lines = skel(toml_, src, "pyproject.toml")
    assert lines[1:] == [
        "[Key] project @L1-8",
        "  [Key] name @L2",
        "  [Key] version @L3",
        "  [Key] requires-python @L4",
        "  [Key] dependencies ×2 @L5-8",
        "    type: array<string>",
        "[Key] tool.ruff @L10-11",  # 暗黙の表 tool は ruff と連ねて 1 つにする（仕様書 T-02 の例）
        "  [Key] line-length @L11",
    ]


def test_xml_skeleton_fixture() -> None:
    src = read("reports/junit.xml")
    lines = skel(xml_, src, "reports/junit.xml")
    assert lines[0].startswith("=== reports/junit.xml (45 lines, skeleton: 7 lines, sha=")
    assert lines[1:] == [
        "[Tag] testsuites @L2-45",
        "  [Tag] testsuite @L3-44",
        "    type: attrs=[name,tests,failures,errors,time]",
        "    [Tag] testcase ×40 @L4-43",
        "      type: attrs=[classname,name,time]",
        # 失敗は別々の testcase の子。同名兄弟を集約した子は和集合になり、範囲は最初の開始〜最後の終了
        f"      [Tag] failure ×2 @L{line_of(src, 'FAILURE_SENTINEL_7') + 1}-{line_of(src, 'FAILURE_SENTINEL_23') + 1}",
        "        type: attrs=[message]",
    ]


def test_xml_skeleton_never_shows_data() -> None:
    src = read("reports/junit.xml")
    text = "\n".join(skel(xml_, src, "reports/junit.xml"))
    assert "FAILURE_SENTINEL" not in text
    assert "assert 1 == 2" not in text  # 属性の値も出さない（名前だけ）
    assert "Traceback" not in text


def test_json_yaml_toml_skeleton_never_shows_values() -> None:
    for mod, rel, secret in (
        (json_, "conf/settings.json", "JPY"),
        (toml_, "pyproject.toml", "pydantic"),
        (yaml_, "conf/app.yaml", "shop"),
    ):
        assert secret not in "\n".join(skel(mod, read(rel), rel)), rel


@pytest.mark.parametrize(
    ("mod", "rel"),
    [
        (json_, "conf/settings.json"),
        (yaml_, "conf/app.yaml"),
        (toml_, "pyproject.toml"),
        (xml_, "reports/junit.xml"),
    ],
)
def test_skeleton_record_and_grammar(mod, rel: str) -> None:
    src = read(rel)
    sha = hashlib.sha256(src).hexdigest()
    rec = mod.skeleton(src, OPTS, rel=rel, sha256=sha)
    assert rec.kind == "file-block"
    assert rec.group == rec.key == rel
    assert rec.confidence.value == "structural"
    assert rec.data["file_sha256"] == sha
    assert rec.data["tier"] == 3
    assert rec.data["skeleton_line_count"] == len(rec.lines) - 1
    assert rec.data["symbols"]
    # Compact DSL の文法（@L の有無・attr-key の語彙・インデント）に適合する
    assert grammar.validate("\n".join(rec.lines), require_header=False) == []


# --- 骨格: 深さ・型・集約 ---------------------------------------------------------------------------


def test_json_depth_limit_folds_nodes() -> None:
    src = b'{\n  "a": {\n    "b": {\n      "c": {\n        "d": 1,\n        "e": 2\n      }\n    }\n  }\n}\n'
    lines = skel(json_, src, "x.json", SkeletonOptions(max_depth=2))
    assert lines[1:] == ["[Key] a @L2-9", "  [Key] b @L3-8 { ...3 nodes omitted }"]
    full = skel(json_, src, "x.json", SkeletonOptions(max_depth=4))
    assert "      [Key] c @L4-7" not in full  # 3 段目は 4 行目の c（インデント 4 つ）
    assert "    [Key] c @L4-7" in full
    assert "      [Key] d @L5" in full


def test_json_array_type_inference() -> None:
    src = b'{"users": [{"id": 1, "user": {"n": "a"}, "tags": ["x"]}, {"id": 2, "note": null}], "xs": [], "m": [1, "a", 2.5]}'
    lines = skel(json_, src, "x.json")
    users = [ln for ln in lines if ln.strip().startswith("type: array<object")][0]
    assert users == "  type: array<object{id:int,user?:object,tags?:array<string>,note?:null}>"
    assert "[Key] xs ×0 @L1" in lines
    assert "  type: array<unknown>" in lines
    assert "  type: array<int|string|float>" in lines


def test_json_root_array_and_scalar() -> None:
    lines = skel(json_, b'[{"id": 1}, {"id": 2}]\n', "x.json")
    assert lines[1:] == ["[Key] $ ×2 @L1", "  type: array<object{id:int}>"]
    assert skel(json_, b"42\n", "x.json")[1:] == ["[Key] $ @L1", "  type: int"]


def test_jsonl_skeleton_infers_from_first_100_records() -> None:
    rows = [f'{{"id": {i}, "name": "n{i}"}}' for i in range(150)]
    rows[120] = '{"id": 120, "late_key": true}'  # 101 件目以降は推論に使わない
    src = ("\n".join(rows) + "\n").encode()
    lines = skel(json_, src, "logs/events.jsonl")  # skeleton() は拡張子で JSONL に振り分ける
    assert lines[0].startswith("=== logs/events.jsonl (150 lines, skeleton: 3 lines, sha=")
    assert lines[1:] == [
        "- note: schema inferred from first 100 records",
        "[Key] $ ×150 @L1-150",
        "  type: array<object{id:int,name:string}>",
    ]
    assert grammar.validate("\n".join(lines), require_header=False) == []


def test_jsonl_skips_blank_lines_and_reports_bad_line() -> None:
    src = b'{"a": 1}\n\n{"a": 2}\n'
    assert skel(json_, src, "x.jsonl")[2] == "[Key] $ ×2 @L1-3"
    with pytest.raises(ValueError, match="line 2"):
        skel(json_, b'{"a": 1}\n{oops}\n', "x.jsonl")


def test_xml_collapses_siblings_and_merges_children() -> None:
    src = b"""<?xml version="1.0"?>
<root>
  <item id="1"><a x="1"/></item>
  <other/>
  <item id="2" lang="en">
    <b/>
  </item>
</root>
"""
    lines = skel(xml_, src, "x.xml")
    assert lines[1:] == [
        "[Tag] root @L2-8",
        "  [Tag] item ×2 @L3-7",
        "    type: attrs=[id,lang]",
        "    [Tag] a @L3",
        "      type: attrs=[x]",
        "    [Tag] b @L6",
        "  [Tag] other @L4",
    ]


def test_xml_multiline_start_tag_and_depth() -> None:
    src = b'<r>\n  <e\n     a="1"\n     b="2"\n  />\n  <d><d2><d3>t</d3></d2></d>\n</r>\n'
    lines = skel(xml_, src, "x.xml", SkeletonOptions(max_depth=2))
    assert lines[1:] == [
        "[Tag] r @L1-7",
        "  [Tag] e @L2-5",
        "    type: attrs=[a,b]",
        "  [Tag] d @L6 { ...2 nodes omitted }",
    ]


def test_xml_large_file_streams_without_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"".join(b'  <row id="%d"><v>x</v></row>\n' % i for i in range(60000))
    src = b'<?xml version="1.0"?>\n<rows>\n' + body + b"</rows>\n"
    assert len(src) > 1_500_000
    monkeypatch.setattr(xml_, "STREAM_BYTES", 1_000_000)
    lines = skel(xml_, src, "big.xml")
    assert "- note: large file (> 5 MB): streamed without building a tree" in lines
    assert "  [Tag] row ×60000 @L3-60002" in lines


# --- 骨格: 位置と形式の癖 -----------------------------------------------------------------------------


def test_json_positions_with_crlf_bom_and_unicode() -> None:
    text = '{\r\n  "名前": "値",\r\n  "list": [\r\n    1,\r\n    2\r\n  ]\r\n}\r\n'
    src = b"\xef\xbb\xbf" + text.encode()
    lines = skel(json_, src, "x.json")
    assert lines[1:] == ["[Key] 名前 @L2", "[Key] list ×2 @L3-6", "  type: array<int>"]
    assert starts(json_.select(src, "jsonpath:$.list")) == [2]
    m = json_.select(src, "jsonpath:$.list")[0]
    assert (m.range.start.line, m.range.end.line) == (2, 5)


def test_json_string_escapes_do_not_confuse_positions() -> None:
    src = b'{"a": "x\\"}\\\\", "b": {"c": "]"},\n "d": 1}'
    m = json_.select(src, "jsonpath:$.d")
    assert starts(m) == [1]
    assert starts(json_.select(src, "jsonpath:$.b.c")) == [0]


def test_yaml_ranges_for_block_scalar_flow_and_comments(yaml_loader: str) -> None:
    src = b"""# header comment
a:
  b: 1
  # trailing comment inside a

c: |
  line1
  line2

d: {x: 1, y: [2, 3]}
e:
  - name: p
    v: 1
  - name: q
# end comment
"""
    lines = skel(yaml_, src, "x.yaml")
    assert lines[1:] == [
        "[Key] a @L2-3",
        "  [Key] b @L3",
        "[Key] c @L6-8",
        "[Key] d @L10",
        "  [Key] x @L10",
        "  [Key] y ×2 @L10",
        "    type: array<int>",
        "[Key] e ×2 @L11-14",
        "  type: array<object{name:string,v?:int}>",
    ]
    assert [(m.range.start.line, m.range.end.line) for m in yaml_.select(src, "yaml:e[1]")] == [(13, 13)]


def test_yaml_multi_document_note_and_empty(yaml_loader: str) -> None:
    lines = skel(yaml_, b"a: 1\n---\nb: 2\n", "x.yaml")
    assert "- note: multi-document YAML: only the first document is shown" in lines
    assert "[Key] a @L1" in lines
    assert skel(yaml_, b"", "x.yaml")[1:] == []
    assert yaml_.select(b"", "yaml:a") == []


def test_yaml_alias_and_custom_tags_do_not_break(yaml_loader: str) -> None:
    src = b"base: &b\n  k: 1\nuse:\n  <<: *b\n  z: !Custom x\n"
    lines = skel(yaml_, src, "x.yaml")
    assert "[Key] base @L1-2" in lines
    assert "[Key] use @L3-5" in lines


def test_toml_arrays_of_tables_dotted_keys_and_multiline_values() -> None:
    src = b'''title = "t"
a.b.c = 1
[[servers]]
name = "x"
ports = [
  1,
  2,
]
[[servers]]
name = "y"
note = """
multi # not a comment
[not.a.table]
"""
[servers.meta]
k = 'v'
[inline]
t = { x = 1, y = 2 }
'''
    lines = skel(toml_, src, "x.toml")
    assert lines[1:] == [
        "[Key] title @L1",
        "[Key] a.b.c @L2",
        "[Key] servers ×2 @L3-16",
        "  type: array<object{name:string,ports?:array<int>,note?:string,meta?:object}>",
        "[Key] inline @L17-18",
        "  [Key] t @L18",
        "    [Key] x @L18",
        "    [Key] y @L18",
    ]
    assert [(m.range.start.line, m.range.end.line) for m in toml_.select(src, "toml:servers[0].ports")] == [
        (4, 7)
    ]
    assert [(m.range.start.line, m.range.end.line) for m in toml_.select(src, "toml:servers[1].note")] == [
        (10, 13)
    ]
    assert [(m.range.start.line, m.range.end.line) for m in toml_.select(src, "toml:servers[1].meta.k")] == [
        (15, 15)
    ]
    assert [(m.range.start.line, m.range.end.line) for m in toml_.select(src, "toml:a.b")] == [(1, 1)]


# --- 部分取得 -------------------------------------------------------------------------------------


def test_select_jsonpath() -> None:
    src = read("conf/settings.json")
    m = json_.select(src, "jsonpath:$.limits[0].currency")
    assert len(m) == 1
    assert (m[0].kind, m[0].name, m[0].data["path"]) == ("Key", "currency", "$.limits[0].currency")
    jpy = line_of(src, '"JPY"')
    assert (m[0].range.start.line, m[0].range.end.line) == (jpy, jpy)
    assert '"JPY"' in m[0].text
    assert starts(json_.select(src, "jsonpath:$.limits[*].max")) == [
        i for i, ln in enumerate(src.decode().splitlines()) if '"max":' in ln
    ]
    assert starts(json_.select(src, "jsonpath:$..max_attempts")) == [line_of(src, "max_attempts")]
    assert len(json_.select(src, "jsonpath:$.limits[?(@.max > 5000)]")) == 3  # フィルタ式（拡張構文）
    assert json_.select(src, "jsonpath:$.nothing") == []
    # 配列の要素は値そのものの範囲（波括弧の行から閉じ括弧の行まで）
    el = json_.select(src, "jsonpath:$.limits[1]")[0]
    assert (el.range.start.line, el.range.end.line) == (line_of(src, '"USD"') - 1, line_of(src, '"USD"') + 2)
    assert json_.select(src, "jsonpath:$.limits[-1].currency")[0].range.start.line == line_of(src, '"EUR"')
    whole = json_.select(src, "jsonpath:$")[0]
    assert (whole.range.start.line, whole.range.end.line) == (0, 25)


def test_select_yaml(yaml_loader: str) -> None:
    src = read("conf/app.yaml")
    m = yaml_.select(src, "yaml:retry.backoff_ms")
    assert [(x.name, x.data["path"]) for x in m] == [("backoff_ms", "retry.backoff_ms")]
    assert starts(m) == [line_of(src, "backoff_ms")]
    assert m[0].text == "  backoff_ms: [100, 200, 400]"
    retry = yaml_.select(src, "yaml:retry")[0]
    assert (retry.range.start.line, retry.range.end.line) == (3, 5)
    assert starts(yaml_.select(src, "yaml:retry.backoff_ms[1]")) == [5]
    assert len(yaml_.select(src, "yaml:service.*")) == 2
    assert yaml_.select(src, "yaml:retry.nothing") == []


def test_select_toml() -> None:
    src = read("pyproject.toml")
    m = toml_.select(src, "toml:project.dependencies")
    assert len(m) == 1
    assert (m[0].range.start.line, m[0].range.end.line) == (
        line_of(src, "dependencies = ["),
        line_of(src, "httpx") + 1,
    )
    assert "pydantic>=2" in m[0].text and "httpx>=0.27" in m[0].text
    assert starts(toml_.select(src, "toml:tool.ruff.line-length")) == [line_of(src, "line-length")]
    ruff = toml_.select(src, "toml:tool.ruff")[0]
    assert (ruff.range.start.line, ruff.range.end.line) == (
        line_of(src, "[tool.ruff]"),
        line_of(src, "line-length"),
    )
    assert toml_.select(src, 'toml:"project".name')[0].name == "name"
    assert toml_.select(src, "toml:project.dependencies[0]")[0].name == "[0]"
    assert toml_.select(src, "toml:project.absent") == []


def test_select_xpath() -> None:
    src = read("reports/junit.xml")
    m = xml_.select(src, "xpath://testcase[failure]")
    assert len(m) == 2
    expected = [i for i, ln in enumerate(src.decode().splitlines()) if "FAILURE_SENTINEL" in ln]
    assert starts(m) == expected
    assert [x.range.end.line for x in m] == expected  # 失敗ケースは 1 行
    assert all("<failure" in x.text for x in m)
    assert {x.kind for x in m} == {"Tag"}
    assert m[0].data["attrs"]["name"] == "test_case_7"
    assert len(xml_.select(src, "xpath:.//testcase")) == 40
    assert (
        len(xml_.select(src, "xpath:/testsuites/testsuite/testcase")) == 40
    )  # 絶対パスは文書ノードからの相対に読み替える
    one = xml_.select(src, "xpath://testcase[@name='test_case_3']")
    assert starts(one) == [line_of(src, 'name="test_case_3"')]
    suite = xml_.select(src, "xpath://testsuite")[0]
    assert (suite.range.start.line, suite.range.end.line) == (2, 43)
    assert xml_.select(src, "xpath://nothing") == []


def test_select_xpath_multiline_element() -> None:
    src = b'<r>\n  <e\n     a="1"\n  >\n    <c/>\n  </e>\n  <f a="2"\n  />\n</r>\n'
    e = xml_.select(src, "xpath:.//e")[0]
    assert (e.range.start.line, e.range.end.line) == (1, 5)
    f = xml_.select(src, "xpath:.//f")[0]
    assert (f.range.start.line, f.range.end.line) == (6, 7)


def test_select_jsonl() -> None:
    src = b'{"id": 1, "x": {"y": 5}}\n\n{"id": 2}\n{"id": 3}\n'
    assert starts(json_.select_jsonl(src, "jsonpath:$[1].id")) == [2]
    assert starts(json_.select_jsonl(src, "jsonpath:$[*].id")) == [0, 2, 3]
    assert starts(json_.select_jsonl(src, "jsonpath:$[?(@.id > 1)]")) == [2, 3]
    assert starts(json_.select_jsonl(src, "jsonpath:$..y")) == [0]
    assert json_.select_jsonl(src, "jsonpath:$[9]") == []


# --- 不正なセレクタと不正な文書 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mod", "selector"),
    [
        (json_, "jsonpath:$.["),
        (json_, "jsonpath:   "),
        (json_, "$.limits"),  # 接頭辞が無い
        (json_, "nonsense:$.a"),
        (json_, "xpath://a"),  # 形式に合わない
        (json_, "jq:.limits"),  # jq: は M2
        (yaml_, "yaml:a..b"),
        (yaml_, "yaml:a["),
        (yaml_, "yaml:.a"),
        (yaml_, "yaml:a."),
        (yaml_, "yaml:a[x]"),
        (yaml_, "toml:a"),
        (toml_, "toml:"),
        (toml_, "toml:a[1.5]"),
        (toml_, 'toml:"unterminated'),
        (toml_, "yaml:a"),
        (xml_, "xpath://testcase["),
        (xml_, "xpath:"),
        (xml_, "jsonpath:$.a"),
    ],
)
def test_invalid_selector_is_value_error(mod, selector: str) -> None:
    sources = {
        json_: read("conf/settings.json"),
        yaml_: read("conf/app.yaml"),
        toml_: read("pyproject.toml"),
        xml_: read("reports/junit.xml"),
    }
    with pytest.raises(ValueError):
        mod.select(sources[mod], selector)


def test_selector_error_classes() -> None:
    src = read("conf/settings.json")
    with pytest.raises(UnsupportedSelectorError):
        json_.select(src, "jq:.limits")
    with pytest.raises(UnsupportedSelectorError):
        json_.select(src, "xpath://a")
    with pytest.raises(SelectorError) as ei:
        json_.select(src, "jsonpath:$.[")
    assert not isinstance(ei.value, UnsupportedSelectorError)
    assert issubclass(SelectorError, ValueError)


@pytest.mark.parametrize(
    ("mod", "bad"),
    [
        (json_, b'{"a": 1,}'),
        (json_, b'{"a": }'),
        (json_, b""),
        (yaml_, b"a: [1, 2\nb: 3\n"),
        (yaml_, b"a: 1\n  b: 2\n"),
        (toml_, b"a = \nb = 2\n"),
        (toml_, b"[a\nb = 1\n"),
        (xml_, b"<a><b></a>"),
        (xml_, b""),
        (xml_, b'<!DOCTYPE x [<!ENTITY e "boom">]><x>&e;</x>'),  # エンティティ宣言は受け付けない
    ],
)
def test_invalid_document_is_value_error(mod, bad: bytes) -> None:
    with pytest.raises(ValueError):
        skel(mod, bad, "bad")
    sel = {json_: "jsonpath:$.a", yaml_: "yaml:a", toml_: "toml:a", xml_: "xpath://a"}[mod]
    with pytest.raises(ValueError):
        mod.select(bad, sel)


# --- 登録表・縮退・大きいファイル ----------------------------------------------------------------------


def test_adapters_table() -> None:
    assert ADAPTERS[".json"].language_id == "json"
    assert ADAPTERS[".jsonl"].language_id == ADAPTERS[".ndjson"].language_id == "jsonl"
    assert ADAPTERS[".yaml"] is ADAPTERS[".yml"]
    assert ADAPTERS[".toml"].language_id == "toml"
    assert all(ADAPTERS[e].language_id == "xml" for e in (".xml", ".xsd", ".xsl", ".xslt", ".wsdl", ".svg"))
    src = read("conf/app.yaml")
    recs = ADAPTERS[".yaml"].skeleton(src, OPTS)
    assert len(recs) == 1 and recs[0].kind == "file-block"
    assert ADAPTERS[".yaml"].select(src, "yaml:retry")[0].kind == "Key"
    assert int(ADAPTERS[".toml"].tier) == 3
    with pytest.raises(NotImplementedError):
        ADAPTERS[".json"].replace(b"{}", "jsonpath:$", b"1")


def test_shrink_drops_depth_one_level_at_a_time() -> None:
    src = b'{"a": {"b": {"c": {"d": 1}}}, "x": 1}\n'
    rec = json_.skeleton(
        src, OPTS, rel="x.json", sha256="0" * 64, estimate=lambda text: len(text.splitlines())
    )
    assert rec.shrink is not None
    assert [ln for ln in rec.lines if ln.startswith("[") or ln.startswith(" ")] == [
        "[Key] a @L1",
        "  [Key] b @L1",
        "    [Key] c @L1",
        "      [Key] d @L1",
        "[Key] x @L1",
    ]
    small = rec.shrink(
        5
    )  # 見積もりは行数。深さ 3 は 6 行、深さ 2 は ヘッダ 1 + 本文 3 + truncated の注 1 = 5 行
    assert small is not None
    assert small.shrink is None
    assert "  [Key] b @L1 { ...2 nodes omitted }" in small.lines
    assert small.data["truncated_members"] == 2
    assert small.lines[-1] == "- truncated: 2 members omitted (depth limited to 2)"
    assert rec.shrink(1) is None  # どれだけ浅くしても入らない
    flat = json_.skeleton(src, SkeletonOptions(max_depth=1), rel="x.json", sha256="0" * 64)
    assert flat.shrink is None


def test_large_json_limits_depth_and_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_, "STREAM_BYTES", 10)
    src = b'{"a": {"b": {"c": 1}}, "xs": [1, 2]}\n'
    lines = skel(json_, src, "x.json")
    assert any(ln.startswith("- note: large file (> 5 MB)") for ln in lines)
    assert "  [Key] b @L1 { ...1 nodes omitted }" in lines


def test_deep_nesting_is_value_error() -> None:
    src = b"[" * 5000 + b"]" * 5000
    with pytest.raises(ValueError):
        skel(json_, src, "deep.json")
