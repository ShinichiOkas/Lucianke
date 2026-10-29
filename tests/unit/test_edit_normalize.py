"""edit/normalize.py の単体テスト（仕様書 §10.9・T-10 処理規約 4）。"""

from __future__ import annotations

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.edit.normalize import blank_lines_between, default_indent, normalize_new_code

PY4 = b"class A:\n    def f(self):\n        return 1\n\n    def g(self):\n        if self:\n            return 2\n"
TAB = b"class A:\n\tdef f(self):\n\t\treturn 1\n\n\tdef g(self):\n\t\tif self:\n\t\t\treturn 2\n"
SP2 = b"function f() {\n  if (x) {\n    return 1;\n  }\n}\n"


def norm(code: str, source: bytes = PY4, *, col: int = 0, lang: str = "python", mode: str = "insert") -> str:
    return normalize_new_code(code, target_source=source, base_column=col, language_id=lang, mode=mode)  # type: ignore[arg-type]


# --- インデント単位の変換 ---------------------------------------------------------------------


def test_two_space_code_into_four_space_file():
    code = "def f():\n  if x:\n    return 1\n  return 2\n"
    assert norm(code) == "def f():\n    if x:\n        return 1\n    return 2\n"


def test_four_space_code_into_two_space_file():
    code = "function g() {\n    if (y) {\n        return 2;\n    }\n}\n"
    assert norm(code, SP2, lang="javascript") == "function g() {\n  if (y) {\n    return 2;\n  }\n}\n"


def test_space_code_into_tab_file():
    code = "def f():\n    if x:\n        return 1\n"
    assert norm(code, TAB) == "def f():\n\tif x:\n\t\treturn 1\n"


def test_tab_code_into_space_file():
    code = "def f():\n\tif x:\n\t\treturn 1\n"
    assert norm(code) == "def f():\n    if x:\n        return 1\n"


def test_tab_code_into_tab_file_is_unchanged():
    code = "def f():\n\tif x:\n\t\treturn 1\n"
    assert norm(code, TAB) == code


def test_same_unit_is_unchanged():
    code = "def f():\n    return 1\n"
    assert norm(code) == code


def test_common_leading_whitespace_is_removed():
    code = "        def f():\n            return 1\n"
    assert norm(code) == "def f():\n    return 1\n"


def test_common_prefix_removed_with_eight_space_unit_in_tab_file():
    code = "    def f():\n        return 1\n"
    assert norm(code, TAB, col=1) == "\tdef f():\n\t\treturn 1\n"


def test_blank_lines_inside_do_not_break_common_prefix_and_get_no_indent():
    code = "    def f():\n\n        return 1\n   \n    def g():\n        return 2\n"
    out = norm(code, col=4)
    assert out == "    def f():\n\n        return 1\n\n    def g():\n        return 2\n"


def test_partial_unit_remainder_is_kept_as_alignment():
    code = "def f():\n    call(a,\n         b)\n"  # 継続行は 9 スペース = 4 * 2 + 1
    assert norm(code, TAB) == "def f():\n\tcall(a,\n\t\t b)\n"


def test_three_space_code_unit_is_inferred():
    code = "def f():\n   if x:\n      return 1\n"
    assert norm(code) == "def f():\n    if x:\n        return 1\n"


def test_unindented_code_is_only_prefixed():
    assert norm("x = 1\n", col=4) == "    x = 1\n"


# --- 基準列 ---------------------------------------------------------------------------------


def test_base_column_eight_insert_prefixes_every_line():
    code = "def h(self):\n    return 3\n"
    assert norm(code, col=8) == "        def h(self):\n            return 3\n"


def test_base_column_replace_leaves_first_line_unindented():
    code = "def h(self):\n    return 3\n"
    assert norm(code, col=4, mode="replace") == "def h(self):\n        return 3\n"


def test_base_column_in_tab_file_uses_tabs():
    code = "def h(self):\n    return 3\n"
    assert norm(code, TAB, col=2) == "\t\tdef h(self):\n\t\t\treturn 3\n"


def test_base_column_zero_replace_equals_insert():
    code = "def h():\n    return 3\n"
    assert norm(code, mode="replace") == norm(code, mode="insert")


def test_negative_base_column_is_a_bug():
    with pytest.raises(ValueError):
        norm("x\n", col=-1)


