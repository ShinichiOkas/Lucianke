"""F-PY（httpx）・F-TS（ky）で、M1 の編集系ツール T-10・T-11・T-13 の事後条件を確かめる契約テスト（BUG-0036、仕様書 §15.2 M1-1）。

ワークスペースは ``bench/.cache`` に取得済みの F-PY・F-TS を ``tmp_path`` に複製したもの。編集は複製に対してだけ行い、原本
（``bench/.cache/``）は書き換えない。試験ごとに新しい複製を作る（戻す手間と、前の編集の持ち越しを避ける）。
結線・LSP の水準・skip の扱いは ``test_fx_read_T00_T08.py`` と同じ（LSP が無い F-TS は降格応答の約束を確かめてから、exact の
事後条件だけを ``_rig.require_exact_lsp`` の事実どおりの理由で skip する。LSP が入っているのに ready でなければ fail）。
"""

from __future__ import annotations

import re
from typing import Any

import tree_sitter_python
import tree_sitter_typescript
from _rig import Rig, patient_lsp, require_exact_lsp, warm_up
from _t11_harness import sha
from _targets import TARGETS, assert_present, target_dict
from test_T10_edit_body import apply_unified_diff  # tests/contract の共通部品（conftest が sys.path に足す）
from tree_sitter import Language, Parser

from lucianke.core.model import Position, RelPath
from lucianke.lsp import features

DELETE_SYM = {"F-PY": ("httpx/_urls.py", "URL.__repr__"), "F-TS": ("source/utils/merge.ts", "mergeHooks")}
CODE_ACTION_FILE = "httpx/_lk_contract_unused.py"
CODE_ACTION_SRC = "import missing_mod_x\nimport sys\n\n\ndef main() -> int:\n    return len(sys.argv)\n"


def parser_for(rig: Rig) -> Parser:
    lang = (
        Language(tree_sitter_python.language())
        if rig.language == "python"
        else Language(tree_sitter_typescript.language_typescript())
    )
    return Parser(lang)


def error_nodes(rig: Rig, data: bytes) -> int:
    count = 0
    stack = [parser_for(rig).parse(data).root_node]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            count += 1
        stack.extend(node.children)
    return count


def eol_and_bom(data: bytes) -> tuple[str, bool]:
    crlf, lf = data.count(b"\r\n"), data.count(b"\n") - data.count(b"\r\n")
    return ("crlf" if crlf > lf else "lf", data.startswith(b"\xef\xbb\xbf"))


async def current_definition(rig: Rig, path: str, sym: str) -> tuple[str, dict[str, Any]]:
    """T-05 で今の定義全体（context なし）を読む。"""
    r = await rig.ok("get_symbol_bodies", targets=[target_dict(path, sym)], context_lines=0)
    item = r["data"]["items"][0]
    assert item["ok"], item
    return item["code"], item


def with_trailing_comment(rig: Rig, code: str) -> str:
    """定義の末尾に 1 行のコメントを足す（構文は壊れない変換）。"""
    lines = code.split("\n")
    if rig.language == "python":
        indent = re.match(r"\s*", lines[-1]).group(0)  # type: ignore[union-attr]
        return "\n".join([*lines, f"{indent}# lk-contract"])
    return "\n".join([*lines[:-1], "\t// lk-contract", lines[-1]])


# --- T-10 -----------------------------------------------------------------------------------------


async def test_t10_postcondition_1_2_3_5_replace_body(rig: Rig) -> None:
    """PC:T-10#1 PC:T-10#2 PC:T-10#3 PC:T-10#5 — sha256_after と diff の再現・構文エラーの部分集合・再解決・改行と BOM の維持。"""
    t = TARGETS[rig.key]
    assert_present(rig.root, t)
    await warm_up(rig, [t.edit_path])
    others = {p: s for p, s in rig.snapshot().items() if p != t.edit_path}
    before = (rig.root / t.edit_path).read_bytes()
    code, _ = await current_definition(rig, t.edit_path, t.edit_sym)
    new_code = with_trailing_comment(rig, code)
    r = await rig.ok(
        "edit_symbol_body", target=target_dict(t.edit_path, t.edit_sym), operation="replace_body", new_code=new_code,
        diagnostics_wait_ms=0,
    )  # fmt: skip
    data = r["data"]
    assert data["applied"] is True and data["syntax_check"] == "ok", data
    after = (rig.root / t.edit_path).read_bytes()
    (change,) = data["files_changed"]
    assert change["path"] == t.edit_path and change["sha256_before"] == sha(before)
    assert change["sha256_after"] == sha(after)  # 事後条件 1
    assert apply_unified_diff(before, data["diff"]) == after
    assert b"lk-contract" in after
    assert error_nodes(rig, after) <= error_nodes(rig, before)  # 事後条件 2
    assert eol_and_bom(after) == eol_and_bom(before)  # 事後条件 5
    assert {
        p: s for p, s in rig.snapshot().items() if p != t.edit_path
    } == others  # 触れたのは 1 ファイルだけ
    # 事後条件 3: resolve_symbols は 1 件に解決し、その範囲は new_range に等しい
    res = await rig.ok("resolve_symbols", targets=[{"urn": data["target_urn"]}])
    item = res["data"]["items"][0]
    assert item["ok"] and item["definition"]["range"] == data["new_range"], (
        item["definition"],
        data["new_range"],
    )


