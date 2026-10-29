"""T-10 ``edit_symbol_body`` の ``delete_symbol`` の契約テスト（仕様書 T-10 処理規約 2・3、§10.8、事後条件 1〜5、§14.5）。

パイプライン経由でツールを呼ぶ。``tests/fixtures/poly`` を ``tmp_path`` に写してワークスペースにする（書き込むので原本は触らない）。
置換と同じ入口・出口（引数の検査、応答の形）は ``test_T10_edit_body.py`` が確かめているので、ここは **削除の範囲の規則**
（デコレータ・Doc コメント・前後の空行・ファイル末尾）、**空の本体の拒否**（Python では ``pass`` を補わない）、
**削除後の応答**（``target_urn`` は削除前、``new_range`` は null、T-05 で見つからない）に絞る。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest
from test_T10_edit_body import (
    EditServices,
    apply_unified_diff,
    data_of,
    edit,
    error_node_count,
    error_of,
    sha,
    snapshot,
)

from lucianke.app import build_services
from lucianke.edit.lock import WorkspaceLocks
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import register_tools

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAY = "src/shop/services/payment.py"
CRLF = "src/shop/models/crlf_bom.py"
TS = "web/src/payment.ts"
COBOL = "legacy/calc.cob"
BOM = b"\xef\xbb\xbf"


# --- 土台 ---------------------------------------------------------------------------------


@pytest.fixture
async def index_writer(tmp_path: Path) -> AsyncIterator[IndexWriter]:
    w = IndexWriter(tmp_path / "cache" / "paad.sqlite")
    try:
        yield w
    finally:
        await asyncio.to_thread(w.close)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    dst = tmp_path / "poly"
    shutil.copytree(POLY, dst)
    return Path(os.path.realpath(dst))


@pytest.fixture
def pipeline(root: Path, tmp_path: Path, index_writer: IndexWriter) -> Pipeline:
    base = build_services(lambda: [str(root)])
    services = EditServices(
        roots=base.roots,
        trees=base.trees,
        lines=base.lines,
        locks=WorkspaceLocks(),
        writer_index=index_writer,
        journal_dir=tmp_path / "cache" / "journal",
    )
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=services)


def delete(pipeline: Pipeline, path: str, name: str | None = None, *, line: int | None = None, **kw: Any):
    target: dict[str, Any] = {"path": path}
    if name is not None:
        target["symbol_name"] = name
    if line is not None:
        target["line"] = line
    return edit(pipeline, target=target, operation="delete_symbol", **kw)


def apply_diff(before: bytes, diff: str) -> bytes:
    """``apply_unified_diff`` に、BOM（diff は BOM を除いた本文で作られる）と末尾改行なしの印を扱う処理を足したもの。"""
    bom = BOM if before.startswith(BOM) else b""
    rows: list[str] = []
    for row in diff.splitlines(keepends=True):
        if row.startswith("\\"):  # "\ No newline at end of file": 直前の追加行の改行を落とす
            if rows and rows[-1][:1] == "+":
                rows[-1] = rows[-1].rstrip("\r\n")
            continue
        rows.append(row)
    return bom + apply_unified_diff(before.removeprefix(BOM), "".join(rows))


def put(root: Path, rel: str, text: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


async def removed(pipeline: Pipeline, root: Path, rel: str, name: str | None = None, **kw: Any) -> bytes:
    """削除して、事後条件 1（sha256_after・diff を適用した結果とのバイト一致）まで確かめ、削除後のバイト列を返す。"""
    before = (root / rel).read_bytes()
    data = data_of(await delete(pipeline, rel, name, **kw))
    after = (root / rel).read_bytes()
    assert data["applied"] is True and data["operation"] == "delete_symbol" and data["syntax_check"] == "ok"
    (change,) = data["files_changed"]
    assert change["path"] == rel and change["sha256_before"] == sha(before)
    assert change["sha256_after"] == sha(after) and change["hunks"] == 1
    assert apply_diff(before, data["diff"]) == after
    assert data["new_range"] is None  # 仕様書: delete_symbol では null
    return after


# --- Python: 削除の範囲 -----------------------------------------------------------------------------------


async def test_python_method_in_the_middle_keeps_one_blank_line_between_its_neighbours(
    pipeline: Pipeline, root: Path
) -> None:
    before = (root / PAY).read_text("utf-8")
    after = (await removed(pipeline, root, PAY, "PaymentProcessor._validate_limit")).decode("utf-8")
    assert "_validate_limit" not in after.replace("self._validate_limit(req.amount)", "")
    assert "# 通貨ごとの上限を確認する" not in after  # 本体のコメントは定義と一緒に消える
    block = (
        "    def _validate_limit(self, amount: Decimal) -> bool:\n"
        "        # 通貨ごとの上限を確認する\n"
        "        limit = self._limits.get(self.currency)\n"
        "        if limit is None:\n"
        "            raise LimitExceeded(self.currency)\n"
        "        return amount <= limit\n\n"
    )
    assert before.replace(block, "") == after  # 定義と直後の空行 1 行だけが消え、ほかは 1 バイトも変わらない
    assert "return Receipt(order_id=req.order_id, amount=req.amount)\n\n    def apply_discount" in after
    assert error_node_count(after.encode()) == 0


async def test_python_decorated_method_goes_with_its_decorator_and_docstring(
    pipeline: Pipeline, root: Path
) -> None:
    after = (await removed(pipeline, root, PAY, "PaymentProcessor.process_charge")).decode("utf-8")
    assert "@transactional\n    async def" not in after and "process_charge" not in after
    assert "決済を実行し領収書を発行する" not in after and "Raises:" not in after
    assert "def transactional(fn):" in after  # 同名の別のシンボルは残る
    # 直前の空行を取り除くので、__init__ と次のメソッドの間の空行は 1 行のまま
    assert 'self.currency = "JPY"\n\n    def _validate_limit' in after


async def test_python_last_method_at_end_of_file_leaves_no_trailing_blank_line(
    pipeline: Pipeline, root: Path
) -> None:
    before = (root / PAY).read_bytes()
    assert before.endswith(b"\n")
    after = await removed(pipeline, root, PAY, "PaymentProcessor.apply_discount")
    assert after.endswith(b"return amount <= limit\n") and not after.endswith(b"\n\n")
    assert error_node_count(after) == 0


async def test_python_toplevel_class_keeps_the_two_blank_lines_between_its_neighbours(
    pipeline: Pipeline, root: Path
) -> None:
    after = (await removed(pipeline, root, PAY, "PaymentDeclined")).decode("utf-8")
    assert "class PaymentDeclined" not in after and "raise PaymentDeclined(req.order_id)" in after
    assert 'currency: str = "JPY"\n\n\nclass PaymentProcessor:' in after  # 前後 2 行ずつ → 2 行
    assert error_node_count(after.encode()) == 0


async def test_python_toplevel_decorated_function_and_first_statement_of_the_file(
    pipeline: Pipeline, root: Path
) -> None:
    put(
        root,
        "src/shop/solo.py",
        '"""doc."""\n\n\ndef first():\n    return 1\n\n\ndef second():\n    return 2\n',
    )
    after = (await removed(pipeline, root, "src/shop/solo.py", "first")).decode("utf-8")
    assert after == '"""doc."""\n\n\ndef second():\n    return 2\n'
    put(root, "src/shop/head.py", "def first():\n    return 1\n\n\ndef second():\n    return 2\n")
    after = (await removed(pipeline, root, "src/shop/head.py", "first")).decode("utf-8")
    assert after == "def second():\n    return 2\n"  # 先頭: 直前に空行が無いので直後の空行を取り除く


async def test_python_doc_comment_and_adjacent_comment_go_but_a_separated_comment_stays(
    pipeline: Pipeline, root: Path
) -> None:
    put(
        root,
        "src/shop/notes.py",
        "x = 1\n\n# standalone note\n\n# about f\ndef f():\n    return 1\n\n\ndef g():\n    return 2\n",
    )
    after = (await removed(pipeline, root, "src/shop/notes.py", "f")).decode("utf-8")
    assert "# about f" not in after  # 空行のない直前の通常コメントは定義の一部
    assert "# standalone note" in after  # 空行で隔てられたコメントは残る
    # 直前の空行 1 行を一緒に取り除き、直後の 2 行は残す
    assert after == "x = 1\n\n# standalone note\n\n\ndef g():\n    return 2\n"


async def test_python_trailing_same_line_comment_goes_with_the_definition(
    pipeline: Pipeline, root: Path
) -> None:
    put(root, "src/shop/trail.py", "def f(): return 1  # inline\n\n\ndef g():\n    return 2\n")
    after = (await removed(pipeline, root, "src/shop/trail.py", "f")).decode("utf-8")
    assert after == "def g():\n    return 2\n"


async def test_python_file_without_final_newline_keeps_having_none(pipeline: Pipeline, root: Path) -> None:
    put(root, "src/shop/nonl.py", "def a():\n    return 1\n\n\ndef b():\n    return 2")
    after = await removed(pipeline, root, "src/shop/nonl.py", "b")
    assert after == b"def a():\n    return 1"  # 最終改行を勝手に足さない
    put(root, "src/shop/nonl2.py", "def a():\n    return 1\n\n\ndef b():\n    return 2")
    after = await removed(pipeline, root, "src/shop/nonl2.py", "a")
    assert after == b"def b():\n    return 2"


async def test_deleting_the_only_symbol_of_a_file_leaves_an_empty_file(
    pipeline: Pipeline, root: Path
) -> None:
    after = await removed(pipeline, root, "src/shop/models/no_final_newline.py", "no_newline_at_end")
    assert after == b""


# --- Python: 本体が空になる削除は拒否する（pass を補わない） -------------------------------------------------------


async def test_deleting_the_only_member_of_a_python_class_is_refused_with_a_hint(
    pipeline: Pipeline, root: Path
) -> None:
    before = snapshot(root)
    err = error_of(await delete(pipeline, CRLF, "CrlfClass.method"))  # CRLF + BOM のファイルでも同じ
    assert err["code"] == "E_SYNTAX_INVALID"
    assert "pass" in err["details"]["hint"] and err["details"]["reason"] == "parent_body_would_be_empty"
    assert snapshot(root) == before  # 事後条件 4


async def test_a_python_body_that_keeps_a_docstring_may_lose_its_only_method(
    pipeline: Pipeline, root: Path
) -> None:
    put(root, "src/shop/doc.py", 'class A:\n    """About A."""\n\n    def only(self):\n        return 1\n')
    after = (await removed(pipeline, root, "src/shop/doc.py", "A.only")).decode("utf-8")
    assert after == 'class A:\n    """About A."""\n'
    assert error_node_count(after.encode()) == 0


async def test_a_nested_only_function_in_a_python_function_is_refused(pipeline: Pipeline, root: Path) -> None:
    put(
        root,
        "src/shop/nest.py",
        "def outer():\n    def inner():\n        return 1\n\n\ndef other():\n    return 2\n",
    )
    before = snapshot(root)
    err = error_of(await delete(pipeline, "src/shop/nest.py", "inner"))
    assert err["code"] == "E_SYNTAX_INVALID" and "hint" in err["details"]
    assert snapshot(root) == before


async def test_a_python_method_that_is_not_alone_in_its_class_is_deleted(
    pipeline: Pipeline, root: Path
) -> None:
    put(
        root,
        "src/shop/two.py",
        "class A:\n    # keep me\n    x = 1\n\n    def only(self):\n        return 1\n",
    )
    after = (await removed(pipeline, root, "src/shop/two.py", "A.only")).decode("utf-8")
    assert after == "class A:\n    # keep me\n    x = 1\n"


# --- 事後条件 5: 改行・BOM -------------------------------------------------------------------------------------


async def test_postcondition_5_crlf_and_bom_survive_a_delete(pipeline: Pipeline, root: Path) -> None:
    before = (root / CRLF).read_bytes()
    assert before.startswith(BOM) and b"\r\n" in before
    after = await removed(pipeline, root, CRLF, "crlf_function")
    assert after.startswith(BOM) and not after.startswith(BOM + BOM)
    assert b"\n" not in after.replace(b"\r\n", b"") and b"\r" not in after.replace(b"\r\n", b"")
    assert b"crlf_function" not in after and b"class CrlfClass" in after
    assert error_node_count(after) == 0
    # 定義（3 行）と直前の空行 2 行が消え、CRLF は残りの行の数だけ減る
    assert before.count(b"\r\n") - after.count(b"\r\n") == 5


async def test_a_delete_at_the_start_of_a_bom_file_keeps_the_bom(pipeline: Pipeline, root: Path) -> None:
    put(
        root,
        "src/shop/bom_head.py",
        BOM + b"def a():\r\n    return 1\r\n\r\n\r\ndef b():\r\n    return 2\r\n",
    )
    after = await removed(pipeline, root, "src/shop/bom_head.py", "a")
    assert after == BOM + b"def b():\r\n    return 2\r\n"


# --- TypeScript ----------------------------------------------------------------------------------------------


async def test_typescript_first_member_takes_the_blank_line_after_it(pipeline: Pipeline, root: Path) -> None:
    after = (await removed(pipeline, root, TS, "PaymentClient.constructor")).decode("utf-8")
    assert "constructor" not in after
    assert "export class PaymentClient {\n  charge(req: PaymentRequest): Promise<string>;\n" in after
    assert error_node_count(after.encode(), "typescript") == 0


async def test_typescript_last_member_goes_with_its_doc_comment(pipeline: Pipeline, root: Path) -> None:
    after = (await removed(pipeline, root, TS, line=15)).decode("utf-8")  # throw の行 → charge の実装
    assert "決済を要求する" not in after and "async charge" not in after
    assert "charge(req: PaymentRequest, retries: number): Promise<string>;\n}\n" in after
    assert error_node_count(after.encode(), "typescript") == 0


async def test_typescript_exported_declaration_goes_whole_with_its_export(
    pipeline: Pipeline, root: Path
) -> None:
    after = (await removed(pipeline, root, TS, "PaymentRequest")).decode("utf-8")
    assert after.startswith("export class PaymentClient {")  # 先頭: 直後の空行も取り除く
    assert "interface" not in after and error_node_count(after.encode(), "typescript") == 0


async def test_typescript_last_declaration_of_the_file_takes_the_blank_line_before_it(
    pipeline: Pipeline, root: Path
) -> None:
    after = (await removed(pipeline, root, TS, "PaymentClient")).decode("utf-8")
    assert after == (
        "export interface PaymentRequest {\n"
        "  orderId: string;\n"
        "  amount: number;\n"
        '  currency: "JPY" | "USD";\n'
        "}\n"
    )


async def test_typescript_only_member_may_go_because_an_empty_body_is_valid(
    pipeline: Pipeline, root: Path
) -> None:
    put(root, "web/src/solo.ts", "export class A {\n  foo(): number {\n    return 1;\n  }\n}\n")
    after = (await removed(pipeline, root, "web/src/solo.ts", "A.foo")).decode("utf-8")
    assert after == "export class A {\n}\n"
    assert error_node_count(after.encode(), "typescript") == 0


async def test_typescript_function_between_two_others_keeps_one_blank_line(
    pipeline: Pipeline, root: Path
) -> None:
    put(
        root,
        "web/src/three.ts",
        "export function a(): number {\n  return 1;\n}\n\n"
        "/** b. */\nexport function b(): number {\n  return 2;\n}\n\n"
        "export const c = (): number => 3;\n",
    )
    after = (await removed(pipeline, root, "web/src/three.ts", "b")).decode("utf-8")
    assert after == "export function a(): number {\n  return 1;\n}\n\nexport const c = (): number => 3;\n"


async def test_typescript_overload_deleted_reports_the_urn_before_and_no_range(
    pipeline: Pipeline, root: Path
) -> None:
    """同名の定義が残っても、``target_urn`` は削除前のもの、``new_range`` は null（再索引後の同名への取り違えを防ぐ）。"""
    data = data_of(await delete(pipeline, TS, line=11))  # 1 つ目のオーバーロード署名
    assert data["applied"] is True and data["new_range"] is None
    assert data["target_urn"].startswith("lk://poly/web/src/payment.ts#PaymentClient.charge")
    after = (root / TS).read_text("utf-8")
    assert after.count("charge(req: PaymentRequest") == 2


# --- 事後条件 3・4 と前提・出力 ----------------------------------------------------------------------------------


async def test_postcondition_3_the_deleted_symbol_is_not_found_afterwards(pipeline: Pipeline) -> None:
    data = data_of(await delete(pipeline, PAY, "PaymentProcessor._validate_limit"))
    urn = data["target_urn"]
    assert urn.endswith("PaymentProcessor._validate_limit") and data["new_range"] is None  # 削除前の URN
    got = await pipeline.call_tool(
        "get_symbol_bodies", {"format": "json", "targets": [{"urn": urn}], "context_lines": 0}
    )
    assert error_of(got)["code"] == "E_TARGET_NOT_FOUND"  # 事後条件 3: 削除前の URN はもう解決しない
    again = error_of(await delete(pipeline, PAY, "PaymentProcessor._validate_limit"))
    assert again["code"] == "E_TARGET_NOT_FOUND"


async def test_postcondition_4_dry_run_changes_nothing_and_predicts_the_write(
    pipeline: Pipeline, root: Path
) -> None:
    before = snapshot(root)
    dry = data_of(await delete(pipeline, PAY, "PaymentProcessor.apply_discount", dry_run=True))
    assert dry["applied"] is False and dry["edit_id"] is None and dry["new_range"] is None
    assert dry["diff"] and dry["syntax_check"] == "ok" and dry["new_diagnostics"]["status"] == "not_computed"
    assert snapshot(root) == before
    real = data_of(await delete(pipeline, PAY, "PaymentProcessor.apply_discount"))
    assert real["files_changed"][0]["sha256_after"] == dry["files_changed"][0]["sha256_after"]
    assert real["diff"] == dry["diff"] and real["applied"] is True


async def test_postcondition_4_wrong_expected_sha_is_refused_and_the_right_one_chains(
    pipeline: Pipeline, root: Path
) -> None:
    before = snapshot(root)
    err = error_of(
        await delete(pipeline, PAY, "PaymentProcessor.apply_discount", expected_file_sha256="0" * 64)
    )
    assert err["code"] == "E_PRECONDITION_FAILED" and err["details"]["actual"] == before[PAY]
    assert snapshot(root) == before
    first = data_of(
        await delete(pipeline, PAY, "PaymentProcessor.apply_discount", expected_file_sha256=before[PAY][:12])
    )
    assert first["applied"] is True
    stale = await delete(pipeline, PAY, "PaymentProcessor._validate_limit", expected_file_sha256=before[PAY])
    assert error_of(stale)["code"] == "E_PRECONDITION_FAILED"
    second = data_of(
        await delete(
            pipeline,
            PAY,
            "PaymentProcessor._validate_limit",
            expected_file_sha256=first["files_changed"][0]["sha256_after"],
        )
    )
    assert second["applied"] is True and second["edit_id"] == "ed_0002"
    assert error_node_count((root / PAY).read_bytes()) == 0


@pytest.mark.parametrize(
    "path, name, code",
    [
        (PAY, "PaymentProcessor.nope", "E_TARGET_NOT_FOUND"),
        ("src/shop/missing.py", "x", "E_TARGET_NOT_FOUND"),
        (COBOL, "CALC", "E_UNSUPPORTED"),  # 文法の無いファイル（Tier 4）
    ],
)
async def test_errors_leave_every_byte_alone(
    pipeline: Pipeline, root: Path, path: str, name: str, code: str
) -> None:
    before = snapshot(root)
    err = error_of(await delete(pipeline, path, name))
    assert err["code"] == code, err
    assert snapshot(root) == before


async def test_compact_output_follows_the_grammar(pipeline: Pipeline) -> None:
    r = await edit(
        pipeline,
        "compact",
        target={"path": PAY, "symbol_name": "PaymentProcessor._validate_limit"},
        operation="delete_symbol",
    )
    assert not r.is_error and g.validate(r.text) == [], r.text
    assert "applied: true  edit_id=ed_0001  op=delete_symbol" in r.text
    assert "syntax_check: ok" in r.text and "```diff" in r.text
    target_line = next(ln for ln in r.text.splitlines() if ln.startswith("target: "))
    assert target_line.endswith("PaymentProcessor._validate_limit")  # 範囲（@L…）は付かない
    dry = await edit(
        pipeline,
        "compact",
        target={"path": PAY, "symbol_name": "PaymentProcessor.apply_discount"},
        operation="delete_symbol",
        dry_run=True,
    )
    assert g.validate(dry.text) == [], dry.text


# --- §14.5: 構文妥当性（ファイルの全シンボルを dry_run で削除し、適用後を解析する） ------------------------------------------


@pytest.mark.parametrize(
    "path, language_id",
    [(PAY, "python"), (CRLF, "python"), (TS, "typescript"), ("web/src/app.ts", "typescript")],
)
async def test_every_symbol_of_a_file_deletes_into_valid_syntax_or_is_refused(
    pipeline: Pipeline, root: Path, path: str, language_id: str
) -> None:
    before = (root / path).read_bytes()
    skeleton = data_of(
        await pipeline.call_tool(
            "get_file_skeleton", {"format": "json", "paths": [path], "include_urns": "all"}
        )
    )
    urns = sorted({s["urn"] for s in _symbols(skeleton["items"][0])})
    assert len(urns) >= 2, skeleton
    applied = refused = 0
    for urn in urns:
        r = await edit(pipeline, target={"urn": urn}, operation="delete_symbol", dry_run=True)
        if r.is_error:
            err = error_of(r)
            assert err["code"] in {"E_SYNTAX_INVALID", "E_TARGET_AMBIGUOUS"}, (urn, err)
            refused += err["code"] == "E_SYNTAX_INVALID"
            continue
        data = data_of(r)
        after = apply_unified_diff(before, data["diff"])
        assert after != before and sha(after) == data["files_changed"][0]["sha256_after"], urn
        assert error_node_count(after, language_id) <= error_node_count(before, language_id), urn
        applied += 1
    assert applied >= 1 and (root / path).read_bytes() == before


def _symbols(skeleton: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("urn"), str) and node.get("kind"):
                out.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(skeleton)
    return out
