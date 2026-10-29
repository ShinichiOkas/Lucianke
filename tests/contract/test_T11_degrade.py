"""T-11 ``rename_symbol`` の降格経路の契約テスト（仕様書 T-11「精度と降格」、§8.4、§10.5）。LSP は無し（``lsp_pool`` なし）。

* ``lexical_fallback``: 文法の無いファイル（Ruby の ``.rb``。Tier 4）。同じ拡張子のファイルの単語境界の字句置換。
  ``allow_heuristic_mutation`` が無ければ強制 ``dry_run``（成功応答）、あれば ``expected_file_sha256s`` に定義のファイルが要る。
  新しい名前が書き換え先に単語として既にあれば ``E_MUTATION_BLOCKED``。``scope_paths`` は配下に限る。
* ``structural``: Python の関数内のローカル変数・引数（``path`` + ``line`` + ``character``）を、構文木だけで同一ファイル内改名する。
  同名の別関数・属性・キーワード引数・コメント・文字列を巻き込まない。それ以外は元の ``E_LSP_UNAVAILABLE``。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lucianke.app import build_services
from lucianke.edit.lock import WorkspaceLocks
from lucianke.edit.transaction import EditTransaction
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import t11_rename_symbol as t11
from lucianke.tools._services import ToolServices
from lucianke.workspace.types import Workspace

WS = "deg"
BOM = b"\xef\xbb\xbf"

MAIN_RB = """\
# charge_order: bills the order
def charge_order(req)
  puts "charge_order called"
  total = charge_order_total(req)
  total + 1
end

