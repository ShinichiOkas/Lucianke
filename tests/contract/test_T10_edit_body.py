"""T-10 ``edit_symbol_body`` の契約テスト（仕様書 §11.5 T-10 の事後条件 1〜5、§10.10 の EP-1〜EP-8）。

パイプライン経由でツールを呼ぶ。``tests/fixtures/poly`` を ``tmp_path`` に写してワークスペースにする（書き込むので原本は触らない）。
LSP・監視・索引は無し（診断差分は構文エラー差分だけ。``structural``）。トランザクションの 8 段そのものは
``tests/unit/test_edit_transaction.py`` が確かめているので、ここは **ツールの入口と出口**（引数の検査、TargetRef、応答の形、
事後条件）を 1 つずつ確かめる。実装した操作は ``replace_body``（他の 4 操作は ``E_UNSUPPORTED``）。
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
from lucianke.core.model import Confidence
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.verify import DiagDelta
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.syntax.parsers import parse_source
from lucianke.tools import register_tools
from lucianke.tools._runtime import WiredServices
from lucianke.tools._services import ToolServices
from lucianke.tools.t10_edit_symbol_body import _diag_records  # pyright: ignore[reportPrivateUsage]

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PAY = "src/shop/services/payment.py"
CRLF = "src/shop/models/crlf_bom.py"
NO_NL = "src/shop/models/no_final_newline.py"
TS = "web/src/payment.ts"
COBOL = "legacy/calc.cob"
VALIDATE = {"path": PAY, "symbol_name": "PaymentProcessor._validate_limit"}

NEW_VALIDATE = """\
def _validate_limit(self, amount: Decimal) -> bool:
    limit = self._limits.get(self.currency)
    if limit is None:
        raise LimitExceeded(self.currency)
    return amount < limit
