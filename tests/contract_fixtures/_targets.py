"""F-PY（httpx）・F-TS（ky）で試験が対象にするシンボル・ファイル（固定。``bench/scenarios/f-py.toml``・``f-ts.toml`` と同じ選び方）。

シンボルの有無は ``bench/.cache`` の実物を ``ast`` / 正規表現で確かめる（``assert_present``）ので、フィクスチャの SHA が変わって
対象が消えたら、試験が「対象が無い」と分かる形で落ちる（lucianke の抽出の不具合と取り違えない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Targets:
    skeleton: tuple[str, ...]  # T-02 / T-05 に渡すファイル
    sym_path: str  # 参照・呼び出し階層の対象
    sym: str  # qualified name
    sym_name: str  # 末尾の名前
    callee_path: str  # 呼び出し階層の対象（呼び元がいる）
    callee: str
    callee_name: str
    diag_path: str  # T-08
    edit_path: str  # T-10
    edit_sym: str
    rename_path: str  # T-11
    rename_sym: str
    rename_to: str
    search_query: str  # T-03
    search_exact: str
    ext: str


TARGETS: dict[str, Targets] = {
    "F-PY": Targets(
        skeleton=("httpx/_urls.py", "httpx/_api.py"),
        sym_path="httpx/_urls.py", sym="URL.copy_with", sym_name="copy_with",
        callee_path="httpx/_urls.py", callee="URL.copy_with", callee_name="copy_with",
        diag_path="httpx/_urls.py",
        edit_path="httpx/_urls.py", edit_sym="URL.is_absolute_url",
        rename_path="httpx/_utils.py", rename_sym="primitive_value_to_str", rename_to="primitive_value_to_text",
        search_query="URL", search_exact="URL",
        ext=".py",
    ),
    "F-TS": Targets(
        skeleton=("source/utils/merge.ts", "source/utils/normalize.ts"),
        sym_path="source/utils/merge.ts", sym="validateAndMerge", sym_name="validateAndMerge",
        callee_path="source/utils/merge.ts", callee="validateAndMerge", callee_name="validateAndMerge",
        diag_path="source/utils/merge.ts",
        edit_path="source/utils/merge.ts", edit_sym="mergeHeaders",
        rename_path="source/utils/merge.ts", rename_sym="mergeHeaders", rename_to="mergeHeadersLk",
        search_query="merge", search_exact="mergeHeaders",
        ext=".ts",
    ),
}  # fmt: skip


def target_dict(path: str, sym: str) -> dict[str, str]:
    return {"path": path, "symbol_name": sym}


def assert_present(root: Path, t: Targets) -> None:
    """対象が実物のファイルに定義されていること（フィクスチャのずれを、抽出の不具合と取り違えない）。"""
    for path, name in ((t.sym_path, t.sym_name), (t.callee_path, t.callee_name), (t.edit_path, t.edit_sym.split(".")[-1]),
                       (t.rename_path, t.rename_sym)):  # fmt: skip
        text = (root / path).read_text(encoding="utf-8")
        pat = rf"\b(?:def|class|function|const|let)\s+{re.escape(name)}\b"
        assert re.search(pat, text), f"fixture drift: {name} is not defined in {path}"
