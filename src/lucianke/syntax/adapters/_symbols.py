"""Tier 3 データファイルの「索引用のシンボル」（骨格の最上位の節だけ。設計書 §7.6.2、仕様書 §8.6 B4）。

骨格（``build_record`` の ``data["symbols"]``）の最上位のノード（キー / タグ）を ``SymbolRecord`` にする。
JSON / YAML / TOML は最上位のキー（TOML は暗黙の表の連なり ``tool.ruff`` を 1 つにまとめた骨格のノード）、XML は根要素とその直下の子要素（根だけでは名前検索の手がかりにならないため 2 階層）。
JSONL と、根が配列・スカラーのファイルはシンボルを持たない。

URN は ``lk://<ws>/<path>#jsonpath:$.service`` / ``#yaml:retry`` / ``#toml:project`` / ``#xpath:/testsuites/testsuite``。
セレクタの式は同じアダプタの ``select`` が解釈できる形で作る（式にできない名前 = 引用符を含むキーなど は落とす）。
TOML は、骨格のノードの名前が暗黙の表の連なり（``tool.ruff``）か 1 つの引用符つきキー（``"a.b"``）かを区別できないので、アダプタが
ノードの「素性」（キーの部品 ``parts``。``SkelNode.parts``）を持ち、式はその部品から作る（``tool.ruff`` と ``"a.b"``）。
同じ URN になるノードは先頭の 1 つだけ残す（``symbols.urn`` は主キー）。1 ファイルあたりの上限は ``MAX_INDEX_SYMBOLS``、
対象のファイルの大きさの上限は ``MAX_SYMBOL_SOURCE_BYTES``。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from lucianke.core.model import Confidence, Kind, Range, RelPath, SymbolRecord
from lucianke.core.records import Record
from lucianke.core.urn import DATA_KINDS, DataKind, DataSel, Urn

__all__ = ["MAX_INDEX_SYMBOLS", "MAX_SYMBOL_SOURCE_BYTES", "data_symbols", "items_symbols", "selector_expr"]

MAX_INDEX_SYMBOLS = (
    500  # 1 ファイルが索引に出すシンボルの上限（巨大な i18n ファイルなどで symbols を膨らませない）
)

# これを超えるファイルはシンボルを出さない（``files`` 行だけ）。アダプタの位置つきパースは Tree-sitter より桁違いに遅く
# （JSON で約 0.6 MB/s、YAML はさらに遅い）、大きなデータファイル（ロックファイル・データのダンプ）の最上位キーは手がかりにもなりにくい。
MAX_SYMBOL_SOURCE_BYTES = 128 * 1024

_BARE_KEY = re.compile(r"[A-Za-z0-9_\-]+")  # toml: / yaml: の経路でそのまま書ける名前
_BARE_JSON = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # jsonpath の `.name`
_BARE_TAG = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")  # xpath の要素名（名前空間の接頭辞は式にできない）
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def selector_expr(scheme: str, path: Sequence[str]) -> str | None:
    """経路 ``path``（最上位から。キー 1 つが 1 要素）を ``scheme`` のセレクタの式にする。式にできない名前を含むときは ``None``。"""
    if not path or any(not p or _CONTROL.search(p) for p in path):
        return None
    if scheme == "jsonpath":
        out = "$"
        for p in path:
            if _BARE_JSON.fullmatch(p):
                out += f".{p}"
            elif '"' in p or "\\" in p or p == "*":  # `*` は引用符つきでも全要素になる
                return None
            else:
                out += f'."{p}"'
        return out
    if scheme in ("yaml", "toml"):
        # どちらも ``parse_key_path`` の経路。名前に ``.`` などを含むとき（TOML の ``"a.b" = 1`` を含む）は引用符で囲み、暗黙の表 ``a.b`` と区別する
        parts: list[str] = []
        for p in path:
            if _BARE_KEY.fullmatch(p):
                parts.append(p)
            elif '"' not in p:
                parts.append(f'"{p}"')
            elif "'" not in p:
                parts.append(f"'{p}'")
            else:
                return None
        return ".".join(parts)
    if scheme == "xpath":
        if not all(_BARE_TAG.fullmatch(p) for p in path):
            return None
        return "/" + "/".join(path)
    return None


def data_symbols(
    record: Record,
    *,
    scheme: str,
    kind: Kind,
    rel: str,
    workspace_id: str,
    depth: int = 1,
    limit: int = MAX_INDEX_SYMBOLS,
) -> list[SymbolRecord]:
    """骨格のレコード ``record`` の ``data["symbols"]`` から、``depth`` 階層までのシンボルを作る。"""
    return items_symbols(
        record.data["symbols"],
        scheme=scheme,
        kind=kind,
        rel=rel,
        workspace_id=workspace_id,
        depth=depth,
        limit=limit,
    )


def items_symbols(
    items: Sequence[Mapping[str, Any]],
    *,
    scheme: str,
    kind: Kind,
    rel: str,
    workspace_id: str,
    depth: int = 1,
    limit: int = MAX_INDEX_SYMBOLS,
) -> list[SymbolRecord]:
    """骨格の項目（``name`` / ``start_line`` / ``end_line`` / ``children``、あれば ``parts``）から、``depth`` 階層までのシンボルを作る。

    ``parts`` はその項目の名前を作るキーの部品（無ければ ``[name]``）。式は祖先の部品と合わせた経路から作る。
    """
    if scheme not in DATA_KINDS:
        return []
    data_kind: DataKind = scheme  # type: ignore[assignment]  # 上で DATA_KINDS に含まれることを確かめた
    out: list[SymbolRecord] = []
    seen: set[str] = set()

    def visit(
        level_items: Sequence[Mapping[str, Any]], names: tuple[str, ...], keys: tuple[str, ...]
    ) -> None:
        for it in level_items:
            if len(out) >= limit:
                return
            name = str(it["name"])
            here = (*names, name)
            if not names and name == "$":  # 根が配列・スカラー（JSONL の全レコードも）。キーではない
                continue
            here_keys = (*keys, *(it.get("parts") or (name,)))
            expr = selector_expr(scheme, here_keys)
            if expr is None:
                continue
            urn = Urn(workspace_id, rel, DataSel(data_kind, expr))
            if str(urn) in seen:
                continue
            seen.add(str(urn))
            out.append(
                SymbolRecord(
                    urn=urn,
                    path=RelPath(rel),
                    name=name,
                    qualified_name=".".join(here),
                    kind=kind,
                    range=Range.from_line_span_external(int(it["start_line"]), int(it["end_line"])),
                    confidence=Confidence.STRUCTURAL,
                )
            )
            if len(here) < depth:
                visit(it.get("children", ()), here, here_keys)

    visit(items, (), ())
    return out