async def test_t10_postcondition_3_5_delete_symbol(rig: Rig) -> None:
    """PC:T-10#3 PC:T-10#5 PC:T-10#1 — delete_symbol のあと対象は E_TARGET_NOT_FOUND になり、改行と BOM は変わらない。"""
    path, sym = DELETE_SYM[rig.key]
    before = (rig.root / path).read_bytes()
    urn = (await current_definition(rig, path, sym))[1]["symbol"]["urn"]
    r = await rig.ok(
        "edit_symbol_body", target=target_dict(path, sym), operation="delete_symbol", diagnostics_wait_ms=0
    )
    data = r["data"]
    assert data["applied"] is True, data
    after = (rig.root / path).read_bytes()
    assert data["files_changed"][0]["sha256_after"] == sha(after)
    assert apply_unified_diff(before, data["diff"]) == after
    assert eol_and_bom(after) == eol_and_bom(before)
    assert error_nodes(rig, after) <= error_nodes(rig, before)
    gone = await rig.call("resolve_symbols", targets=[{"urn": urn}])
    code = (
        gone.structured["error"]["code"]
        if gone.is_error
        else gone.structured["data"]["items"][0]["error"]["code"]
    )
    assert code == "E_TARGET_NOT_FOUND", gone.text[:300]


async def test_t10_postcondition_4_unapplied_edits_change_nothing(rig: Rig) -> None:
    """PC:T-10#4 — dry_run・SHA の不一致・構文を壊す断片・存在しない対象は applied: false で、どのファイルの SHA-256 も変わらない。"""
    t = TARGETS[rig.key]
    before = rig.snapshot()
    code, _ = await current_definition(rig, t.edit_path, t.edit_sym)
    target = target_dict(t.edit_path, t.edit_sym)
    dry = await rig.ok(
        "edit_symbol_body", target=target, operation="replace_body", new_code=with_trailing_comment(rig, code), dry_run=True,
    )  # fmt: skip
    assert dry["data"]["applied"] is False and dry["data"]["diff"]
    assert rig.snapshot() == before
    stale = await rig.call(
        "edit_symbol_body", target=target, operation="replace_body", new_code=code, expected_file_sha256="0" * 64,
    )  # fmt: skip
    assert stale.is_error and stale.structured["error"]["code"] == "E_PRECONDITION_FAILED", stale.text[:300]
    assert rig.snapshot() == before
    broken = await rig.call(
        "edit_symbol_body", target=target, operation="replace_body", new_code=code + "\n)))(((\n"
    )
    assert broken.is_error or broken.structured["data"]["applied"] is False
    assert rig.snapshot() == before
    missing = await rig.call(
        "edit_symbol_body", target=target_dict(t.edit_path, "NoSuchSymbolLk"), operation="replace_body", new_code=code,
    )  # fmt: skip
    assert missing.is_error and missing.structured["error"]["code"] == "E_TARGET_NOT_FOUND"
    assert rig.snapshot() == before


# --- T-11 -----------------------------------------------------------------------------------------


async def symbol_urns(rig: Rig, paths: list[str]) -> set[str]:
    r = await rig.ok("get_file_skeleton", paths=paths, max_tokens=60000)
    out: set[str] = set()

    def walk(symbols: list[dict[str, Any]]) -> None:
        for s in symbols:
            out.add(s["urn"])
            walk(s.get("children") or [])

    for item in r["data"]["items"]:
        walk(item["symbols"])
    return out


