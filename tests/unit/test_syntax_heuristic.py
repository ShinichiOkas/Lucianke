"""Tier 4 ヒューリスティック（``syntax/heuristic.py``）の単体テスト。

* ブロック推定（ctags なし）: COBOL の段落・字下げ・括弧・``end`` の終了行、BOM・CRLF・非 ASCII の名前。
* 字句検索: 単語境界・定義らしさの並べ替え・メタ文字を含む名前・打ち切り。
* ctags 経路: この機械に Universal Ctags が無くても通るよう、偽の ctags（Python スクリプト）で子プロセス経由の
  経路全体（一時ファイル・引数・タイムアウト・失敗時の降格）を試す。本物の ctags があれば、それでも試す。
"""

from __future__ import annotations

import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lucianke.core.limits import REGEX_PATTERN_MAX_CHARS
from lucianke.core.model import Confidence, Kind, RelPath, SymbolRecord
from lucianke.core.procs import ProcessRegistry
from lucianke.syntax import heuristic
from lucianke.syntax.heuristic import (
    BLOCK_WINDOW_RADIUS,
    block_outline,
    block_window,
    find_ctags,
    lexical_find,
    lexical_scan,
    outline,
    parse_ctags_json,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS = "app"


def rel(path: str) -> RelPath:
    return RelPath(path)


def spans(records: list[SymbolRecord]) -> dict[str, tuple[int, int]]:
    """名前 → 外部表現の行範囲（1 始まり・両端を含む）。"""
    return {r.name: r.range.line_span_external() for r in records}


def block(text: str | bytes, path: str = "x/file.custom") -> tuple[list[SymbolRecord], Confidence]:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return block_outline(data, rel=rel(path), workspace_id=WS)


# --- ブロック推定（ctags なし） -----------------------------------------------------------------


def test_cobol_paragraphs_get_start_and_estimated_end_lines() -> None:
    source = (FIXTURES / "legacy" / "calc.cob").read_bytes()
    records, conf = block_outline(source, rel=rel("legacy/calc.cob"), workspace_id=WS)
    assert conf is Confidence.HEURISTIC
    # PROCEDURE DIVISION. が ``procedure`` の定義として拾われない（DIVISION という記号が出ない）こと
    assert spans(records) == {"MAIN-PARA": (8, 11), "COMPUTE-TAX": (12, 13), "WRITE-REPORT": (14, 15)}
    assert all(r.confidence is Confidence.HEURISTIC for r in records)
    assert all(r.kind is Kind.SECTION for r in records)
    first = records[0]
    assert str(first.urn) == "lk://app/legacy/calc.cob#ctags:MAIN-PARA@L8"
    assert first.path == "legacy/calc.cob"
    assert first.signature == "MAIN-PARA."
    assert first.selection is not None
    assert (first.selection.start.line, first.selection.start.col, first.selection.end.col) == (7, 7, 16)


def test_cobol_division_header_with_using_is_not_a_symbol() -> None:
    source = "       PROCEDURE DIVISION USING X.\n       MAIN.\n           STOP RUN.\n"
    records, _ = block(source)
    assert spans(records) == {"MAIN": (2, 3)}


def test_custom_rules_file_yields_one_line_definitions() -> None:
    source = (FIXTURES / "conf" / "rules.custom").read_bytes()
    records, conf = block_outline(source, rel=rel("conf/rules.custom"), workspace_id=WS)
    assert conf is Confidence.HEURISTIC
    assert spans(records) == {"MAX_RETRY": (2, 2), "BACKOFF": (3, 3)}
    assert str(records[0].urn) == "lk://app/conf/rules.custom#ctags:MAX_RETRY@L2"


def test_indentation_decides_the_end_of_python_like_blocks() -> None:
    source = textwrap.dedent(
        """\
        import os

        class Foo:
            def bar(self, a,
                    b):
                return a

            def baz(self):
                pass


        def qux(): pass
        x = 1
        """
    )
    records, conf = block(source)
    assert conf is Confidence.HEURISTIC
    assert spans(records) == {"Foo": (3, 9), "bar": (4, 6), "baz": (8, 9), "qux": (12, 12), "x": (13, 13)}
    kinds = {r.name: r.kind for r in records}
    assert kinds["Foo"] is Kind.CLASS
    assert kinds["bar"] is Kind.FUNCTION
    assert kinds["x"] is Kind.VARIABLE


def test_braces_decide_the_end_of_c_like_blocks() -> None:
    source = textwrap.dedent(
        """\
        function foo(a) {
          if (a) {
            return "}";
          }
          return 1;
        }

        class Bar
        {
          int x;
        }
        """
    )
    records, _ = block(source)
    assert spans(records) == {"foo": (1, 6), "Bar": (8, 11)}


def test_braces_closed_on_the_definition_line_fall_back_to_indentation() -> None:
    source = "def f(a={}):\n    return a\n\nx = 1\n"
    records, _ = block(source)
    assert spans(records) == {"f": (1, 2), "x": (4, 4)}


def test_end_keyword_closes_the_block() -> None:
    ruby = "def foo\n  1\nend\n\ndef bar\n  2\nend\n"
    assert spans(block(ruby)[0]) == {"foo": (1, 3), "bar": (5, 7)}
    lua = "function a()\nreturn 1\nend\n\nfunction b()\nreturn 2\nend\n"
    assert spans(block(lua)[0]) == {"a": (1, 3), "b": (5, 7)}


def test_one_line_definitions_without_a_closer_stay_one_line() -> None:
    records, _ = block("def a(): pass\nprint(1)\nprint(2)\ndef b(): pass\n")
    assert spans(records) == {"a": (1, 1), "b": (4, 4)}


def test_comparison_and_indented_assignments_are_not_definitions() -> None:
    records, conf = block("a == 3\n  b = 4\nwhen x = 5 then y\nc >= 1\nd => 2\ne =~ f\n")
    assert records == []
    assert conf is Confidence.LEXICAL_FALLBACK


def test_empty_and_definition_free_sources_give_lexical_fallback() -> None:
    for text in (b"", b"\n\n", b"plain prose\nwithout definitions\n"):
        records, conf = block(text)
        assert records == []
        assert conf is Confidence.LEXICAL_FALLBACK


def test_constants_are_told_from_variables_and_rule_is_a_function() -> None:
    records, _ = block("MAX = 1\nmin = 2\nrule R = 3\nconst K = 4\n")
    kinds = {r.name: r.kind for r in records}
    assert kinds == {"MAX": Kind.CONSTANT, "min": Kind.VARIABLE, "R": Kind.FUNCTION, "K": Kind.CONSTANT}


def test_bom_crlf_and_non_ascii_names() -> None:
    records, _ = block(
        b"\xef\xbb\xbfdef f():\r\n    pass\r\n\r\ndef \xe9\x96\xa2\xe6\x95\xb0():\r\n    pass\r\n"
    )
    assert spans(records) == {"f": (1, 2), "関数": (4, 5)}
    by_name = {r.name: r for r in records}
    sel = by_name["f"].selection
    assert sel is not None
    assert (sel.start.line, sel.start.col, sel.end.col) == (0, 4, 5)
    sel = by_name["関数"].selection
    assert sel is not None
    assert (sel.start.col, sel.end.col) == (4, 6)  # コードポイント単位


def test_records_round_trip_through_json() -> None:
    source = (FIXTURES / "legacy" / "calc.cob").read_bytes()
    records, _ = block_outline(source, rel=rel("legacy/calc.cob"), workspace_id=WS)
    for r in records:
        again = SymbolRecord.from_json(r.to_json())
        assert again.urn == r.urn
        assert again.range.line_span_external() == r.range.line_span_external()
        assert again.confidence is Confidence.HEURISTIC


# --- 字句検索 ----------------------------------------------------------------------------------


def test_lexical_find_puts_the_definition_line_first_in_rules_custom() -> None:
    source = (FIXTURES / "conf" / "rules.custom").read_bytes()
    hits = lexical_find(source, "MAX_RETRY")
    assert [(h.line + 1, h.is_definition) for h in hits] == [(2, True), (3, False)]
    assert hits[0].line_text == "rule MAX_RETRY = 3"
    assert "MAX_RETRY * 100" in hits[1].line_text
    assert (hits[0].col, hits[0].end_col) == (5, 14)
    assert hits[0].selector_line == 2


def test_lexical_find_requires_word_boundaries() -> None:
    source = b"MAX_RETRY_X = 1\nxMAX_RETRY = 2\nMAX_RETRY2 = 3\nuse(MAX_RETRY)\n-MAX_RETRY-\n"
    hits = lexical_find(source, "MAX_RETRY")
    assert [h.line + 1 for h in hits] == [4, 5]


def test_lexical_find_orders_definition_like_lines_first() -> None:
    source = b"x = foo + 1\ndef foo(a):\n    if foo == 3: pass\n    foo: 1\n"
    hits = lexical_find(source, "foo")
    assert [(h.line + 1, h.is_definition) for h in hits] == [(2, True), (4, True), (1, False), (3, False)]


def test_lexical_find_snippet_has_context_lines_and_clamps_at_the_edges() -> None:
    source = b"\n".join(b"line%d" % i for i in range(1, 11)) + b"\n"
    (hit,) = lexical_find(source, "line5")
    assert (hit.snippet_start, hit.snippet_end) == (2, 6)
    assert hit.snippet.split("\n") == ["line3", "line4", "line5", "line6", "line7"]
    (first,) = lexical_find(source, "line1")
    assert first.snippet.split("\n") == ["line1", "line2", "line3"]
    (last,) = lexical_find(source, "line10")
    assert last.snippet.split("\n") == ["line8", "line9", "line10"]
    (alone,) = lexical_find(source, "line5", context=0)
    assert alone.snippet == "line5"


@pytest.mark.parametrize("name", ["a.b", "x(", "(", "[", "*", "a+b", "\\", "$x", "a|b", "{", "?"])
def test_regex_metacharacters_in_names_are_matched_literally(name: str) -> None:
    source = ("zz " + name + " zz\n" + "aXb ax b a.bc x1 nothing here\n").encode()
    hits = lexical_find(source, name)
    assert [h.line for h in hits][:1] == [0]
    assert all(h.line == 0 for h in hits), "メタ文字が正規表現として働いて別の行に当たった"


def test_dotted_and_paren_names_respect_boundaries_only_on_word_ends() -> None:
    assert [h.line for h in lexical_find(b"axb\na.b\na.bc\n(a.b)\n", "a.b")] == [1, 3]
    assert [h.line for h in lexical_find(b"x(1)\nxx(1)\ny x (1)\n", "x(")] == [0]


def test_lexical_find_rejects_empty_and_oversized_names() -> None:
    with pytest.raises(ValueError):
        lexical_find(b"abc", "")
    with pytest.raises(ValueError):
        lexical_find(b"abc", "a" * (REGEX_PATTERN_MAX_CHARS + 1))
    with pytest.raises(ValueError):
        lexical_find(b"abc", "a", context=-1)


def test_lexical_find_on_empty_and_binaryish_sources() -> None:
    assert lexical_find(b"", "a") == []
    assert [h.line for h in lexical_find(b"\xff\xfe a b\nzzz\n", "a")] == [0]  # 不正な UTF-8 でも落ちない


def test_lexical_scan_reports_timeout_and_truncation() -> None:
    source = b"x\n" * 10
    timed = lexical_scan(source, "x", budget_ms=-1)
    assert timed.timed_out and timed.hits == []
    capped = lexical_scan(source, "x", max_hits=3)
    assert capped.truncated and len(capped.hits) == 3 and not capped.timed_out
    full = lexical_scan(source, "x")
    assert not full.truncated and not full.timed_out and len(full.hits) == 10


def test_long_lines_are_windowed_around_the_match() -> None:
    line = "a" * 600 + " needle " + "b" * 600
    (hit,) = lexical_find(line.encode(), "needle")
    assert len(hit.line_text) <= 200
    assert "needle" in hit.line_text
    assert len(hit.snippet) <= 200


def test_cobol_paragraph_name_counts_as_a_definition() -> None:
    source = (FIXTURES / "legacy" / "calc.cob").read_bytes()
    hits = lexical_find(source, "COMPUTE-TAX")
    assert [(h.line + 1, h.is_definition) for h in hits] == [(12, True), (9, False)]


# --- block_window ------------------------------------------------------------------------------


def lines_source(n: int) -> bytes:
    return b"\n".join(b"l%d" % i for i in range(n)) + b"\n"


def test_block_window_is_at_most_31_lines() -> None:
    rng = block_window(lines_source(100), 50)
    assert rng.line_span_external() == (36, 66)
    assert rng.line_span_external()[1] - rng.line_span_external()[0] + 1 == 31
    wide = block_window(lines_source(100), 50, radius=500)  # 半径は 15 に抑える
    assert wide.line_span_external() == (36, 66)
    assert BLOCK_WINDOW_RADIUS == 15


def test_block_window_clamps_at_both_ends_and_out_of_range_lines() -> None:
    src = lines_source(20)
    assert block_window(src, 0).line_span_external() == (1, 16)
    assert block_window(src, 19).line_span_external() == (5, 20)
    assert block_window(src, 5, radius=2).line_span_external() == (4, 8)
    assert block_window(src, 10_000).line_span_external() == (5, 20)
    assert block_window(src, -5).line_span_external() == (1, 16)
    assert block_window(src, 3, radius=0).line_span_external() == (4, 4)


def test_block_window_small_and_empty_files() -> None:
    assert block_window(b"one\ntwo\n", 0).line_span_external() == (1, 2)
    empty = block_window(b"", 0)
    assert empty.is_empty
    with pytest.raises(ValueError):
        block_window(b"x", 0, radius=-1)


# --- ctags の JSON の解釈（ctags 無しでも通る） --------------------------------------------------

CTAGS_SOURCE = "alpha\n  x\n  y\n\nBeta\n  z\n\nGamma\n"
CTAGS_JSON = "\n".join(
    [
        '{"_type": "ptag", "name": "JSON_OUTPUT_VERSION", "path": "0.0", "pattern": "0.0"}',
        '{"_type": "tag", "name": "alpha", "path": "x", "pattern": "/^alpha$/", "line": 1, "kind": "function", "end": 3}',
        '{"_type": "tag", "name": "Beta", "path": "x", "line": 5, "kind": "class", "scope": "Outer", "scopeKind": "namespace"}',
        '{"_type": "tag", "name": "alpha", "path": "x", "line": 1, "kind": "function"}',
        '{"_type": "tag", "name": "far", "path": "x", "line": 99, "kind": "function"}',
        '{"_type": "tag", "name": "p", "path": "x", "line": 2, "kind": "parameter"}',
        '{"_type": "tag", "name": "Gamma", "path": "x", "line": 8, "kind": "weird-kind", "end": 8}',
        '{"_type": "tag", "name": "noline", "path": "x", "kind": "function"}',
        "this is not json",
        "[1, 2]",
    ]
)


def test_parse_ctags_json_reads_tags_and_skips_the_rest() -> None:
    records = parse_ctags_json(CTAGS_JSON, source=CTAGS_SOURCE.encode(), rel=rel("d/f.x"), workspace_id=WS)
    assert [r.name for r in records] == ["alpha", "Beta", "Gamma"]
    assert spans(records) == {
        "alpha": (1, 3),
        "Beta": (5, 6),
        "Gamma": (8, 8),
    }  # Beta は end が無く字下げで推定
    by = {r.name: r for r in records}
    assert by["alpha"].kind is Kind.FUNCTION
    assert by["Beta"].kind is Kind.CLASS
    assert by["Beta"].qualified_name == "Outer.Beta"
    assert by["Gamma"].kind is Kind.SECTION  # 未知の kind
    assert str(by["Beta"].urn) == "lk://app/d/f.x#ctags:Beta@L5"
    assert all(r.confidence is Confidence.HEURISTIC and r.path == "d/f.x" for r in records)


def test_parse_ctags_json_trims_trailing_blank_lines_from_end() -> None:
    line = '{"_type": "tag", "name": "alpha", "line": 1, "kind": "function", "end": 4}'
    (rec,) = parse_ctags_json(line.encode(), source=CTAGS_SOURCE.encode(), rel=rel("f.x"), workspace_id=WS)
    assert rec.range.line_span_external() == (1, 3)


def test_parse_ctags_json_on_empty_output() -> None:
    assert parse_ctags_json(b"", source=b"x\n", rel=rel("f.x"), workspace_id=WS) == []


# --- ctags の子プロセス経路（偽の ctags で通す） -------------------------------------------------

FAKE_CTAGS = """\
import os
import sys
import time

mode = sys.argv[1]
args = sys.argv[2:]
if mode == "ok":
    # ctags 互換の呼ばれ方を確かめる: JSON 出力・end フィールド・標準出力へ出す（-f -）・相対のファイル名
    assert "--output-format=json" in args and "--fields=+ne" in args, args
    assert args[-3:-1] == ["-f", "-"], args
    name = args[-1]
    assert os.path.basename(name) == name and os.path.isfile(name), (name, os.getcwd())
    for a in args:  # 一時ファイルは呼び出し側が指定した置き場の直下の専用ディレクトリに作られている
        if a.startswith("--stage-root="):
            stage_root = a.split("=", 1)[1]
            assert os.path.realpath(os.path.dirname(os.getcwd())) == os.path.realpath(stage_root), (os.getcwd(), stage_root)
    assert open(name, "rb").read().startswith(b"alpha"), "source was not staged"
    print('{"_type": "ptag", "name": "JSON_OUTPUT_VERSION", "path": "0.0"}')
    print('{"_type": "tag", "name": "alpha", "path": "%s", "line": 1, "kind": "function", "end": 3}' % name)
    print('{"_type": "tag", "name": "Beta", "path": "%s", "line": 5, "kind": "class"}' % name)
    sys.stdout.flush()
    assert os.listdir(".") == [name], os.listdir(".")  # tags ファイルを作っていない
elif mode == "empty":
    pass
elif mode == "garbage":
    print("not json at all")
elif mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(3)
elif mode == "hang":
    time.sleep(60)
"""

ARRAY_SOURCE = b"alpha\n  x\n  y\n\nBeta\n  z\n"


@pytest.fixture
def fake_ctags(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_ctags.py"
    script.write_text(FAKE_CTAGS, encoding="utf-8")
    return [sys.executable, str(script)]


async def test_outline_uses_ctags_through_a_child_process(fake_ctags: list[str], tmp_path: Path) -> None:
    # ブロック推定では何も取れない入力。ctags 経路を通った証拠になる。
    assert block_outline(ARRAY_SOURCE, rel=rel("d/f.unk"), workspace_id=WS)[1] is Confidence.LEXICAL_FALLBACK
    # 一時物の置き場はこのテスト専用にする。共有の一時ディレクトリを数えると、同時に走る別のプロセスの一時物を拾って揺れる（BUG-0009）。
    stage = tmp_path / "stage"
    stage.mkdir()
    reg = ProcessRegistry()
    records, conf = await outline(
        ARRAY_SOURCE,
        rel=rel("d/f.unk"),
        workspace_id=WS,
        ctags_cmd=[*fake_ctags, "ok", f"--stage-root={stage}"],
        registry=reg,
        staging_root=stage,
    )
    assert conf is Confidence.HEURISTIC
    assert spans(records) == {"alpha": (1, 3), "Beta": (5, 6)}
    assert str(records[0].urn) == "lk://app/d/f.unk#ctags:alpha@L1"
    assert reg.live() == []
    assert list(stage.iterdir()) == [], "一時ディレクトリが残った"


async def test_outline_without_a_registry_owns_and_cleans_up_its_own(fake_ctags: list[str]) -> None:
    records, conf = await outline(
        ARRAY_SOURCE, rel=rel("f.unk"), workspace_id=WS, ctags_cmd=[*fake_ctags, "ok"]
    )
    assert conf is Confidence.HEURISTIC and len(records) == 2


@pytest.mark.parametrize("mode", ["empty", "garbage", "fail"])
async def test_outline_falls_back_to_block_estimation_when_ctags_gives_nothing(
    fake_ctags: list[str], mode: str
) -> None:
    source = b"def f():\n    pass\n"
    reg = ProcessRegistry()
    records, conf = await outline(
        source, rel=rel("f.unk"), workspace_id=WS, ctags_cmd=[*fake_ctags, mode], registry=reg
    )
    assert conf is Confidence.HEURISTIC
    assert spans(records) == {"f": (1, 2)}
    assert reg.live() == []


async def test_outline_gives_up_on_a_hanging_ctags_and_kills_it(fake_ctags: list[str]) -> None:
    reg = ProcessRegistry()
    started = time.monotonic()
    records, conf = await outline(
        b"def f():\n    pass\n",
        rel=rel("f.unk"),
        workspace_id=WS,
        ctags_cmd=[*fake_ctags, "hang"],
        registry=reg,
        timeout_ms=300,
    )
    assert time.monotonic() - started < 20
    assert conf is Confidence.HEURISTIC and spans(records) == {"f": (1, 2)}
    assert reg.live() == []


async def test_outline_survives_a_missing_ctags_executable() -> None:
    reg = ProcessRegistry()
    records, conf = await outline(
        b"def f():\n    pass\n",
        rel=rel("f.unk"),
        workspace_id=WS,
        ctags_cmd=["this-ctags-does-not-exist-lucianke"],
        registry=reg,
    )
    assert conf is Confidence.HEURISTIC and spans(records) == {"f": (1, 2)}


async def test_outline_without_ctags_and_without_definitions_is_lexical_fallback() -> None:
    records, conf = await outline(b"just words\n", rel=rel("f.unk"), workspace_id=WS, ctags_cmd=None)
    assert records == [] and conf is Confidence.LEXICAL_FALLBACK


async def test_outline_cobol_without_ctags() -> None:
    source = (FIXTURES / "legacy" / "calc.cob").read_bytes()
    records, conf = await outline(source, rel=rel("legacy/calc.cob"), workspace_id=WS, ctags_cmd=None)
    assert conf is Confidence.HEURISTIC
    assert [r.name for r in records] == ["MAIN-PARA", "COMPUTE-TAX", "WRITE-REPORT"]


# --- find_ctags -------------------------------------------------------------------------------


async def test_find_ctags_returns_none_when_nothing_is_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heuristic.shutil, "which", lambda _name: None)
    assert await find_ctags() is None


async def test_find_ctags_rejects_a_ctags_that_is_not_universal(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``python --version`` は "Python 3.x" と答える（= BSD ctags など Universal Ctags でないもの）
    monkeypatch.setattr(heuristic.shutil, "which", lambda _name: sys.executable)
    assert await find_ctags() is None


async def test_find_ctags_accepts_universal_ctags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if sys.platform == "win32":
        exe = tmp_path / "ctags.cmd"
        exe.write_text(
            "@echo off\r\necho Universal Ctags 6.1.0, Copyright (C) 2015-2023\r\n", encoding="utf-8"
        )
    else:
        exe = tmp_path / "ctags"
        exe.write_text("#!/bin/sh\necho 'Universal Ctags 6.1.0, Copyright (C) 2015-2023'\n", encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(heuristic.shutil, "which", lambda name: str(exe) if name == "ctags" else None)
    assert await find_ctags() == [str(exe)]


async def test_real_ctags_outline() -> None:
    ctags = await find_ctags()
    if ctags is None:
        pytest.skip(
            "Universal Ctags が PATH に無い（GPL のため同梱しない。子プロセス経路は偽の ctags のテストで通している）"
        )
    source = b"def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    reg = ProcessRegistry()
    records, conf = await outline(
        source, rel=rel("sample.py"), workspace_id=WS, ctags_cmd=ctags, registry=reg
    )
    assert conf is Confidence.HEURISTIC
    assert {"alpha", "beta"} <= {r.name for r in records}
    assert reg.live() == []