# --- 混在の拒否 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "def f():\n\tif x:\n        return 1\n",  # 行ごとにタブとスペース
        "def f():\n    if x:\n\treturn 1\n",
        "def f():\n \tx = 1\n",  # スペースのあとにタブ
        "def f():\n\tx = 1\n    y = 2\n",
    ],
)
def test_mixed_indent_is_rejected(code):
    with pytest.raises(LuciankeError) as ei:
        norm(code)
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details["param"] == "new_code"
    assert ei.value.details["reason"] == "mixed_indent"


def test_tab_in_the_middle_of_a_line_is_fine():
    assert norm("x = 1\t# note\n") == "x = 1\t# note\n"


def test_tab_then_alignment_spaces_is_allowed():
    out = norm("f(a,\n\t b)\n", TAB)
    assert out == "f(a,\n\t b)\n"


@pytest.mark.parametrize("code", ["", "\n\n", "   \n\t\n"])
def test_empty_new_code_is_rejected(code):
    with pytest.raises(LuciankeError) as ei:
        norm(code)
    assert ei.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert ei.value.details["param"] == "new_code"
    assert ei.value.details["reason"] == "empty"


# --- 改行コード・末尾 --------------------------------------------------------------------------


def test_lf_code_into_crlf_file():
    source = PY4.replace(b"\n", b"\r\n")
    out = norm("def f():\n    return 1\n", source)
    assert out == "def f():\r\n    return 1\r\n"


def test_crlf_code_into_lf_file():
    assert norm("def f():\r\n    return 1\r\n") == "def f():\n    return 1\n"


def test_lone_cr_code_is_split_as_lines():
    assert norm("a = 1\rb = 2\r") == "a = 1\nb = 2\n"


def test_target_without_newlines_defaults_to_lf():
    assert norm("a = 1\n", b"x = 0") == "a = 1\n"


def test_cr_file_gets_cr():
    assert norm("a = 1\nb = 2\n", b"x = 0\ry = 1\r") == "a = 1\rb = 2\r"


@pytest.mark.parametrize(
    "code",
    ["a = 1", "a = 1\n", "a = 1\n\n", "a = 1\n\n\n\n", "a = 1\n  \n\t\n", "a = 1\r\n\r\n"],
)
def test_trailing_blank_lines_collapse_to_one_newline(code):
    assert norm(code) == "a = 1\n"


def test_leading_blank_lines_are_dropped_but_inner_blank_lines_stay():
    assert norm("\n\na = 1\n\n\nb = 2\n") == "a = 1\n\n\nb = 2\n"


def test_bom_in_new_code_is_dropped():
    assert norm("\ufeffa = 1\n") == "a = 1\n"


def test_non_ascii_survives():
    assert norm("名前 = '😀'\n", col=4) == "    名前 = '😀'\n"


def test_trailing_whitespace_on_code_lines_is_kept():
    assert norm("a = 1  \nb = 2\n") == "a = 1  \nb = 2\n"


# --- 対象ファイルの単位が決まらないとき -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("lang", "unit_text"),
    [
        ("python", "    "),
        ("typescript", "  "),
        ("javascript", "  "),
        ("rust", "    "),
        ("go", "\t"),
        ("nope", "    "),
    ],
)
def test_language_default_unit_when_file_has_no_evidence(lang, unit_text):
    out = norm("def f():\n  return 1\n", b"", lang=lang)
    assert out == "def f():\n" + unit_text + "return 1\n"


def test_default_indent_values():
    assert default_indent("python").label == "4sp"
    assert default_indent("typescript").label == "2sp"
    assert default_indent("go").label == "tab"
    assert default_indent("no-such-language").label == "4sp"


def test_file_majority_beats_language_default():
    assert norm("def f():\n  return 1\n", SP2, lang="python") == "def f():\n  return 1\n"


# --- blank_lines_between ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lang", "toplevel", "expected"),
    [
        ("python", True, 2),
        ("python", False, 1),
        ("typescript", True, 1),
        ("typescript", False, 1),
        ("go", True, 1),
        ("rust", False, 1),
    ],
)
def test_blank_lines_between(lang, toplevel, expected):
    assert blank_lines_between(lang, toplevel=toplevel) == expected