async def test_t11_postcondition_1_2_3_rename(rig: Rig) -> None:
    """PC:T-11#1 PC:T-11#2 PC:T-11#3 — files_changed の sha と他ファイルの不変・urn_map・edits_count（LSP が無い F-TS は降格の約束）。"""
    t = TARGETS[rig.key]
    assert_present(rig.root, t)
    await warm_up(rig, [t.rename_path])
    before = rig.snapshot()
    target = target_dict(t.rename_path, t.rename_sym)
    if not rig.lsp_ready:
        # 降格（LSP なし）: 関数・メソッドの改名は字句置換をしない。許可なしの書き込みは applied: false で何も変わらない（事後条件 4）。
        # 許可しても書き込むなら、事後条件 1 は降格の応答でも成り立つ。
        r = await rig.call("rename_symbol", target=target, new_name=t.rename_to, diagnostics_wait_ms=0)
        wrote = (not r.is_error) and r.structured["data"]["applied"] is True
        after = rig.snapshot()
        if not wrote:
            assert after == before, r.text[:400]
        else:
            data = r.structured["data"]
            changed = {c["path"] for c in data["files_changed"]}
            for c in data["files_changed"]:
                assert after[c["path"]] == c["sha256_after"]
            assert {p: s for p, s in after.items() if p not in changed} == {
                p: s for p, s in before.items() if p not in changed
            }
            assert r.structured["_resolution_meta"]["is_degraded"] is True
        require_exact_lsp(rig, "T-11#2 urn_map, T-11#3 edits_count")
    uses = await _lsp_reference_count(rig, t.rename_path, t.rename_sym)
    urns_before = await symbol_urns(rig, sorted({t.rename_path, *_files_with(rig, t.rename_sym)}))
    r = await rig.ok("rename_symbol", target=target, new_name=t.rename_to, diagnostics_wait_ms=0)
    data = r["data"]
    assert data["applied"] is True and data["old_name"] == t.rename_sym and data["new_name"] == t.rename_to, (
        data
    )
    after = rig.snapshot()
    changed = {c["path"] for c in data["files_changed"]}
    assert t.rename_path in changed and len(changed) >= 2
    for c in data["files_changed"]:  # 事後条件 1
        assert after[c["path"]] == c["sha256_after"] and before[c["path"]] == c["sha256_before"]
    assert {p: s for p, s in after.items() if p not in changed} == {
        p: s for p, s in before.items() if p not in changed
    }
    urns_after = await symbol_urns(rig, sorted({t.rename_path, *changed}))
    for old, new in data["urn_map"].items():  # 事後条件 2
        assert old in urns_before and old not in urns_after and new in urns_after, (old, new)
    assert data["urn_map"], data
    assert (
        data["edits_count"] == uses + 1
    )  # 事後条件 3（edits[] は max_tokens で切れうるので件数は edits_count で見る）


def _files_with(rig: Rig, word: str) -> list[str]:
    ext = TARGETS[rig.key].ext
    return sorted(
        p.relative_to(rig.root).as_posix()
        for p in rig.root.rglob(f"*{ext}")
        if re.search(rf"\b{word}\b", p.read_text("utf-8", "ignore"))
    )


async def _lsp_reference_count(rig: Rig, path: str, sym: str) -> int:
    handle = rig.pool.get(rig.language, rig.ws)
    res = await rig.ok("resolve_symbols", targets=[target_dict(path, sym)])
    sel = res["data"]["items"][0]["symbol"]["selection_range"]
    with patient_lsp():  # 真値（BUG-0080: 製品の期限で切れると試験が落ち、LSP の連続時間切れにも数えられる）
        refs = await features.references(
            handle, RelPath(path), Position(sel["line"] - 1, sel["start_col"] - 1), include_declaration=False
        )
    return len(refs)


async def test_t11_postcondition_4_unapplied_renames_change_nothing(rig: Rig) -> None:
    """PC:T-11#4 — dry_run・SHA の不一致・不正な新名・存在しない対象は applied: false で、どのファイルの SHA-256 も変わらない。"""
    t = TARGETS[rig.key]
    await warm_up(rig, [t.rename_path])
    before = rig.snapshot()
    target = target_dict(t.rename_path, t.rename_sym)
    dry = await rig.call(
        "rename_symbol", target=target, new_name=t.rename_to, dry_run=True, diagnostics_wait_ms=0
    )
    if not dry.is_error:
        assert dry.structured["data"]["applied"] is False
    assert rig.snapshot() == before
    stale = await rig.call(
        "rename_symbol", target=target, new_name=t.rename_to, expected_file_sha256s={t.rename_path: "0" * 64}, diagnostics_wait_ms=0,
    )  # fmt: skip
    assert stale.is_error or stale.structured["data"]["applied"] is False
    assert rig.snapshot() == before
    bad = await rig.call("rename_symbol", target=target, new_name="not a valid name!", diagnostics_wait_ms=0)
    assert bad.is_error and bad.structured["error"]["code"] in ("E_INVALID_ARGUMENT", "E_PRECONDITION_FAILED")
    assert rig.snapshot() == before
    missing = await rig.call(
        "rename_symbol", target=target_dict(t.rename_path, "NoSuchSymbolLk"), new_name=t.rename_to
    )
    assert missing.is_error
    # LSP が無い言語は、対象を探す前に「この言語の LSP が無い」で断る（E_UNSUPPORTED）。LSP があれば対象が無いと答える
    expected = "E_TARGET_NOT_FOUND" if rig.lsp_ready else "E_UNSUPPORTED"
    assert missing.structured["error"]["code"] == expected, missing.text[:300]
    assert rig.snapshot() == before