x = charge_order(1)
"""
OTHER_RB = "y = charge_order(2)\nz = my_charge_order(3)\n"
THIRD_RB = "w = charge_order(4)\n"
TEXT_TXT = "charge_order stays in text files\n"
PY_SAME = "def charge_order():\n    return 1\n"

PY_SRC = '''\
class Shop:
    def total(self, order, extra=0):
        """order: the order"""
        count = order.count + extra  # order is a parameter
        label = "order"
        helper = lambda order: order * 2
        return self.charge(order=order, count=count) + helper(count)

    def order(self):
        return 1


def other(order):
    return order


def outer(order):
    def inner(n):
        return order + n

    def shadow(order):
        return order

    return inner(1) + shadow(2) + order


GLOBAL = 1


def reads_global():
    return GLOBAL


def declares():
    global GLOBAL
    GLOBAL = 2
    return GLOBAL
'''


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): sha(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


@dataclass(slots=True)
class Services(ToolServices):
    ws: Workspace | None = None
    lsp_pool: Any = None
    tx_kwargs: dict[str, Any] = field(default_factory=dict)

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        assert self.ws is not None
        return self.ws

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        return WS

    def edit_transaction(self, ws: Workspace, **overrides: Any) -> EditTransaction:
        return EditTransaction(ws, **self.tx_kwargs, **overrides)


@dataclass
class Rig:
    pipeline: Pipeline
    root: Path

    async def rename(self, target: dict[str, Any], new_name: str, **args: Any):
        return await self.pipeline.call_tool(
            "rename_symbol", {"target": target, "new_name": new_name, "format": "json", **args}
        )

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def put(self, rel: str, data: str | bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)

    def sha_of(self, rel: str) -> str:
        return sha(self.read(rel))


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    writer = IndexWriter(tmp_path / "cache" / "paad.sqlite")
    ws = Workspace(workspace_id=WS, root=root)
    registry = ToolRegistry()
    registry.register(t11.TOOL_SPEC)
    b = build_services(lambda: [str(root)])
    svc = Services(
        roots=b.roots,
        trees=b.trees,
        lines=b.lines,
        ws=ws,
        lsp_pool=None,
        tx_kwargs={
            "locks": WorkspaceLocks(),
            "writer_index": writer,
            "journal_dir": tmp_path / "cache" / "journal",
        },
    )
    r = Rig(Pipeline(registry, services=svc), root)
    r.put("app/main.rb", MAIN_RB)
    r.put("app/other.rb", OTHER_RB)
    r.put("lib/third.rb", THIRD_RB)
    r.put("app/data.txt", TEXT_TXT)
    r.put("app/same.py", PY_SAME)
    try:
        yield r
    finally:
        await asyncio.to_thread(writer.close)


def code(r: Any) -> str:
    assert r.is_error, r.text
    return r.structured["error"]["code"]


def word_replace(text: str, old: str, new: str) -> str:
    return re.sub(rf"(?<!\w){re.escape(old)}(?!\w)", new, text)


NAME = {"path": "app/main.rb", "symbol_name": "charge_order"}


# --- lexical_fallback ---------------------------------------------------------------------------


async def test_lexical_without_the_flag_is_a_forced_dry_run(rig: Rig):
    before = snapshot(rig.root)
    r = await rig.rename(NAME, "bill_order")
    assert not r.is_error, r.text
    data = r.structured["data"]
    assert data["applied"] is False and data["edit_id"] is None
    assert data["old_name"] == "charge_order" and data["new_name"] == "bill_order"
    assert "-def charge_order(req)" in data["diff"] and "+def bill_order(req)" in data["diff"]
    assert {c["path"] for c in data["files_changed"]} == {"app/main.rb", "app/other.rb", "lib/third.rb"}
    assert data["edits_count"] == 6  # main.rb 4（コメント・定義・文字列・呼び出し）+ other.rb 1 + third.rb 1
    assert data["syntax_check"] == "skipped"
    assert any("降格のため書き込みを抑止" in w for w in r.structured["warnings"])
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "lexical_fallback" and meta["is_degraded"] is True
    assert meta["engine_used"] == "lexical-replace"
    assert "コメント" in meta["accuracy_warning"] and "文字列" in meta["accuracy_warning"]
    assert snapshot(rig.root) == before


async def test_lexical_write_replaces_words_only_and_only_same_extension(rig: Rig):
    before = snapshot(rig.root)
    main_before = rig.read("app/main.rb")
    r = await rig.rename(
        NAME,
        "bill_order",
        allow_heuristic_mutation=True,
        expected_file_sha256s={"app/main.rb": rig.sha_of("app/main.rb")},
    )
    assert not r.is_error, r.text
    data = r.structured["data"]
    assert data["applied"] is True and data["edit_id"]
    after_main = rig.read("app/main.rb").decode()
    assert after_main == word_replace(MAIN_RB, "charge_order", "bill_order")
    assert "charge_order_total" in after_main  # 単語の一部は巻き込まない
    assert (
        "# bill_order: bills" in after_main and '"bill_order called"' in after_main
    )  # コメント・文字列も置換される（仕様）
    assert rig.read("app/other.rb").decode() == "y = bill_order(2)\nz = my_charge_order(3)\n"
    assert rig.read("lib/third.rb").decode() == "w = bill_order(4)\n"
    changed = {p for p, s in snapshot(rig.root).items() if s != before[p]}
    assert changed == {"app/main.rb", "app/other.rb", "lib/third.rb"}  # 別の拡張子（.txt, .py）は触れない
    files = {c["path"]: c for c in data["files_changed"]}
    assert files["app/main.rb"]["sha256_before"] == sha(main_before)
    assert files["app/main.rb"]["sha256_after"] == rig.sha_of("app/main.rb")
    assert all(rig.sha_of(p) == c["sha256_after"] for p, c in files.items())
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "lexical_fallback" and meta["is_degraded"] is True


async def test_lexical_write_needs_expected_sha_of_the_definition_file(rig: Rig):
    before = snapshot(rig.root)
    r = await rig.rename(NAME, "bill_order", allow_heuristic_mutation=True)
    assert code(r) == "E_INVALID_ARGUMENT"
    assert r.structured["error"]["details"]["reason"] == "required_for_lexical_fallback"
    r = await rig.rename(
        NAME,
        "bill_order",
        allow_heuristic_mutation=True,
        expected_file_sha256s={"app/other.rb": rig.sha_of("app/other.rb")},
    )
    assert code(r) == "E_INVALID_ARGUMENT"
    assert snapshot(rig.root) == before


async def test_lexical_stale_expected_sha_is_precondition_failed(rig: Rig):
    before = snapshot(rig.root)
    r = await rig.rename(
        NAME,
        "bill_order",
        allow_heuristic_mutation=True,
        expected_file_sha256s={"app/main.rb": "0" * 64},
    )
    assert code(r) == "E_PRECONDITION_FAILED"
    assert snapshot(rig.root) == before


async def test_lexical_explicit_dry_run_with_the_flag_writes_nothing(rig: Rig):
    before = snapshot(rig.root)
    r = await rig.rename(NAME, "bill_order", allow_heuristic_mutation=True, dry_run=True)
    assert not r.is_error, r.text
    assert r.structured["data"]["applied"] is False and r.structured["data"]["diff"]
    assert snapshot(rig.root) == before


async def test_lexical_new_name_already_a_word_is_blocked(rig: Rig):
    rig.put("app/other.rb", OTHER_RB + "bill_order = 1\n")
    before = snapshot(rig.root)
    r = await rig.rename(
        NAME,
        "bill_order",
        allow_heuristic_mutation=True,
        expected_file_sha256s={"app/main.rb": rig.sha_of("app/main.rb")},
    )
    assert code(r) == "E_MUTATION_BLOCKED"
    err = r.structured["error"]["details"]
    assert err["reason"] == "new_name_already_present" and err["path"] == "app/other.rb"
    assert snapshot(rig.root) == before
    # 許可が無いときは強制 dry_run の成功応答で、警告に書く（エラーにしない）
    r = await rig.rename(NAME, "bill_order")
    assert not r.is_error and r.structured["data"]["applied"] is False
    assert any("already appears as a word" in w for w in r.structured["warnings"])
    assert snapshot(rig.root) == before


async def test_lexical_new_name_in_a_file_that_is_not_rewritten_is_fine(rig: Rig):
    rig.put("app/unrelated.rb", "bill_order = 1\n")  # charge_order を含まないので書き換え対象ではない
    r = await rig.rename(
        NAME,
        "bill_order",
        allow_heuristic_mutation=True,
        expected_file_sha256s={"app/main.rb": rig.sha_of("app/main.rb")},
    )
    assert not r.is_error, r.text
    assert r.structured["data"]["applied"] is True
    assert rig.read("app/unrelated.rb") == b"bill_order = 1\n"


async def test_lexical_scope_paths_limits_the_files(rig: Rig):
    before = snapshot(rig.root)
    r = await rig.rename(
        NAME,
        "bill_order",
        scope_paths=["app"],
        allow_heuristic_mutation=True,
        expected_file_sha256s={"app/main.rb": rig.sha_of("app/main.rb")},
    )
    assert not r.is_error, r.text
    after = snapshot(rig.root)
    assert {p for p in after if after[p] != before[p]} == {"app/main.rb", "app/other.rb"}
    assert rig.read("lib/third.rb").decode() == THIRD_RB  # 範囲外
    # 1 ファイルだけを指すこともできる
    r = await rig.rename(
        {"path": "app/main.rb", "symbol_name": "bill_order"},
        "pay_order",
        scope_paths=["app/main.rb"],
        dry_run=True,
    )
    assert not r.is_error, r.text
    assert [c["path"] for c in r.structured["data"]["files_changed"]] == ["app/main.rb"]


async def test_lexical_scope_paths_must_include_the_definition_file(rig: Rig):
    r = await rig.rename(NAME, "bill_order", scope_paths=["lib"])
    assert code(r) == "E_INVALID_ARGUMENT"
    assert r.structured["error"]["details"]["reason"] == "target_outside_scope"
    r = await rig.rename(NAME, "bill_order", scope_paths=["../elsewhere"])
    assert r.is_error  # ワークスペースの外


async def test_lexical_by_line_and_character_marks_the_definition(rig: Rig):
    r = await rig.rename({"path": "app/main.rb", "line": 2, "character": 8}, "bill_order", dry_run=True)
    assert not r.is_error, r.text
    data = r.structured["data"]
    assert data["old_name"] == "charge_order"
    edits = {(e["path"], e["line"]): e for e in data["edits"]}
    assert edits[("app/main.rb", 2)]["use"] == "def"  # 指した位置が定義
    assert edits[("app/main.rb", 8)]["use"] == "call"
    assert edits[("app/other.rb", 1)]["use"] == "call"


async def test_lexical_keeps_bom_crlf_and_non_ascii(rig: Rig):
    text = (
        "# 注文 charge_order\r\ndef charge_order(x)\r\n  '注文' + charge_order_x(x) # charge_order\r\nend\r\n"
    )
    raw = BOM + text.encode("utf-8")
    rig.put("app/crlf.rb", raw)
    r = await rig.rename(
        {"path": "app/crlf.rb", "symbol_name": "charge_order"},
        "bill_order",
        scope_paths=["app/crlf.rb"],
        allow_heuristic_mutation=True,
        expected_file_sha256s={"app/crlf.rb": sha(raw)},
    )
    assert not r.is_error, r.text
    after = rig.read("app/crlf.rb")
    assert after.startswith(BOM)
    assert after == BOM + word_replace(text, "charge_order", "bill_order").encode("utf-8")
    assert after.count(b"\r\n") == raw.count(b"\r\n") and b"\n" not in after.replace(b"\r\n", b"")


async def test_lexical_target_word_must_exist_in_the_file(rig: Rig):
    before = snapshot(rig.root)
    r = await rig.rename({"path": "app/main.rb", "symbol_name": "nothing_here"}, "bill_order")
    assert code(r) == "E_TARGET_NOT_FOUND"
    r = await rig.rename(NAME, "charge_order")
    assert code(r) == "E_INVALID_ARGUMENT"
    r = await rig.rename(NAME, "9bad")
    assert code(r) == "E_INVALID_ARGUMENT"
    r = await rig.rename(NAME, "bill order")
    assert code(r) == "E_INVALID_ARGUMENT"
    r = await rig.rename(NAME, "bill_order", revision="HEAD")
    assert code(r) == "E_INVALID_ARGUMENT"
    assert snapshot(rig.root) == before


async def test_tier_3_is_unsupported(rig: Rig):
    rig.put("doc/readme.md", "# charge_order\n")
    rig.put("conf/a.json", '{"charge_order": 1}\n')
    for rel in ("doc/readme.md", "conf/a.json"):
        r = await rig.rename({"path": rel, "symbol_name": "charge_order"}, "bill_order")
        assert code(r) == "E_UNSUPPORTED", rel


# --- structural ---------------------------------------------------------------------------------


def at(src: str, needle: str, nth: int = 0, *, line_hint: str | None = None) -> dict[str, Any]:
    """``needle`` の ``nth`` 番目の出現（``line_hint`` を含む行に限る）を指す TargetRef（1 始まり）。"""
    seen = 0
    for i, text in enumerate(src.split("\n")):
        if line_hint is not None and line_hint not in text:
            continue
        for m in re.finditer(rf"(?<!\w){re.escape(needle)}(?!\w)", text):
            if seen == nth:
                return {"path": "m.py", "line": i + 1, "character": m.start() + 1}
            seen += 1
    raise AssertionError(f"{needle!r} #{nth} not found")


async def test_structural_renames_a_parameter_inside_its_function_only(rig: Rig):
    rig.put("m.py", PY_SRC)
    before = snapshot(rig.root)
    r = await rig.rename(at(PY_SRC, "order", line_hint="def total"), "req")
    assert not r.is_error, r.text
    data = r.structured["data"]
    assert data["applied"] is True and data["syntax_check"] == "ok"
    after = rig.read("m.py").decode()
    total = after.split("def order(self)")[0]
    assert "def total(self, req, extra=0):" in total
    assert "count = req.count + extra  # order is a parameter" in total  # コメントは触れない
    assert 'label = "order"' in total and '"""order: the order"""' in total  # 文字列は触れない
    assert "helper = lambda order: order * 2" in total  # ラムダの引数は別の変数
    assert "self.charge(order=req, count=count)" in total  # キーワード引数の名前は触れない
    assert "def order(self):" in after  # メソッド名は別の識別子
    assert "def other(order):\n    return order" in after  # 別の関数の同名の引数
    assert "def outer(order):" in after
    changed = {p for p, s in snapshot(rig.root).items() if s != before[p]}
    assert changed == {"m.py"}
    meta = r.structured["_resolution_meta"]
    assert meta["confidence_level"] == "structural" and meta["is_degraded"] is True
    assert meta["engine_used"] == "tree-sitter"
    assert data["edits_count"] == 3  # 引数・order.count・order=req の値


async def test_structural_closure_variable_is_renamed_in_nested_functions_but_not_shadows(rig: Rig):
    rig.put("m.py", PY_SRC)
    r = await rig.rename(at(PY_SRC, "order", line_hint="def outer"), "ticket")
    assert not r.is_error, r.text
    after = rig.read("m.py").decode()
    outer = after.split("def outer(")[1].split("GLOBAL = 1")[0]
    assert outer.startswith("ticket):")
    assert "return ticket + n" in outer  # 入れ子の関数が外側の変数を使う
    assert "def shadow(order):\n        return order" in outer  # 自分で束縛する入れ子の関数は別の変数
    assert "+ shadow(2) + ticket" in outer


async def test_structural_local_variable_by_assignment(rig: Rig):
    rig.put("m.py", PY_SRC)
    r = await rig.rename(at(PY_SRC, "count", line_hint="count = order.count"), "n_items")
    assert not r.is_error, r.text
    after = rig.read("m.py").decode()
    assert "n_items = order.count + extra" in after  # order.count の属性 count は触れない
    assert "self.charge(order=order, count=n_items)" in after  # キーワード引数の名前 count は触れない
    assert "helper(n_items)" in after


async def test_structural_dry_run_writes_nothing_and_keeps_bom_crlf(rig: Rig):
    src = "def f(order):\r\n    return order + 1  # order\r\n"
    raw = BOM + src.encode("utf-8")
    rig.put("m.py", raw)
    r = await rig.rename(at(src, "order", line_hint="def f"), "req", dry_run=True)
    assert not r.is_error and r.structured["data"]["applied"] is False
    assert rig.read("m.py") == raw
    r = await rig.rename(at(src, "order", line_hint="def f"), "req")
    assert not r.is_error, r.text
    assert rig.read("m.py") == BOM + b"def f(req):\r\n    return req + 1  # order\r\n"


@pytest.mark.parametrize(
    "target",
    [
        pytest.param({"path": "m.py", "symbol_name": "Shop.order"}, id="method-by-name"),
        pytest.param({"urn": "lk://deg/m.py#Shop.total"}, id="method-by-urn"),
    ],
)
async def test_structural_non_local_targets_keep_the_lsp_error(rig: Rig, target: dict[str, Any]):
    rig.put("m.py", PY_SRC)
    before = snapshot(rig.root)
    r = await rig.rename(target, "renamed")
    assert code(r) == "E_LSP_UNAVAILABLE"
    assert snapshot(rig.root) == before


async def test_structural_module_global_and_declared_names_are_not_renamed(rig: Rig):
    rig.put("m.py", PY_SRC)
    before = snapshot(rig.root)
    for hint in ("def reads_global", "GLOBAL = 1"):
        r = await rig.rename(
            at(PY_SRC, "GLOBAL", line_hint="return GLOBAL" if "reads" in hint else "GLOBAL = 1"), "GLOBAL2"
        )
        assert code(r) == "E_LSP_UNAVAILABLE", hint
    r = await rig.rename(at(PY_SRC, "GLOBAL", line_hint="global GLOBAL"), "GLOBAL2")
    assert code(r) == "E_LSP_UNAVAILABLE"
    assert snapshot(rig.root) == before


async def test_structural_new_name_already_used_is_blocked(rig: Rig):
    rig.put("m.py", PY_SRC)
    before = snapshot(rig.root)
    r = await rig.rename(at(PY_SRC, "order", line_hint="def total"), "extra")
    assert code(r) == "E_MUTATION_BLOCKED"
    assert r.structured["error"]["details"]["reason"] == "new_name_already_in_scope"
    r = await rig.rename(at(PY_SRC, "order", line_hint="def total"), "order")
    assert code(r) == "E_INVALID_ARGUMENT"
    assert snapshot(rig.root) == before
