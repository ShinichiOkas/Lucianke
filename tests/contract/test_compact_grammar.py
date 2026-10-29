"""Compact DSL の検査器 ``compact_grammar.validate`` の契約テスト（仕様書 §6.1.1 の例と陽性対照）。

検査器が「通すべきものを通し、壊した行を落とす」ことを確かめる。落とせない検査器は無価値なので、
陽性対照（壊した行が不適合になること）を必ず置く。
"""

from __future__ import annotations

import compact_grammar as g
import pytest

HEADER = "# get_file_skeleton ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~310/2000tok\n"

# 仕様書 §6.1.1 の 3 例（そのまま）
EXAMPLE_SKELETON = """\
# get_file_skeleton ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~310/2000tok
=== src/services/payment.py (245 lines, skeleton: 21 lines, sha=3f9c1a2b4c5d) ===
imports: [Decimal from decimal, OrderRepository, PaymentGateway]
[Class] PaymentRequest @L12-18
  doc: 決済要求。amount > 0 が必須
  urn: lk://app/src/services/payment.py#PaymentRequest
[Class] PaymentProcessor @L25-240
  urn: lk://app/src/services/payment.py#PaymentProcessor
  [Constructor] __init__(self, repo: OrderRepository, gateway: PaymentGateway) @L28-32 { ...4 lines omitted }
  [Method] pub async process_charge(self, req: PaymentRequest) -> Receipt @L40-115 { ...75 lines omitted }
    doc: 注文に対する決済を実行し領収書を発行する
    raises: PaymentDeclined, LimitExceeded
  [Method] priv _validate_limit(self, amount: Decimal) -> bool @L117-140 { ...23 lines omitted }
"""

EXAMPLE_REFERENCES = """\
# find_references_and_implementations ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~420/2000tok
target: lk://app/src/services/payment.py#PaymentProcessor.process_charge
implementations: (none; concrete method)
references: 3 files, 5 sites
src/api/routes.py:88:20  [call] in create_order  receipt = await processor.process_charge(req)
src/api/routes.py:131:20  [call] in retry_order  receipt = await processor.process_charge(req)
tests/test_payment.py:44:12  [call] in test_charge_ok  r = await p.process_charge(req)
"""

EXAMPLE_DIAGNOSTICS = """\
# get_diagnostics ok conf=exact lsp=ready rev=WORKING_TREE ws=app ~180/2000tok
E src/services/payment.py:58:16 [pyright:reportArgumentType] "str" は "Decimal" に割り当てできません  fix=qf_7a1c
W src/services/payment.py:9:1 [ruff:F401] `os` imported but unused  fix=qf_02b9
"""


@pytest.mark.parametrize("text", [EXAMPLE_SKELETON, EXAMPLE_REFERENCES, EXAMPLE_DIAGNOSTICS])
def test_spec_examples_conform(text):
    assert g.validate(text) == []


def test_all_line_kinds_conform():
    text = (
        "# find_code_paths ok conf=structural lsp=indexing(40%) rev=HEAD ws=- ~12/2000tok\n"
        "! DEGRADED: confidence=structural engine=tree-sitter lang=unknown(.cob)\n"
        "! Warning: LSP が利用できないため構文木のみで解決しました。\n"
        "source: lk://app/a.py#f  var=x\n"
        "=== notebooks/eda.ipynb (310 lines, 12 cells, skeleton: 9 lines, sha=4f1e9a0b7c2d) ===\n"
        "[Cell] 3 code (exec_count: 12) @L10-48\n"
        "  [Function] load_frame(path: str) -> pd.DataFrame @L12 { ...19 lines omitted }\n"
        "- truncated: 3 members omitted\n"
        "<- create_order @src/api/routes.py:88  when: req.amount > 0  conf=heuristic\n"
        "  <- retry_order @src/api/routes.py:140 (cycle)\n"
        "-> callee @src/a.py:3\n"
        "path 1 (3 hops)\n"
        "  1. create_order @src/api/routes.py:84  var=payload\n"
        "  2. build_query @src/services/order.py:40  var=req.note  when: req.note is not None\n"
        "| urn | caller_count |\n"
        "| lk://app/a.py#f | 7 |\n"
        "```python\n"
        "@L116: def f():\n"
        "this line is not checked inside a fence\n"
        "```\n"
        "# summary: 38 more files (src: 30, tests: 8)\n"
        "# more: cursor=eyJyYW5rIjoyfQ remaining≈38\n"
    )
    assert g.validate(text) == []


def test_error_document_conforms():
    text = (
        "# resolve_symbols error E_TARGET_AMBIGUOUS\n"
        "! symbol_name 'login' は 3 件に一致します\n"
        "! candidate: lk://app/a.py#A.login  (self) -> bool  @a.py:10\n"
    )
    assert g.validate(text) == []