# --- T-13 -----------------------------------------------------------------------------------------


def fixes_of(diags: list[dict[str, Any]], source: str | None = None) -> list[tuple[dict[str, Any], str]]:
    return [
        (d, act["id"])
        for d in diags
        if source is None or d["source"] == source
        for act in d.get("code_actions") or []
    ]


async def test_t13_postcondition_1_2_3_4_code_action(rig: Rig) -> None:
    """PC:T-13#1 PC:T-13#2 PC:T-13#3 PC:T-13#4 — LSP が返す修正の適用（F-PY: ruff の F401）と、LSP が無い F-TS の何も変えない応答。"""
    t = TARGETS[rig.key]
    if rig.language == "python":
        (rig.root / CODE_ACTION_FILE).write_text(CODE_ACTION_SRC, encoding="utf-8")
        path = CODE_ACTION_FILE
        await warm_up(rig, [path])
    else:
        path = t.diag_path
    before = rig.snapshot()
    diag = await rig.ok("get_diagnostics", paths=[path], min_severity="hint", include_code_actions=True)
    fixes = fixes_of(diag["data"]["diagnostics"], "ruff" if rig.language == "python" else None)
    if not rig.lsp_ready:
        # LSP が無い（この環境の F-TS）: 修正は 1 件も出ず、code action は適用できない。応答は applied: false で
        # ワークスペースは変わらない（事後条件 4）。TypeScript の LSP があれば、T-08 が返す quickfix を F-PY と同じ手順で適用する。
        assert not fixes
        r = await rig.call("apply_code_action", path=path, line=1, action_kind_or_id="quickfix")
        assert r.is_error or r.structured["data"]["applied"] is False
        assert rig.snapshot() == before
        require_exact_lsp(rig, "T-13#1 applied, T-13#2 diff, T-13#3 diagnostic resolved")
    f401 = [(d, i) for d, i in fixes if d["code"] == "F401"] or fixes
    assert f401, diag["data"]["diagnostics"]
    await apply_and_check(rig, path, *f401[0])


async def apply_and_check(rig: Rig, path: str, diag: dict[str, Any], action_id: str) -> None:
    line = diag["line"]
    before = rig.snapshot()
    # 事後条件 4: dry_run は applied: false で何も変えない。存在しない ID も何も変えない
    dry = await rig.ok(
        "apply_code_action",
        path=path,
        line=line,
        action_kind_or_id=action_id,
        dry_run=True,
        diagnostics_wait_ms=0,
    )
    assert dry["data"]["applied"] is False and rig.snapshot() == before
    nope = await rig.call(
        "apply_code_action", path=path, line=line, action_kind_or_id="qf_9999", diagnostics_wait_ms=0
    )
    assert nope.is_error and nope.structured["error"]["code"] == "E_TARGET_NOT_FOUND"
    assert rig.snapshot() == before
    old = (rig.root / path).read_bytes()
    r = await rig.ok(
        "apply_code_action", path=path, line=line, action_kind_or_id=action_id, diagnostics_wait_ms=10_000
    )
    data = r["data"]
    assert data["applied"] is True, data
    after = rig.snapshot()
    changed = {c["path"] for c in data["files_changed"]}
    assert path in changed
    for c in data["files_changed"]:  # 事後条件 1
        assert after[c["path"]] == c["sha256_after"]
    assert {p: s for p, s in after.items() if p not in changed} == {
        p: s for p, s in before.items() if p not in changed
    }
    new = (rig.root / path).read_bytes()
    assert new != old and apply_unified_diff(old, data["diff"]) == new  # 事後条件 2
    # 事後条件 3: ID 指定で new_diagnostics が complete のとき、target_diagnostic_resolved は
    # 「その診断が直後の get_diagnostics に現れない」ことと一致する
    assert data["new_diagnostics"]["status"] == "complete", data["new_diagnostics"]
    still = await rig.ok("get_diagnostics", paths=[path], min_severity="hint")
    key = (diag["source"], diag["code"], diag["message"])
    present = any((d["source"], d["code"], d["message"]) == key for d in still["data"]["diagnostics"])
    assert data["target_diagnostic_resolved"] is (not present), (key, data["target_diagnostic_resolved"])
