"""edit.syntax_check のテスト（仕様書 §10.3 段 4・§11 T-10 処理規約 3、設計書 §7.7.4）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lucianke.core.model import Position, Range
from lucianke.core.textpos import BOM, LineIndex
from lucianke.edit.syntax_check import (
    MAX_REPORTED_ERRORS,
    SyntaxCheckResult,
    SyntaxIssue,
    check_after,
    check_fragment,
    starts_with_definition,
    to_error_details,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def apply(before: str, old: str, new: str) -> tuple[bytes, list[Range]]:
    """``before`` の ``old``（1 か所だけ現れる）を ``new`` に置き換え、``(適用後のバイト列, 編集後座標の範囲)`` を返す。"""
    assert before.count(old) == 1
    start = before.index(old)
    after = before[:start] + new + before[start + len(old) :]
    idx = LineIndex(after.encode())
    # 編集後の範囲: 置換したテキストの先頭から末尾まで
    a = idx.offset_to_position(len(after[:start].encode())).value
    b = idx.offset_to_position(len((after[:start] + new).encode())).value
    return after.encode(), [Range(a, b)]


PY = """\
def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b
"""

# `def broken(:` は MISSING ")" を 1 つ生む。ファイルの先頭に置き、離れた箇所を編集する
PY_BROKEN = (
    "def broken(:\n    pass\n"
    + "\n" * 8
    + "\n".join(f"x{i} = {i}" for i in range(8))
    + "\n\n\ndef good():\n    return 1\n"
)

TS = """\
function add(a: number, b: number): number {
  return a + b;
}

function sub(a: number, b: number): number {
  return a - b;
}

