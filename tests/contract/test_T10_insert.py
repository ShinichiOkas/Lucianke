"""T-10 ``edit_symbol_body`` の挿入 3 操作（``insert_before`` / ``insert_after`` / ``insert_member``）の契約テスト
（仕様書 T-10 処理規約 2〜5、事後条件 1〜5、§10.8 の挿入位置と空行、§10.9 の正規化、EP-1・EP-2・EP-3・EP-6）。

``test_T10_edit_body.py`` と同じ土台（``tests/fixtures/poly`` を ``tmp_path`` に写し、パイプライン経由でツールを呼ぶ。LSP・監視・索引なし）。
挿入の結果は **ファイルの全文** で確かめる（空行の数・インデント・改行・BOM・末尾改行を 1 つずつ目で追えるようにするため）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import compact_grammar as g
import pytest

from lucianke.app import build_services
from lucianke.edit.lock import WorkspaceLocks
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.syntax.parsers import parse_source
from lucianke.tools import register_tools
from lucianke.tools._services import ToolServices

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAY = "src/shop/services/payment.py"
CRLF = "src/shop/models/crlf_bom.py"
NO_NL = "src/shop/models/no_final_newline.py"
TS = "web/src/payment.ts"
EMPTY_PY = "src/shop/empty_bodies.py"
EMPTY_TS = "web/src/empty_bodies.ts"
BOM = b"\xef\xbb\xbf"

PROCESSOR = {"path": PAY, "symbol_name": "PaymentProcessor"}
VALIDATE = {"path": PAY, "symbol_name": "PaymentProcessor._validate_limit"}
DISCOUNT = {"path": PAY, "symbol_name": "PaymentProcessor.apply_discount"}
INIT = {"path": PAY, "symbol_name": "PaymentProcessor.__init__"}
TRANSACTIONAL = {"path": PAY, "symbol_name": "transactional"}
REQUEST = {"path": PAY, "symbol_name": "PaymentRequest"}  # @dataclass つき
CLIENT = {"path": TS, "symbol_name": "PaymentClient"}
CTOR = {"path": TS, "symbol_name": "PaymentClient.constructor"}

PY_METHOD = 'def refund(self, order_id: str) -> bool:\n    """返金する。"""\n    return bool(order_id)\n'
PY_FUNC = "def helper(x: int) -> int:\n    return x + 1\n"
TS_METHOD = 'refund(orderId: string): boolean {\n  return orderId !== "";\n}\n'
TS_CONST = 'export const DEFAULT_CURRENCY = "JPY";\n'

EMPTY_PY_SOURCE = (
    "class OnlyPass:\n    pass\n\n\n"
    "class WithEllipsis:\n    ...\n\n\n"
    "class Inline: pass\n\n\n"
    'class Documented:\n    """doc"""\n'
)
EMPTY_TS_SOURCE = "export class Closed {}\n\nexport class Open {\n}\n\nexport class Spaced { }\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- 土台 ---------------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class EditServices(ToolServices):
    """``ToolServices`` に編集の依存（``locks`` / ``writer_index`` / ``journal_dir``）を足したもの。合成ルートの結線の代わり。"""

    locks: Any = None
    writer_index: Any = None
    journal_dir: Any = None


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
    (dst / EMPTY_PY).write_bytes(EMPTY_PY_SOURCE.encode())
    (dst / EMPTY_TS).write_bytes(EMPTY_TS_SOURCE.encode())
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


async def edit(pipeline: Pipeline, fmt: str = "json", **args: Any):
    args.setdefault("diagnostics_wait_ms", 0)
    return await pipeline.call_tool("edit_symbol_body", {"format": fmt, **args})


def data_of(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.text
    return result.structured["data"]


def error_of(result: Any) -> dict[str, Any]:
    assert result.is_error, result.text
    return result.structured["error"]


def snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()
    }


def apply_unified_diff(before: bytes, diff: str) -> bytes:
    """最小の unified diff 適用器（事後条件 1 の「diff を適用した結果とバイト単位で一致」を確かめるため）。"""
    src = before.decode("utf-8").splitlines(keepends=True)
    rows = diff.splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    i = 0
    while i < len(rows):
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", rows[i])
        if not m:
            i += 1
            continue
        start = int(m[1]) - (0 if m[2] == "0" else 1)
        out.extend(src[pos:start])
        pos = start
        i += 1
        while i < len(rows) and not rows[i].startswith("@@"):
            tag, text = rows[i][:1], rows[i][1:]
            if tag == " ":
                out.append(src[pos])
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(text)
            i += 1
    out.extend(src[pos:])
    return "".join(out).encode("utf-8")


def error_node_count(data: bytes, language_id: str = "python") -> int:
    parsed, _ = parse_source(data, language_id, sha(data))
    return len(parsed.error_ranges)


def text_of(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8")


async def insert(pipeline: Pipeline, target: dict[str, Any], operation: str, new_code: str, **extra: Any):
    return data_of(await edit(pipeline, target=target, operation=operation, new_code=new_code, **extra))


async def symbol_at(pipeline: Pipeline, urn: str) -> dict[str, Any]:
    got = await pipeline.call_tool(
        "get_symbol_bodies", {"format": "json", "targets": [{"urn": urn}], "context_lines": 0}
    )
    (item,) = data_of(got)["items"]
    return item


# --- insert_after / insert_before: Python -----------------------------------------------------


async def test_python_insert_after_a_method_puts_one_blank_line_on_each_side(
    pipeline: Pipeline, root: Path
) -> None:
    before = text_of(root, PAY)
    data = await insert(pipeline, VALIDATE, "insert_after", PY_METHOD)
    after = text_of(root, PAY)
    assert data["applied"] is True and data["operation"] == "insert_after" and data["syntax_check"] == "ok"
    assert after == before.replace(
        "        return amount <= limit\n\n    def apply_discount",
        "        return amount <= limit\n\n"
        '    def refund(self, order_id: str) -> bool:\n        """返金する。"""\n        return bool(order_id)\n\n'
        "    def apply_discount",
    )
    assert data["indent_unit"] == "4sp" and data["files_changed"][0]["hunks"] == 1


async def test_python_insert_before_a_method(pipeline: Pipeline, root: Path) -> None:
    before = text_of(root, PAY)
    await insert(pipeline, DISCOUNT, "insert_before", PY_METHOD)
    after = text_of(root, PAY)
    assert after == before.replace(
        "    def apply_discount",
        '    def refund(self, order_id: str) -> bool:\n        """返金する。"""\n        return bool(order_id)\n\n'
        "    def apply_discount",
    )


async def test_python_insert_before_the_first_method_of_a_class_adds_no_blank_line_above(
    pipeline: Pipeline, root: Path
) -> None:
    """``__init__`` の前には docstring + 空行 1 行が既にある。足りている分は足さない。"""
    before = text_of(root, PAY)
    await insert(pipeline, INIT, "insert_before", PY_METHOD)
    after = text_of(root, PAY)
    assert '    """注文の決済を担う。"""\n\n    def refund(' in after
    assert "return bool(order_id)\n\n    def __init__" in after
    assert after.count("\n") - before.count("\n") == 4  # 断片 3 行 + 空行 1 行


async def test_python_toplevel_function_gets_two_blank_lines_after(pipeline: Pipeline, root: Path) -> None:
    before = text_of(root, PAY)
    await insert(pipeline, TRANSACTIONAL, "insert_after", PY_FUNC)
    after = text_of(root, PAY)
    assert after == before.replace(
        "    return fn\n\n\n@dataclass",
        "    return fn\n\n\ndef helper(x: int) -> int:\n    return x + 1\n\n\n@dataclass",
    )


async def test_python_insert_before_a_decorated_class_goes_above_the_decorator(
    pipeline: Pipeline, root: Path
) -> None:
    before = text_of(root, PAY)
    await insert(pipeline, REQUEST, "insert_before", PY_FUNC)
    after = text_of(root, PAY)
    assert after == before.replace(
        "    return fn\n\n\n@dataclass",
        "    return fn\n\n\ndef helper(x: int) -> int:\n    return x + 1\n\n\n@dataclass",
    )


async def test_python_insert_after_the_last_toplevel_class_keeps_the_end_of_file(
    pipeline: Pipeline, root: Path
) -> None:
    before = text_of(root, PAY)
    assert before.endswith("Decimal(factor)\n")
    await insert(pipeline, PROCESSOR, "insert_after", PY_FUNC)
    assert text_of(root, PAY) == before + "\n\ndef helper(x: int) -> int:\n    return x + 1\n"


async def test_python_new_code_indentation_is_converted_to_the_file_unit(
    pipeline: Pipeline, root: Path
) -> None:
    """2 スペースの断片は、4 スペースのファイルの単位に付け替わる。共通の先頭空白は除かれる。"""
    code = "        def tidy(self):\n          if self:\n            return 1\n          return 2\n"
    await insert(pipeline, VALIDATE, "insert_after", code)
    assert (
        "    def tidy(self):\n        if self:\n            return 1\n        return 2\n\n    def apply_discount"
        in text_of(root, PAY)
    )


# --- insert_member: Python ---------------------------------------------------------------------


async def test_python_insert_member_end_appends_after_the_last_member(pipeline: Pipeline, root: Path) -> None:
    before = text_of(root, PAY)
    data = await insert(pipeline, PROCESSOR, "insert_member", PY_METHOD)
    after = text_of(root, PAY)
    assert after == before + (
        '\n    def refund(self, order_id: str) -> bool:\n        """返金する。"""\n        return bool(order_id)\n'
    )
    assert data["target_urn"].endswith("PaymentProcessor.refund")


async def test_python_insert_member_start_goes_after_the_docstring(pipeline: Pipeline, root: Path) -> None:
    before = text_of(root, PAY)
    await insert(pipeline, PROCESSOR, "insert_member", PY_METHOD, position="start")
    after = text_of(root, PAY)
    assert after == before.replace(
        '    """注文の決済を担う。"""\n\n    def __init__',
        '    """注文の決済を担う。"""\n\n'
        '    def refund(self, order_id: str) -> bool:\n        """返金する。"""\n        return bool(order_id)\n\n'
        "    def __init__",
    )


async def test_python_insert_member_into_a_pass_only_class_replaces_the_pass(
    pipeline: Pipeline, root: Path
) -> None:
    before = text_of(root, PAY)
    await insert(
        pipeline,
        {"path": PAY, "symbol_name": "PaymentDeclined"},
        "insert_member",
        "def reason(self) -> str:\n    return 'declined'\n",
    )
    after = text_of(root, PAY)
    assert after == before.replace(
        "class PaymentDeclined(Exception):\n    pass\n",
        "class PaymentDeclined(Exception):\n    def reason(self) -> str:\n        return 'declined'\n",
    )


@pytest.mark.parametrize("position", ["start", "end"])
async def test_python_insert_member_into_empty_bodies(pipeline: Pipeline, root: Path, position: str) -> None:
    code = "def hello(self):\n    return 1\n"
    expectations = {
        "OnlyPass": "class OnlyPass:\n    def hello(self):\n        return 1\n\n\nclass WithEllipsis",
        "WithEllipsis": "class WithEllipsis:\n    def hello(self):\n        return 1\n\n\nclass Inline",
        "Inline": "class Inline:\n    def hello(self):\n        return 1\n\n\nclass Documented",
        "Documented": 'class Documented:\n    """doc"""\n\n    def hello(self):\n        return 1\n',
    }
    for name, expected in expectations.items():
        data = await insert(
            pipeline, {"path": EMPTY_PY, "symbol_name": name}, "insert_member", code, position=position
        )
        assert data["applied"] is True and data["syntax_check"] == "ok", name
        assert expected in text_of(root, EMPTY_PY), name
        assert error_node_count((root / EMPTY_PY).read_bytes()) == 0, name


# --- TypeScript -----------------------------------------------------------------------------------


async def test_typescript_insert_member_end(pipeline: Pipeline, root: Path) -> None:
    before = text_of(root, TS)
    data = await insert(pipeline, CLIENT, "insert_member", TS_METHOD)
    after = text_of(root, TS)
    assert data["indent_unit"] == "2sp" and data["syntax_check"] == "ok"
    assert after == before.replace(
        "    return `${this.baseUrl}/charge/${req.orderId}?r=${retries}`;\n  }\n}\n",
        "    return `${this.baseUrl}/charge/${req.orderId}?r=${retries}`;\n  }\n\n"
        '  refund(orderId: string): boolean {\n    return orderId !== "";\n  }\n}\n',
    )
    assert error_node_count((root / TS).read_bytes(), "typescript") == 0


async def test_typescript_insert_member_start_goes_before_the_first_member(
    pipeline: Pipeline, root: Path
) -> None:
    before = text_of(root, TS)
    await insert(pipeline, CLIENT, "insert_member", TS_METHOD, position="start")
    assert text_of(root, TS) == before.replace(
        "export class PaymentClient {\n  constructor",
        'export class PaymentClient {\n  refund(orderId: string): boolean {\n    return orderId !== "";\n  }\n\n'
        "  constructor",
    )


async def test_typescript_insert_after_and_before_a_member(pipeline: Pipeline, root: Path) -> None:
    before = text_of(root, TS)
    await insert(pipeline, CTOR, "insert_after", "static VERSION = 1;\n")
    after = text_of(root, TS)
    assert after == before.replace(
        "  constructor(private readonly baseUrl: string) {}\n\n",
        "  constructor(private readonly baseUrl: string) {}\n\n  static VERSION = 1;\n\n",
        1,
    )
    await insert(pipeline, CTOR, "insert_before", "static NAME = 'c';\n")
    assert "export class PaymentClient {\n  static NAME = 'c';\n\n  constructor" in text_of(root, TS)


async def test_typescript_insert_after_a_toplevel_interface_uses_one_blank_line(
    pipeline: Pipeline, root: Path
) -> None:
    before = text_of(root, TS)
    await insert(pipeline, {"path": TS, "symbol_name": "PaymentRequest"}, "insert_after", TS_CONST)
    assert text_of(root, TS) == before.replace(
        '"USD";\n}\n\nexport class', '"USD";\n}\n\nexport const DEFAULT_CURRENCY = "JPY";\n\nexport class'
    )


async def test_typescript_insert_before_the_first_definition_in_the_file(
    pipeline: Pipeline, root: Path
) -> None:
    before = text_of(root, TS)
    await insert(pipeline, {"path": TS, "symbol_name": "PaymentRequest"}, "insert_before", TS_CONST)
    assert text_of(root, TS) == TS_CONST + "\n" + before


async def test_typescript_insert_member_into_empty_bodies(pipeline: Pipeline, root: Path) -> None:
    code = "ping(): string {\n  return 'pong';\n}\n"
    body = "{\n  ping(): string {\n    return 'pong';\n  }\n}\n"
    for name in ("Closed", "Open", "Spaced"):
        data = await insert(pipeline, {"path": EMPTY_TS, "symbol_name": name}, "insert_member", code)
        assert data["applied"] is True and data["syntax_check"] == "ok", name
        assert f"export class {name} {body}" in text_of(root, EMPTY_TS), name
    assert error_node_count((root / EMPTY_TS).read_bytes(), "typescript") == 0


# --- 事後条件 1・EP-1: sha・diff ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, operation, code, path, language",
    [
        (VALIDATE, "insert_after", PY_METHOD, PAY, "python"),
        (DISCOUNT, "insert_before", PY_METHOD, PAY, "python"),
        (PROCESSOR, "insert_member", PY_METHOD, PAY, "python"),
        (CTOR, "insert_after", TS_METHOD, TS, "typescript"),
        (CTOR, "insert_before", TS_METHOD, TS, "typescript"),
        (CLIENT, "insert_member", TS_METHOD, TS, "typescript"),
    ],
)
async def test_postconditions_1_2_sha_after_diff_and_error_nodes(
    pipeline: Pipeline,
    root: Path,
    target: dict[str, Any],
    operation: str,
    code: str,
    path: str,
    language: str,
) -> None:
    before = (root / path).read_bytes()
    data = await insert(pipeline, target, operation, code)
    after = (root / path).read_bytes()
    (change,) = data["files_changed"]
    assert data["applied"] is True and data["edit_id"] == "ed_0001"
    assert change["path"] == path and change["sha256_before"] == sha(before)
    assert change["sha256_after"] == sha(after)  # EP-1
    assert apply_unified_diff(before, data["diff"]) == after  # 事後条件 1
    assert error_node_count(after, language) <= error_node_count(before, language) == 0  # 事後条件 2
    assert data["syntax_check"] == "ok"


# --- 事後条件 3: target_urn と new_range が挿入したシンボルを指す ----------------------------------------


@pytest.mark.parametrize(
    "target, operation, code, name, position",
    [
        (VALIDATE, "insert_after", PY_METHOD, "PaymentProcessor.refund", "end"),
        (DISCOUNT, "insert_before", PY_METHOD, "PaymentProcessor.refund", "end"),
        (PROCESSOR, "insert_member", PY_METHOD, "PaymentProcessor.refund", "end"),
        (PROCESSOR, "insert_member", PY_METHOD, "PaymentProcessor.refund", "start"),
        (TRANSACTIONAL, "insert_after", PY_FUNC, "helper", "end"),
        (REQUEST, "insert_before", PY_FUNC, "helper", "end"),
        (CTOR, "insert_after", TS_METHOD, "PaymentClient.refund", "end"),
        (CTOR, "insert_before", TS_METHOD, "PaymentClient.refund", "end"),
        (CLIENT, "insert_member", TS_METHOD, "PaymentClient.refund", "start"),
        (CLIENT, "insert_member", TS_METHOD, "PaymentClient.refund", "end"),
    ],
)
async def test_postcondition_3_target_urn_and_new_range_point_at_the_inserted_symbol(
    pipeline: Pipeline, target: dict[str, Any], operation: str, code: str, name: str, position: str
) -> None:
    data = await insert(pipeline, target, operation, code, position=position)
    urn, new_range = data["target_urn"], data["new_range"]
    assert urn.endswith(f"#{name}") and new_range is not None, urn
    item = await symbol_at(pipeline, urn)  # T-05 が 1 件に解決し、範囲が一致する
    assert item["ok"] and item["range_returned"] == new_range
    assert name.rsplit(".", 1)[-1] in item["code"].splitlines()[0]
    assert item["code"].strip().endswith(code.strip().splitlines()[-1].strip())  # 末尾行も挿入した断片
    assert item["file_sha256"] == data["files_changed"][0]["sha256_after"]


# --- 事後条件 4: applied: false のとき何も変わらない ------------------------------------------------------


@pytest.mark.parametrize(
    "target, operation, code",
    [
        (VALIDATE, "insert_after", PY_METHOD),
        (PROCESSOR, "insert_member", PY_METHOD),
        (CTOR, "insert_before", TS_METHOD),
    ],
)
async def test_postcondition_4_dry_run_changes_nothing_and_predicts_the_write(
    pipeline: Pipeline, root: Path, target: dict[str, Any], operation: str, code: str
) -> None:
    before = snapshot(root)
    dry = await insert(pipeline, target, operation, code, dry_run=True)
    assert dry["applied"] is False and dry["edit_id"] is None and dry["new_range"] is None
    assert dry["diff"] and dry["new_diagnostics"]["status"] == "not_computed"
    assert snapshot(root) == before
    real = await insert(pipeline, target, operation, code)
    assert real["files_changed"][0]["sha256_after"] == dry["files_changed"][0]["sha256_after"]
    assert real["diff"] == dry["diff"]


@pytest.mark.parametrize(
    "operation, target",
    [("insert_after", VALIDATE), ("insert_before", VALIDATE), ("insert_member", PROCESSOR)],
)
async def test_expected_file_sha256_is_checked_and_chains_with_sha_after(
    pipeline: Pipeline, root: Path, operation: str, target: dict[str, Any]
) -> None:
    current = sha((root / PAY).read_bytes())
    before = snapshot(root)
    err = error_of(
        await edit(
            pipeline, target=target, operation=operation, new_code=PY_METHOD, expected_file_sha256="0" * 64
        )
    )
    assert err["code"] == "E_PRECONDITION_FAILED" and err["details"]["actual"] == current
    assert snapshot(root) == before  # EP-3
    first = await insert(pipeline, target, operation, PY_METHOD, expected_file_sha256=current[:12])
    second = await insert(
        pipeline,
        target,
        operation,
        "def other(self):\n    return 2\n",
        expected_file_sha256=first["files_changed"][0]["sha256_after"],
    )
    assert second["applied"] is True and second["edit_id"] == "ed_0002"


# --- 構文を壊す断片は書かない（EP-5） --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, operation, code",
    [
        (VALIDATE, "insert_after", "def broken(self, x:\n    return 1\n"),
        (DISCOUNT, "insert_before", "def broken(self):\n    return (1 +\n"),
        (PROCESSOR, "insert_member", "def broken(self:\n    pass\n"),
        (PROCESSOR, "insert_member", "x = = 1\n"),
        (CTOR, "insert_after", "broken(orderId: string {\n  return 1;\n}\n"),
        (CTOR, "insert_before", "broken( {\n"),
        (CLIENT, "insert_member", "refund(orderId: string): boolean {\n  return 1;\n"),
    ],
)
async def test_a_fragment_that_breaks_the_syntax_is_refused_and_the_file_is_unchanged(
    pipeline: Pipeline, root: Path, target: dict[str, Any], operation: str, code: str
) -> None:
    before = snapshot(root)
    err = error_of(await edit(pipeline, target=target, operation=operation, new_code=code))
    assert err["code"] == "E_SYNTAX_INVALID"
    assert err["details"].get("errors") or "hint" in err["details"]
    assert snapshot(root) == before


async def test_a_fragment_that_is_valid_alone_but_breaks_the_file_is_refused(
    pipeline: Pipeline, root: Path
) -> None:
    """断片単体は正しいが、挿入先では構文が壊れる（TS の enum に宣言を入れる等）。適用後の検証が止める。"""
    enum = root / "web/src/colors.ts"
    enum.write_text("export enum Color {\n  Red,\n  Green,\n}\n")
    before = snapshot(root)
    err = error_of(
        await edit(
            pipeline,
            target={"path": "web/src/colors.ts", "symbol_name": "Color"},
            operation="insert_member",
            new_code="function f() {}\n",
        )
    )
    assert err["code"] == "E_SYNTAX_INVALID"
    assert snapshot(root) == before


# --- 引数の検査 ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("target", [VALIDATE, INIT, {"path": PAY, "symbol_name": "transactional"}])
async def test_insert_member_needs_a_container(
    pipeline: Pipeline, root: Path, target: dict[str, str]
) -> None:
    before = snapshot(root)
    err = error_of(await edit(pipeline, target=target, operation="insert_member", new_code=PY_METHOD))
    assert err["code"] == "E_INVALID_ARGUMENT" and err["details"]["param"] == "target"
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "operation, target",
    [("insert_before", VALIDATE), ("insert_after", VALIDATE), ("insert_member", PROCESSOR)],
)
async def test_mixed_tabs_and_spaces_and_empty_new_code_are_invalid_arguments(
    pipeline: Pipeline, root: Path, operation: str, target: dict[str, Any]
) -> None:
    before = snapshot(root)
    mixed = error_of(
        await edit(
            pipeline, target=target, operation=operation, new_code="def f(self):\n\treturn 1\n    # x\n"
        )
    )
    assert mixed["code"] == "E_INVALID_ARGUMENT" and mixed["details"]["param"] == "new_code"
    for missing in (None, "  \n"):
        args = {} if missing is None else {"new_code": missing}
        err = error_of(await edit(pipeline, target=target, operation=operation, **args))
        assert err["code"] == "E_INVALID_ARGUMENT" and err["details"]["param"] == "new_code"
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "target, code",
    [
        ({"path": PAY, "symbol_name": "PaymentProcessor.nope"}, "E_TARGET_NOT_FOUND"),
        ({"path": "src/shop/missing.py", "symbol_name": "x"}, "E_TARGET_NOT_FOUND"),
        ({"path": PAY}, "E_INVALID_ARGUMENT"),
    ],
)
async def test_target_errors_leave_every_byte_alone(
    pipeline: Pipeline, root: Path, target: dict[str, str], code: str
) -> None:
    before = snapshot(root)
    for operation in ("insert_before", "insert_after", "insert_member"):
        err = error_of(await edit(pipeline, target=target, operation=operation, new_code=PY_FUNC))
        assert err["code"] == code, (operation, err)
    assert snapshot(root) == before


async def test_a_file_without_a_grammar_cannot_take_an_insert(pipeline: Pipeline, root: Path) -> None:
    before = snapshot(root)
    for operation in ("insert_before", "insert_after", "insert_member"):
        err = error_of(
            await edit(
                pipeline,
                target={"path": "legacy/calc.cob", "symbol_name": "CALC"},
                operation=operation,
                new_code="MOVE 1 TO X.\n",
            )
        )
        assert err["code"] == "E_UNSUPPORTED", operation
    assert snapshot(root) == before


# --- 事後条件 5・EP-6: 改行・BOM・末尾改行 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, operation",
    [
        ({"path": CRLF, "symbol_name": "crlf_function"}, "insert_after"),
        ({"path": CRLF, "symbol_name": "crlf_function"}, "insert_before"),
        ({"path": CRLF, "symbol_name": "CrlfClass"}, "insert_member"),
        ({"path": CRLF, "symbol_name": "CrlfClass.method"}, "insert_after"),
    ],
)
async def test_postcondition_5_crlf_and_bom_survive_an_insert(
    pipeline: Pipeline, root: Path, target: dict[str, str], operation: str
) -> None:
    before = (root / CRLF).read_bytes()
    assert before.startswith(BOM) and b"\r\n" in before and b"\n" not in before.replace(b"\r\n", b"")
    code = 'def added(x: int) -> int:\n    """Adds."""\n    return x + 1\n'
    data = await insert(pipeline, target, operation, code)
    after = (root / CRLF).read_bytes()
    assert data["applied"] is True and data["syntax_check"] == "ok"
    assert after.startswith(BOM) and not after.startswith(BOM + BOM)
    assert b"\n" not in after.replace(b"\r\n", b"")  # 単独の LF は無い
    added_lines = sum(
        1 for ln in data["diff"].splitlines() if ln.startswith("+") and not ln.startswith("+++")
    )
    assert after.count(b"\r\n") - before.count(b"\r\n") == added_lines  # 足した行はすべて CRLF
    assert b'    """Adds."""\r\n' in after.replace(b"        ", b"    ") or b'"""Adds."""\r\n' in after
    assert data["files_changed"][0]["sha256_after"] == sha(after)
    assert apply_unified_diff(before, data["diff"]) == after


async def test_crlf_insert_at_the_very_first_line_keeps_the_bom_in_front(
    pipeline: Pipeline, root: Path
) -> None:
    path = root / "src/shop/models/first_line.py"
    path.write_bytes(BOM + b"def first():\r\n    return 1\r\n")
    data = await insert(
        pipeline, {"path": "src/shop/models/first_line.py", "symbol_name": "first"}, "insert_before", PY_FUNC
    )
    after = path.read_bytes()
    assert data["applied"] is True
    assert (
        after
        == BOM + b"def helper(x: int) -> int:\r\n    return x + 1\r\n\r\n\r\ndef first():\r\n    return 1\r\n"
    )


async def test_missing_final_newline_stays_missing(pipeline: Pipeline, root: Path) -> None:
    before = (root / NO_NL).read_bytes()
    assert not before.endswith(b"\n")
    await insert(pipeline, {"path": NO_NL, "symbol_name": "no_newline_at_end"}, "insert_after", PY_FUNC)
    assert (root / NO_NL).read_bytes() == before + b"\n\n\ndef helper(x: int) -> int:\n    return x + 1"


# --- 出力（compact / json） ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, operation, name",
    [
        (VALIDATE, "insert_after", "PaymentProcessor.refund"),
        (DISCOUNT, "insert_before", "PaymentProcessor.refund"),
        (PROCESSOR, "insert_member", "PaymentProcessor.refund"),
    ],
)
async def test_compact_output_follows_the_grammar(
    pipeline: Pipeline, target: dict[str, Any], operation: str, name: str
) -> None:
    r = await edit(pipeline, "compact", target=target, operation=operation, new_code=PY_METHOD)
    assert not r.is_error and g.validate(r.text) == [], r.text
    lines = r.text.splitlines()
    assert re.fullmatch(
        r"# edit_symbol_body ok conf=structural lsp=unavailable rev=WORKING_TREE ws=poly ~\d+/1500tok",
        lines[0],
    )
    assert f"applied: true  edit_id=ed_0001  op={operation}" in lines
    assert any(re.fullmatch(rf"target: lk://poly/{PAY}#{re.escape(name)} @L\d+-\d+", ln) for ln in lines), (
        r.text
    )
    assert "```diff" in lines and "syntax_check: ok" in lines


async def test_json_data_has_every_documented_field(pipeline: Pipeline) -> None:
    data = await insert(pipeline, PROCESSOR, "insert_member", PY_METHOD)
    for key in ("applied", "edit_id", "operation", "target_urn", "new_range", "indent_unit", "files_changed", "diff",
                "syntax_check", "new_diagnostics"):  # fmt: skip
        assert key in data, key
    assert data["operation"] == "insert_member" and set(data["new_range"]) == {"start_line", "end_line"}