def test_extended_fence_conforms_and_short_fence_inside_does_not_close():
    text = HEADER + "````python\n@L1: x = '''\n```\n'''\n````\n- after\n"
    assert g.validate(text) == []


# --- 陽性対照: 壊した行は不適合になる ------------------------------------------------------------


def _assert_rejects(text: str, needle: str) -> None:
    problems = g.validate(text)
    assert problems, f"検査器が壊した入力を通した: {text!r}"
    assert any(needle in p for p in problems), problems


def test_fold_accepts_lines_and_nodes_omitted():
    """仕様書 §T-02 処理規約 4: 本体は `{ ...N lines omitted }`、データファイルの深さ超過は `{ ...N nodes omitted }`。"""
    assert g.validate(HEADER + "[Tag] testsuite @L3-44 { ...2 nodes omitted }\n") == []
    assert g.validate(HEADER + "[Function] f() @L3-44 { ...2 lines omitted }\n") == []
    _assert_rejects(HEADER + "[Tag] testsuite @L3-44 { ...2 rows omitted }\n", "symbol-block")
    _assert_rejects(HEADER + "[Tag] testsuite @L3-44 { ...nodes omitted }\n", "symbol-block")


def test_rejects_symbol_without_range():
    broken = EXAMPLE_SKELETON.replace("process_charge(self, req: PaymentRequest) -> Receipt @L40-115", "process_charge(self, req: PaymentRequest) -> Receipt")
    _assert_rejects(broken, "symbol-block")


def test_rejects_unknown_attr_key():
    _assert_rejects(EXAMPLE_SKELETON.replace("    raises:", "    raisez:"), "未知の attr-key 'raisez'")


def test_rejects_unknown_kind_and_reversed_range():
    _assert_rejects(HEADER + "[Widget] w @L1-2\n", "未知の kind")
    _assert_rejects(HEADER + "[Class] C @L9-2\n", "終端")


def test_rejects_odd_indent_and_tab():
    _assert_rejects(HEADER + "[Class] C @L1-5\n   urn: lk://a\n", "2 の倍数")
    _assert_rejects(HEADER + "[Class] C @L1-5\n\turn: lk://a\n", "タブ")


def test_rejects_header_problems():
    _assert_rejects("# t ok conf=exact\n", "lsp=")
    _assert_rejects("# t ok conf=wild lsp=ready rev=HEAD ws=a ~1/2tok\n", "水準でない")
    _assert_rejects("t ok conf=exact lsp=ready rev=HEAD ws=a ~1/2tok\n", "ヘッダ")
    _assert_rejects(HEADER + HEADER, "ヘッダ")


def test_rejects_bad_vocabulary_in_lines():
    _assert_rejects(HEADER + "a.py:1:2  [calls] x\n", "use が語彙外")
    _assert_rejects(HEADER + "X a.py:1:2 [ruff:F401] msg\n", "")
    _assert_rejects(HEADER + "<- n @a.py:1  conf=sure\n", "水準でない")


def test_rejects_zero_based_positions():
    _assert_rejects(HEADER + "a.py:0:1  x\n", "1 始まり")
    _assert_rejects(HEADER + "E a.py:3:0 [ruff:F401] msg\n", "1 始まり")
    _assert_rejects(HEADER + "[Class] C @L0-5\n", "1 始まり")


def test_rejects_overlong_snippet_and_doc():
    _assert_rejects(HEADER + "a.py:1:2  " + "x" * 200 + "\n", "200 文字")
    _assert_rejects(HEADER + "[Class] C @L1\n  doc: " + "x" * 200 + "\n", "200 文字")


def test_rejects_table_column_mismatch():
    _assert_rejects(HEADER + "| a | b |\n| 1 |\n", "列数")


def test_rejects_unclosed_fence_and_blank_line():
    _assert_rejects(HEADER + "```python\n@L1: x\n", "閉じていない")
    _assert_rejects(HEADER + "- a\n\n- b\n", "空行")


def test_rejects_trailer_misuse():
    _assert_rejects(HEADER + "# more: cursor=abc\n- late\n", "トレーラ")
    _assert_rejects(HEADER + "# more: cursor=abc\n# summary: x\n", "より前")
    _assert_rejects(HEADER + "# note: hello\n", "ヘッダ")


def test_rejects_missing_final_newline_and_empty():
    _assert_rejects(HEADER.rstrip("\n"), "改行")
    assert g.validate("") != []


def test_body_only_mode_does_not_require_header():
    assert g.validate("[Class] C @L1-5\n  urn: lk://a", require_header=False) == []