"""
CRLF_FUNCTION = 'def crlf_function(x: int) -> int:\n    """Doubles x."""\n    return x * 2\n'


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


# --- 登録・入力の検査 -----------------------------------------------------------------------------


async def test_t10_is_registered_and_declares_the_five_operations(pipeline: Pipeline) -> None:
    (desc,) = [d for d in pipeline.registry.descriptors() if d.name == "edit_symbol_body"]
    schema = desc.input_schema
    assert set(schema["required"]) == {"target", "operation"}
    assert schema["properties"]["operation"]["enum"] == [
        "replace_body", "insert_before", "insert_after", "insert_member", "delete_symbol",
    ]  # fmt: skip
    assert schema["properties"]["position"]["enum"] == ["start", "end"]
    assert schema["properties"]["dry_run"]["default"] is False


@pytest.mark.parametrize(
    "args, param",
    [
        ({"target": VALIDATE, "operation": "frobnicate", "new_code": "x"}, "operation"),
        (
            {"target": VALIDATE, "operation": "insert_member", "new_code": "x", "position": "middle"},
            "position",
        ),
        ({"target": VALIDATE, "operation": "replace_body"}, "new_code"),  # new_code 欠落
        ({"target": VALIDATE, "operation": "replace_body", "new_code": "   \n"}, "new_code"),  # 空
        ({"target": VALIDATE, "operation": "delete_symbol", "new_code": "x"}, "new_code"),
        ({"target": VALIDATE, "operation": "replace_body", "new_code": "x", "revision": "HEAD"}, "revision"),
        (
            {
                "target": {"path": PAY},
                "operation": "replace_body",
                "new_code": "x",
                "expected_file_sha256": "abc",
            },
            None,
        ),
    ],
)
async def test_invalid_arguments_are_rejected_and_write_nothing(
    pipeline: Pipeline, root: Path, args: dict[str, Any], param: str | None
) -> None:
    before = snapshot(root)
    r = await edit(pipeline, **args)
    err = error_of(r)
    assert err["code"] == "E_INVALID_ARGUMENT", err
    if param is not None:
        assert err["details"].get("param") == param
    assert snapshot(root) == before  # 事後条件 4


async def test_mixed_tabs_and_spaces_in_new_code_is_an_invalid_argument(
    pipeline: Pipeline, root: Path
) -> None:
    before = snapshot(root)
    r = await edit(
        pipeline,
        target=VALIDATE,
        operation="replace_body",
        new_code="def _validate_limit(self, a):\n\treturn 1\n    # x\n",
    )
    err = error_of(r)
    assert err["code"] == "E_INVALID_ARGUMENT" and err["details"]["param"] == "new_code"
    assert snapshot(root) == before


async def test_bare_tool_services_without_edit_services_says_so(root: Path) -> None:
    """編集の依存（``locks`` / ``writer_index`` / ``journal_dir``）も ``edit_transaction`` も持たない素の ``ToolServices`` → ``edit_services_missing``。"""
    base = build_services(lambda: [str(root)])
    bare = ToolServices(roots=base.roots, trees=base.trees, lines=base.lines)
    registry = ToolRegistry()
    register_tools(registry)
    pipe = Pipeline(registry, services=bare)
    before = snapshot(root)
    r = await edit(pipe, target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE)
    err = error_of(r)
    assert err["code"] == "E_UNSUPPORTED" and err["details"]["reason"] == "edit_services_missing"
    assert set(err["details"]["missing"]) == {"locks", "writer_index", "journal_dir"}
    assert snapshot(root) == before


async def test_wired_services_with_the_index_not_open_says_so(root: Path) -> None:
    """結線済み（``WiredServices``）だが、このワークスペースの束が開いていない（束の表〈``hub``〉が無い、起動直後で開けていない）→ ``index_not_open``。

    ``edit_services_missing``（そもそも編集サービスが無いビルド）とは別の理由。呼び出し側が「待てば直る」と「直らない」を区別できるように。
    """
    services = build_services(lambda: [str(root)])  # hub 無し = 束が 1 つも開いていない
    assert isinstance(services, WiredServices) and services.hub is None
    registry = ToolRegistry()
    register_tools(registry)
    pipe = Pipeline(registry, services=services)
    before = snapshot(root)
    r = await edit(pipe, target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE)
    err = error_of(r)
    assert err["code"] == "E_UNSUPPORTED" and err["details"]["reason"] == "index_not_open"
    assert err["details"]["workspace_id"] == services.workspace().workspace_id
    assert "missing" not in err["details"]
    assert snapshot(root) == before


# --- 事後条件 1: sha256_after・diff・EP-1 ------------------------------------------------------------


async def test_postcondition_1_sha_after_matches_disk_and_diff_reproduces_the_bytes(
    pipeline: Pipeline, root: Path
) -> None:
    before = (root / PAY).read_bytes()
    r = await edit(pipeline, target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE)
    data = data_of(r)
    assert data["applied"] is True and data["edit_id"] == "ed_0001" and data["operation"] == "replace_body"
    (change,) = data["files_changed"]
    after = (root / PAY).read_bytes()
    assert change["path"] == PAY and change["sha256_before"] == sha(before)
    assert change["sha256_after"] == sha(after)  # EP-1
    assert change["hunks"] == 1
    assert apply_unified_diff(before, data["diff"]) == after  # diff を適用した結果とバイト単位で一致
    assert b"return amount < limit" in after and b"return amount <= limit" not in after
    assert data["syntax_check"] == "ok" and data["indent_unit"] == "4sp"


# --- 事後条件 2: 構文 ----------------------------------------------------------------------------


async def test_postcondition_2_error_nodes_are_a_subset_of_the_ones_before(
    pipeline: Pipeline, root: Path
) -> None:
    before = (root / PAY).read_bytes()
    data = data_of(await edit(pipeline, target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE))
    assert data["syntax_check"] == "ok"
    assert error_node_count((root / PAY).read_bytes()) <= error_node_count(before) == 0


@pytest.mark.parametrize(
    "new_code, why",
    [
        ("def _validate_limit(self, amount:\n    return 1\n", "unclosed signature"),
        ("def _validate_limit(self, amount):\n    return (1 +\n", "unclosed expression"),
        ("return amount < 1\n", "body only"),
    ],
)
async def test_an_edit_that_would_add_a_syntax_error_is_refused(
    pipeline: Pipeline, root: Path, new_code: str, why: str
) -> None:
    before = snapshot(root)
    err = error_of(await edit(pipeline, target=VALIDATE, operation="replace_body", new_code=new_code))
    assert err["code"] == "E_SYNTAX_INVALID", why
    assert err["details"].get("errors") or "hint" in err["details"]
    assert snapshot(root) == before


async def test_body_only_new_code_gets_the_whole_definition_hint(pipeline: Pipeline) -> None:
    err = error_of(
        await edit(pipeline, target=VALIDATE, operation="replace_body", new_code="return amount < 1\n")
    )
    assert "シグネチャを含む定義全体" in err["details"]["hint"]


# --- 事後条件 3: 再解決 ----------------------------------------------------------------------------


async def test_postcondition_3_target_urn_resolves_to_one_symbol_with_new_range(pipeline: Pipeline) -> None:
    data = data_of(await edit(pipeline, target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE))
    urn, new_range = data["target_urn"], data["new_range"]
    assert urn.endswith("PaymentProcessor._validate_limit") and new_range is not None
    got = await pipeline.call_tool(
        "get_symbol_bodies", {"format": "json", "targets": [{"urn": urn}], "context_lines": 0}
    )
    (item,) = data_of(got)["items"]
    assert item["ok"] and item["range_returned"] == new_range
    assert item["code"].strip().endswith("return amount < limit")
    assert item["file_sha256"] == data["files_changed"][0]["sha256_after"]


# --- 事後条件 4: applied: false のとき何も変わらない（EP-2, EP-3） -----------------------------------------


async def test_postcondition_4_dry_run_changes_nothing_and_predicts_the_write(
    pipeline: Pipeline, root: Path
) -> None:
    before = snapshot(root)
    dry = data_of(
        await edit(pipeline, target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE, dry_run=True)
    )
    assert dry["applied"] is False and dry["edit_id"] is None and dry["new_range"] is None
    assert dry["diff"] and dry["new_diagnostics"]["status"] == "not_computed"
    assert snapshot(root) == before

    real = data_of(await edit(pipeline, target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE))
    assert real["files_changed"][0]["sha256_after"] == dry["files_changed"][0]["sha256_after"]  # 予測のとおり
    assert real["diff"] == dry["diff"]


async def test_postcondition_4_wrong_expected_sha_is_refused(pipeline: Pipeline, root: Path) -> None:
    before = snapshot(root)
    err = error_of(
        await edit(
            pipeline,
            target=VALIDATE,
            operation="replace_body",
            new_code=NEW_VALIDATE,
            expected_file_sha256="0" * 64,
        )
    )
    assert err["code"] == "E_PRECONDITION_FAILED"
    assert err["details"]["path"] == PAY and err["details"]["actual"] == before[PAY]
    assert snapshot(root) == before


async def test_expected_sha_prefix_and_chaining_with_the_sha_after(pipeline: Pipeline, root: Path) -> None:
    current = sha((root / PAY).read_bytes())
    first = data_of(
        await edit(
            pipeline,
            target=VALIDATE,
            operation="replace_body",
            new_code=NEW_VALIDATE,
            expected_file_sha256=current[:12],
        )
    )
    assert first["applied"] is True
    # 直前の応答の sha256_after をそのまま次の前提にできる。古い値は拒否される
    again = NEW_VALIDATE.replace("amount < limit", "amount <= limit")
    stale = await edit(
        pipeline, target=VALIDATE, operation="replace_body", new_code=again, expected_file_sha256=current
    )
    assert error_of(stale)["code"] == "E_PRECONDITION_FAILED"
    second = data_of(
        await edit(
            pipeline,
            target=VALIDATE,
            operation="replace_body",
            new_code=again,
            expected_file_sha256=first["files_changed"][0]["sha256_after"],
        )  # fmt: skip
    )
    assert second["applied"] is True and second["edit_id"] == "ed_0002"
    assert (root / PAY).read_bytes().count(b"return amount <= limit") == 1


@pytest.mark.parametrize("short", ["abc", "z" * 12, "0123456789a"])
async def test_expected_sha_must_be_hex_and_at_least_twelve_digits(pipeline: Pipeline, short: str) -> None:
    err = error_of(
        await edit(
            pipeline,
            target=VALIDATE,
            operation="replace_body",
            new_code=NEW_VALIDATE,
            expected_file_sha256=short,
        )
    )
    assert err["code"] == "E_INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "target, code",
    [
        ({"path": PAY, "symbol_name": "PaymentProcessor.nope"}, "E_TARGET_NOT_FOUND"),
        ({"path": "src/shop/missing.py", "symbol_name": "x"}, "E_TARGET_NOT_FOUND"),
        ({"urn": "lk://poly/src/shop/services/payment.py#Nope"}, "E_TARGET_NOT_FOUND"),
        ({"path": "../outside.py", "symbol_name": "x"}, "E_INVALID_ARGUMENT"),
        ({"path": PAY}, "E_INVALID_ARGUMENT"),
    ],
)
async def test_target_errors_leave_every_byte_alone(
    pipeline: Pipeline, root: Path, target: dict[str, str], code: str
) -> None:
    before = snapshot(root)
    err = error_of(await edit(pipeline, target=target, operation="replace_body", new_code=NEW_VALIDATE))
    assert err["code"] == code, err
    assert snapshot(root) == before


async def test_all_target_forms_reach_the_same_method(pipeline: Pipeline, root: Path) -> None:
    urn = f"lk://poly/{PAY}#PaymentProcessor._validate_limit"
    for ref in (
        VALIDATE,
        {"path": PAY, "symbol_name": "_validate_limit"},
        {"urn": urn},
        {"path": PAY, "line": 56},
    ):
        data = data_of(
            await edit(pipeline, target=ref, operation="replace_body", new_code=NEW_VALIDATE, dry_run=True)
        )
        assert data["target_urn"].endswith("PaymentProcessor._validate_limit"), ref
        assert data["files_changed"][0]["path"] == PAY


# --- 事後条件 5・EP-6: 改行・BOM・末尾改行 --------------------------------------------------------------


async def test_postcondition_5_crlf_and_bom_survive_an_edit(pipeline: Pipeline, root: Path) -> None:
    before = (root / CRLF).read_bytes()
    assert before.startswith(b"\xef\xbb\xbf") and b"\r\n" in before
    new_code = 'def crlf_function(x: int) -> int:\n    """Triples x."""\n    return x * 3\n'
    data = data_of(
        await edit(
            pipeline,
            target={"path": CRLF, "symbol_name": "crlf_function"},
            operation="replace_body",
            new_code=new_code,
        )
    )
    after = (root / CRLF).read_bytes()
    assert (
        data["applied"] is True
        and after.startswith(b"\xef\xbb\xbf")
        and not after.startswith(b"\xef\xbb\xbf\xef\xbb\xbf")
    )
    assert b"return x * 3\r\n" in after
    assert after.count(b"\r\n") == before.count(b"\r\n") and b"\n" not in after.replace(b"\r\n", b"")
    assert data["files_changed"][0]["sha256_after"] == sha(after)


@pytest.mark.parametrize(
    "path, name, code",
    [
        (CRLF, "crlf_function", CRLF_FUNCTION),
        (NO_NL, "no_newline_at_end", "def no_newline_at_end(value: int) -> int:\n    return value + 1\n"),
    ],
)
async def test_ep6_identity_edit_keeps_every_byte(
    pipeline: Pipeline, root: Path, path: str, name: str, code: str
) -> None:
    before = (root / path).read_bytes()
    mtime = (root / path).stat().st_mtime_ns
    data = data_of(
        await edit(
            pipeline, target={"path": path, "symbol_name": name}, operation="replace_body", new_code=code
        )
    )
    assert data["applied"] is True and data["edit_id"] is None and data["diff"] == ""
    assert data["files_changed"][0]["sha256_after"] == sha(before)
    assert (root / path).read_bytes() == before and (root / path).stat().st_mtime_ns == mtime


# --- TypeScript の replace_body -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, new_code, expect_in_file",
    [
        # クラスのメンバー（コンストラクタ・メソッド）は、単体ではモジュール直下で構文エラーになる断片
        (
            {"path": TS, "symbol_name": "PaymentClient.constructor"},
            "constructor(private readonly baseUrl: string, private readonly timeoutMs = 1000) {}\n",
            "private readonly timeoutMs = 1000",
        ),
        (
            {"path": TS, "line": 15},  # throw の行 → 最も内側のシンボル（charge の実装）
            "async charge(req: PaymentRequest, retries = 0): Promise<string> {\n  return `${retries}`;\n}\n",
            "return `${retries}`;",
        ),
        (
            {"path": TS, "symbol_name": "PaymentRequest"},
            'export interface PaymentRequest {\n  orderId: string;\n  amount: number;\n  currency: "JPY" | "USD" | "EUR";\n}\n',
            '"JPY" | "USD" | "EUR"',
        ),
        (
            {"path": TS, "symbol_name": "PaymentClient"},
            "export class PaymentClient {\n  ping(): string {\n    return 'pong';\n  }\n}\n",
            "ping(): string",
        ),
    ],
)
async def test_typescript_replace_body(
    pipeline: Pipeline, root: Path, target: dict[str, Any], new_code: str, expect_in_file: str
) -> None:
    before = (root / TS).read_bytes()
    data = data_of(await edit(pipeline, target=target, operation="replace_body", new_code=new_code))
    after = (root / TS).read_bytes()
    assert data["applied"] is True and data["syntax_check"] == "ok" and data["indent_unit"] == "2sp"
    assert expect_in_file.encode() in after and after != before
    assert error_node_count(after, "typescript") == 0
    assert data["files_changed"][0]["sha256_after"] == sha(after)
    assert apply_unified_diff(before, data["diff"]) == after


async def test_typescript_overloaded_name_is_ambiguous_with_candidates(
    pipeline: Pipeline, root: Path
) -> None:
    before = snapshot(root)
    err = error_of(
        await edit(
            pipeline,
            target={"path": TS, "symbol_name": "charge"},
            operation="replace_body",
            new_code="charge(): void {}\n",
        )
    )
    assert err["code"] == "E_TARGET_AMBIGUOUS" and len(err["candidates"]) == 3
    assert all(c["urn"].startswith("lk://poly/") for c in err["candidates"])
    assert snapshot(root) == before


async def test_typescript_syntax_error_in_a_member_is_refused(pipeline: Pipeline, root: Path) -> None:
    before = snapshot(root)
    err = error_of(
        await edit(
            pipeline,
            target={"path": TS, "symbol_name": "PaymentClient.constructor"},
            operation="replace_body",
            new_code="constructor(private readonly baseUrl: string {}\n",
        )  # fmt: skip
    )
    assert err["code"] == "E_SYNTAX_INVALID"
    assert snapshot(root) == before


# --- 降格・Tier 4 -----------------------------------------------------------------------------------------


async def test_a_file_without_a_grammar_cannot_be_replaced(pipeline: Pipeline, root: Path) -> None:
    before = snapshot(root)
    err = error_of(
        await edit(
            pipeline,
            target={"path": COBOL, "symbol_name": "CALC"},
            operation="replace_body",
            new_code="MOVE 1 TO X.\n",
        )
    )
    assert err["code"] == "E_UNSUPPORTED"
    assert snapshot(root) == before


# --- 出力（compact / json） ----------------------------------------------------------------------------------


async def test_compact_output_follows_the_grammar_and_the_spec_example(
    pipeline: Pipeline, root: Path
) -> None:
    r = await edit(pipeline, "compact", target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE)
    assert not r.is_error and g.validate(r.text) == [], r.text
    lines = r.text.splitlines()
    assert re.fullmatch(
        r"# edit_symbol_body ok conf=structural lsp=unavailable rev=WORKING_TREE ws=poly ~\d+/1500tok",
        lines[0],
    )
    kv = {ln.split(":", 1)[0]: ln.split(": ", 1)[1] for ln in lines if re.match(r"^[a-z_]+: ", ln)}
    assert kv["applied"] == "true  edit_id=ed_0001  op=replace_body"
    assert re.fullmatch(rf"lk://poly/{PAY}#PaymentProcessor\._validate_limit @L\d+-\d+", kv["target"])
    assert re.fullmatch(
        rf"{re.escape(PAY)} \(\+\d+ -\d+\)  sha256 [0-9a-f]{{12}}…→[0-9a-f]{{12}}…", kv["files_changed"]
    )
    assert kv["syntax_check"] == "ok"
    assert kv["new_diagnostics"] == "complete (+0 -0) checked=1 files"
    assert "```diff" in lines and "+        return amount < limit" in r.text.replace(
        "+    return", "+        return"
    )  # 差分はフェンスの中


async def test_dry_run_compact_says_applied_false(pipeline: Pipeline) -> None:
    r = await edit(
        pipeline, "compact", target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE, dry_run=True
    )
    assert g.validate(r.text) == [], r.text
    assert (
        "applied: false  edit_id=-  op=replace_body" in r.text and "new_diagnostics: not_computed" in r.text
    )


async def test_json_data_has_every_documented_field(pipeline: Pipeline) -> None:
    r = await edit(pipeline, target=VALIDATE, operation="replace_body", new_code=NEW_VALIDATE)
    data = data_of(r)
    for key in ("applied", "edit_id", "operation", "target_urn", "new_range", "indent_unit", "files_changed", "diff",
                "syntax_check", "new_diagnostics"):  # fmt: skip
        assert key in data, key
    assert set(data["new_diagnostics"]) == {"status", "added", "resolved", "checked_files"}
    assert data["new_diagnostics"]["status"] == "complete"
    assert set(data["files_changed"][0]) == {"path", "sha256_before", "sha256_after", "hunks"}
    assert set(data["new_range"]) == {"start_line", "end_line"}
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "structural" and meta["is_degraded"] is False


def test_diagnostic_sections_render_as_diag_lines() -> None:
    """``added:`` / ``resolved:`` の見出しの後ろに診断行が続く（診断差分を作る LSP が無い build では、整形だけを確かめる）。"""
    svc = build_services(lambda: ["."])
    delta = DiagDelta(
        status="complete",
        resolved=(
            {
                "path": PAY,
                "severity": "warning",
                "source": "pyright",
                "code": "reportUnusedVariable",
                "start_line": 120,
                "start_col": 9,
                "end_line": 120,
                "end_col": 12,
                "message": '変数 "tmp" は使われていません',
                "enclosing_urn": None,
                "confidence_level": "exact",
            },
        ),  # fmt: skip
        added=(
            {
                "path": PAY,
                "severity": "error",
                "source": "tree-sitter",
                "code": None,
                "start_line": 3,
                "start_col": 1,
                "end_line": 3,
                "end_col": 2,
                "message": "syntax error",
                "enclosing_urn": None,
                "confidence_level": "structural",
            },
        ),  # fmt: skip
    )
    records = _diag_records(svc, "resolved", delta.resolved, 2, Confidence.EXACT) + _diag_records(
        svc, "added", delta.added, 1, Confidence.EXACT
    )
    text = "\n".join(line for rec in records for line in rec.lines) + "\n"
    assert g.validate(text, require_header=False) == [], text
    assert f'resolved:\nW {PAY}:120:9 [pyright:reportUnusedVariable] 変数 "tmp" は使われていません' in text
    assert f"added:\nE {PAY}:3:1 [tree-sitter:-] syntax error" in text