function mul(a: number, b: number): number {
  return a * b;
}
"""

TS_BROKEN = (
    "const broken = ;\n"
    + "\n" * 8
    + "\n".join(f"const x{i} = {i};" for i in range(8))
    + "\n\n\nfunction good() {\n  return 1;\n}\n"
)

CASES: dict[str, dict[str, Any]] = {
    "python": {
        "src": PY,
        "good": ("return a - b", "return (a - b)"),
        "bad": ("return a - b", "return (a - b"),
        "broken": PY_BROKEN,
        "far_edit": ("return 1", "return 2"),
        "far_bad_edit": ("return 1", "return (1"),
    },
    "typescript": {
        "src": TS,
        "good": ("return a - b;", "return (a - b);"),
        "bad": ("return a - b;", "return (a - b;"),
        "broken": TS_BROKEN,
        "far_edit": ("return 1;", "return 2;"),
        "far_bad_edit": ("return 1;", "return (1;"),
    },
}


@pytest.fixture(params=sorted(CASES))
def case(request: pytest.FixtureRequest) -> tuple[str, dict[str, Any]]:
    return request.param, CASES[request.param]


# --- check_after -------------------------------------------------------------------------


def test_valid_replacement_is_ok(case):
    lang, c = case
    after, ranges = apply(c["src"], *c["good"])
    r = check_after(lang, c["src"].encode(), after, edited_ranges_after=ranges)
    assert r == SyntaxCheckResult("ok", [], 0, 0)


def test_unclosed_paren_fails_with_line_number(case):
    lang, c = case
    after, ranges = apply(c["src"], *c["bad"])
    r = check_after(lang, c["src"].encode(), after, edited_ranges_after=ranges)
    assert r.status == "failed"
    assert r.before_error_count == 0
    assert r.after_error_count >= 1
    assert r.new_errors
    edit_line = ranges[0].start.line  # 0 始まり。壊れた行は 6 行目（1 始まり）
    assert edit_line == 5
    errors = to_error_details(r)["errors"]
    lines = [e["line"] for e in errors]
    assert all(isinstance(n, int) and n >= 1 for n in lines)
    assert any(edit_line + 1 <= n <= edit_line + 3 for n in lines), lines  # 壊れた行の近く（1 始まり）
    assert all(e["message"].startswith("syntax error") for e in errors)


def test_edit_without_ranges_uses_diff(case):
    """``edited_ranges_after`` を渡さなくても、2 つのバイト列の違いから窓を決めて判定できる。"""
    lang, c = case
    after, _ = apply(c["src"], *c["bad"])
    assert check_after(lang, c["src"].encode(), after).status == "failed"
    after, _ = apply(c["src"], *c["good"])
    assert check_after(lang, c["src"].encode(), after).status == "ok"


def test_preexisting_error_is_not_asked_far_from_edit(case):
    lang, c = case
    before = c["broken"]
    after, ranges = apply(before, *c["far_edit"])
    r = check_after(lang, before.encode(), after, edited_ranges_after=ranges)
    assert r.status == "ok"
    assert r.before_error_count >= 1
    assert r.after_error_count == r.before_error_count
    assert r.new_errors == []


def test_new_error_near_edit_fails_even_if_file_was_already_broken(case):
    lang, c = case
    before = c["broken"]
    after, ranges = apply(before, *c["far_bad_edit"])
    r = check_after(lang, before.encode(), after, edited_ranges_after=ranges)
    assert r.status == "failed"
    assert r.after_error_count > r.before_error_count >= 1
    edit_line = ranges[0].start.line
    assert any(abs(i.range.start.line - edit_line) <= 1 for i in r.new_errors), (edit_line, r.new_errors)


def test_clean_file_requires_zero_errors_anywhere(case):
    """編集前にエラーが無ければ、申告された編集範囲から離れた場所のエラーでも失敗にする（ファイル全体で 0）。"""
    lang, c = case
    after, _ = apply(c["src"], *c["bad"])
    far = [Range(Position(0, 0), Position(0, 1))]
    r = check_after(lang, c["src"].encode(), after, edited_ranges_after=far)
    assert r.status == "failed"


def test_fixing_the_broken_line_is_ok(case):
    lang, c = case
    bad, _ = apply(c["src"], *c["bad"])
    r = check_after(lang, bad, c["src"].encode())
    assert r.status == "ok"  # 編集前にあったエラーを減らす編集は通す
    assert r.before_error_count >= 1
    assert r.after_error_count == 0


def test_identity_edit_is_ok_even_when_file_is_broken(case):
    lang, c = case
    src = c["broken"].encode()
    r = check_after(lang, src, src)
    assert r.status == "ok"
    assert r.new_errors == []
    assert r.before_error_count == r.after_error_count >= 1


def test_insertion_and_deletion_of_whole_functions(case):
    lang, c = case
    src = c["src"]
    if lang == "python":
        extra, sep = "\n\ndef div(a, b):\n    return a / b\n", "\n\n\n"
    else:
        extra, sep = "\nfunction div(a: number, b: number) {\n  return a / b;\n}\n", "\n\n"
    n = LineIndex(src.encode()).line_count
    r = check_after(
        lang,
        src.encode(),
        (src + extra).encode(),
        edited_ranges_after=[Range(Position(n - 1, 0), Position(n + 3, 0))],
    )
    assert r.status == "ok"
    parts = src.split(sep)
    r = check_after(lang, src.encode(), sep.join(parts[:1] + parts[2:]).encode())
    assert r.status == "ok"


def test_language_without_grammar_is_skipped():
    r = check_after("no-such-language", b"anything ((", b"anything ((((")
    assert r == SyntaxCheckResult("skipped", [], 0, 0)
    assert check_fragment("no-such-language", "x ((").status == "skipped"


def test_bom_is_ignored_on_either_side():
    src = PY.encode()
    after, ranges = apply(PY, "return a - b", "return (a - b)")
    assert check_after("python", BOM + src, after, edited_ranges_after=ranges).status == "ok"
    assert check_after("python", src, BOM + after, edited_ranges_after=ranges).status == "ok"
    bad, ranges = apply(PY, "return a - b", "return (a - b")
    assert check_after("python", BOM + src, BOM + bad, edited_ranges_after=ranges).status == "failed"


def test_new_errors_are_capped():
    src = "\n".join(f"x{i} = {i}" for i in range(40)) + "\n"
    broken = "\n".join(f"x{i} = (" for i in range(40)) + "\n"
    r = check_after("python", src.encode(), broken.encode())
    assert r.status == "failed"
    assert 1 <= len(r.new_errors) <= MAX_REPORTED_ERRORS


# --- BOM・CRLF のフィクスチャ --------------------------------------------------------------


CRLF_BOM = FIXTURES / "poly" / "src" / "shop" / "models" / "crlf_bom.py"


def test_crlf_bom_fixture_identity_edit_is_ok():
    data = CRLF_BOM.read_bytes()
    assert data.startswith(BOM)
    assert b"\r\n" in data
    r = check_after("python", data, data, edited_ranges_after=[Range(Position(0, 0), Position(2, 0))])
    assert r.status == "ok"
    assert r.after_error_count == 0


def test_crlf_bom_fixture_real_edits():
    data = CRLF_BOM.read_bytes()
    text = data.decode("utf-8-sig")
    old = "def crlf_function(x: int) -> int:"
    assert old in text
    # 正しい編集（CRLF のまま。BOM は片側にだけ付いていても構わない）
    good, ranges = apply(text, old, "def crlf_function(x: int, y: int = 0) -> int:")
    assert check_after("python", data, good, edited_ranges_after=ranges).status == "ok"
    assert check_after("python", data, BOM + good, edited_ranges_after=ranges).status == "ok"
    # 壊す編集
    bad, ranges = apply(text, old, "def crlf_function(x: int -> int:")
    r = check_after("python", data, bad, edited_ranges_after=ranges)
    assert r.status == "failed"
    assert to_error_details(r)["errors"][0]["line"] >= 1


# --- check_fragment ----------------------------------------------------------------------


def test_fragment_method_in_class_body_is_ok_python():
    code = "    def total(self) -> int:\n        return 1\n"
    assert check_fragment("python", code, wrap="class_body") == SyntaxCheckResult("ok", [], 0, 0)
    # 字下げ無しの断片でも、共通先頭空白を除いて包むので ok。デコレータ付きも ok
    assert check_fragment("python", "def total(self):\n    return 1\n", wrap="class_body").status == "ok"
    assert (
        check_fragment("python", "@property\ndef x(self):\n    return 1\n", wrap="class_body").status == "ok"
    )


def test_fragment_comment_only_in_python_class_body_is_ok():
    assert check_fragment("python", "# TODO: later\n", wrap="class_body").status == "ok"


def test_fragment_method_in_class_body_is_ok_typescript():
    code = "  total(): number {\n    return 1;\n  }\n"
    assert check_fragment("typescript", code, wrap="class_body").status == "ok"
    assert check_fragment("typescript", "private x = 1;", wrap="class_body").status == "ok"
    # クラス本体の外ではメソッド記法は構文として成り立たない（包まなければ failed）
    assert check_fragment("typescript", code, wrap="none").status == "failed"


def test_fragment_broken_fails_with_fragment_coordinates():
    r = check_fragment("python", "def total(self):\n    return (1\n", wrap="class_body")
    assert r.status == "failed"
    assert r.new_errors
    lines = [e["line"] for e in to_error_details(r)["errors"]]
    assert all(1 <= n <= 2 for n in lines), lines  # 容器のヘッダ行を引いた、断片内の行
    r = check_fragment("typescript", "total(): number {\n  return (1;\n}\n", wrap="class_body")
    assert r.status == "failed"
    assert all(1 <= e["line"] <= 3 for e in to_error_details(r)["errors"])


def test_fragment_none_and_module():
    assert check_fragment("python", "def f():\n    return 1\n").status == "ok"
    assert check_fragment("python", "def f(:\n    return 1\n").status == "failed"
    assert (
        check_fragment("python", "import os\n\n\ndef f():\n    return os.name\n", wrap="module").status
        == "ok"
    )
    assert (
        check_fragment("typescript", "export function f(): number { return 1; }", wrap="module").status
        == "ok"
    )
    assert check_fragment("typescript", "export function f( { return 1; }", wrap="module").status == "failed"


def test_fragment_crlf_and_trailing_blank_lines():
    assert check_fragment("python", "def f(self):\r\n    return 1\r\n\r\n", wrap="class_body").status == "ok"


# --- to_error_details ----------------------------------------------------------------------


def test_to_error_details_shape():
    r = SyntaxCheckResult(
        "failed",
        [SyntaxIssue(Range(Position(4, 2), Position(4, 9)), "syntax error near 'x'")],
        0,
        1,
    )
    assert to_error_details(r) == {"errors": [{"line": 5, "message": "syntax error near 'x'"}]}
    assert to_error_details(SyntaxCheckResult("ok")) == {"errors": []}


# --- plan.apply_hunks との組み合わせ（複数 hunk・CRLF・BOM） -------------------------------------


def test_multi_hunk_plan_on_crlf_bom_file():
    from lucianke.core.model import RelPath
    from lucianke.edit.plan import Hunk, apply_hunks

    path = RelPath("m.py")
    before = BOM + PY.replace("\n", "\r\n").encode()

    def hunks(first: str) -> list[Hunk]:
        return [
            Hunk(path, Range(Position(1, 4), Position(1, 16)), first),
            Hunk(path, Range(Position(9, 4), Position(9, 16)), "return a ** b"),
        ]

    ranges = [Range(Position(1, 4), Position(1, 20)), Range(Position(9, 4), Position(9, 17))]
    ok_after = apply_hunks(before, hunks("return a + b + 1"))
    assert check_after("python", before, ok_after, edited_ranges_after=ranges).status == "ok"
    bad_after = apply_hunks(before, hunks("return (a + b"))
    r = check_after("python", before, bad_after, edited_ranges_after=ranges)
    assert r.status == "failed"
    assert any(2 <= e["line"] <= 5 for e in to_error_details(r)["errors"])


# --- starts_with_definition（T-10 replace_body の「定義全体か」。BUG-0045）-----------------------------------


@pytest.mark.parametrize(
    "language_id, code, expected",
    [
        # Python: 複数行のデコレータの 2 行目以降（``("a", "b"),`` など）を読み飛ばさず、構文木で判定する
        ("python", "def f():\n    pass\n", True),
        ("python", "async def f():\n    pass\n", True),
        ("python", "class C:\n    x = 1\n", True),
        ("python", "@d\ndef f():\n    pass\n", True),
        ("python", "@pytest.mark.parametrize(\n    ('a', 'b'),\n    [(1, 2)],\n)\ndef f(a, b):\n    pass\n", True),
        (
            "python",
            "@d(  # open (\n    a=[1,\n       2],\n    # note )\n    b={'k': (1, 2)},  # tail )\n)\n@e\nasync def f():\n    pass\n",
            True,
        ),
        ("python", "# lead\n@d(\n    1,\n)\nclass C: ...\n", True),
        ("python", "    @d(\n        1,\n    )\n    def m(self):\n        pass\n", True),  # 字下げされた断片
        ("python", "@d(\r\n    1,\r\n)\r\ndef f():\r\n    pass\r\n", True),
        ("python", "return a + b\n", False),
        ("python", "x = (\n    1,\n)\n", False),
        ("python", "# only a comment\n", False),
        ("python", "@d(\n    1,\n)\nx = 1\n", None),  # デコレータの後が定義でない → 構文エラー（報告は check_fragment の仕事）
        ("python", "def f(:\n", None),
        # Rust: 複数行の属性・Doc コメント・ブロックコメントを読み飛ばす
        ("rust", "fn f() {}\n", True),
        ("rust", "pub async fn f() {}\n", True),
        ("rust", "#[derive(\n    Debug,\n    Clone,\n)]\npub struct S {\n    pub x: i32,\n}\n", True),
        ("rust", '#[cfg_attr(\n    feature = "x",\n    derive(Debug)\n)]\n/// doc\n/* a\n b */\nimpl S {}\n', True),
        ("rust", "unsafe impl Send for S {}\n", True),
        ("rust", "const N: u32 = 1;\n", True),
        ("rust", "let y = 2;\n", False),
        ("rust", "x * 2;\n", False),
        ("rust", "#[inline]\n", False),
        # 対象外の言語（構文検証だけで判定する）
        ("typescript", "@Component({\n  a: 1,\n})\nexport class C {}\n", None),
        ("go", "func f() {}\n", None),
    ],
)  # fmt: skip
def test_starts_with_definition(language_id: str, code: str, expected: bool | None) -> None:
    assert starts_with_definition(language_id, code) is expected
