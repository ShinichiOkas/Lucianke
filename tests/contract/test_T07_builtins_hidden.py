"""T-07 ``query_call_hierarchy`` の outgoing は、言語の組み込み名（``lk://_ext/builtins#`` ・ ``lk://_ext/globals#``）を既定で出さない。

関係抽出（53ce5aa）で ``raise ValueError(...)`` や ``print(...)`` も外部シンボルへの ``CALLS`` になり、outgoing に ``ValueError`` などの
葉が増えた。仕様書 T-07 の ``exclude_external``（既定 ``true``）が真のあいだは組み込み名の葉を出さず、``false`` で出す。標準ライブラリ・
外部パッケージの呼び先（``lk://_ext/decimal#Decimal`` など）は今までどおり ``(external)`` の葉として残る。
実索引（``tests/fixtures/poly`` を ``Indexer`` で取り込んだもの）で確かめる。
"""

from __future__ import annotations

from test_T06_T07 import (  # noqa: F401  フィクスチャ（real_db は module スコープ）と道具を使い回す
    EXT_DECIMAL,
    PC,
    ROUTES,
    U,
    calls,
    real,
    real_db,
)

from lucianke.core.urn import Urn

EXT_VALUE_ERROR = str(Urn.external("builtins", None, "ValueError"))
EXT_PREFIXES = ("lk://_ext/builtins#", "lk://_ext/globals#")


async def test_outgoing_hides_builtin_leaves_by_default(real) -> None:  # noqa: F811
    """process_charge は ``raise ValueError(...)`` を持つが、既定では ``ValueError`` の葉は出ない。"""
    r = await calls(real, PC, "outgoing", depth=1)
    assert not r.is_error, r.text
    nodes = r.structured["data"]["nodes"]
    assert nodes, "ValueError 以外の呼び先（_validate_limit など）は残る"
    assert not [n["urn"] for n in nodes if n["urn"].startswith(EXT_PREFIXES)]
    c = await calls(real, PC, "outgoing", "compact", depth=1)
    assert "ValueError" not in c.text and "(external)" not in c.text.replace("Decimal (external)", "")


async def test_exclude_external_false_shows_builtin_leaves(real) -> None:  # noqa: F811
    r = await calls(real, PC, "outgoing", depth=1, exclude_external=False)
    assert not r.is_error, r.text
    nodes = {n["urn"]: n for n in r.structured["data"]["nodes"]}
    leaf = nodes[EXT_VALUE_ERROR]
    assert leaf["is_external"] is True and leaf["name"] == "ValueError"
    assert not any(n["parent_urn"] == EXT_VALUE_ERROR for n in nodes.values())  # 葉は展開されない
    c = await calls(real, PC, "outgoing", "compact", depth=1, exclude_external=False)
    assert "ValueError (external) @" in c.text


async def test_non_builtin_externals_stay_as_leaves_by_default(real) -> None:  # noqa: F811
    """隠すのは組み込み名だけ。``from decimal import Decimal`` の呼び先は既定でも ``(external)`` の葉。"""
    r = await calls(real, {"urn": U(ROUTES, "create_order")}, "outgoing", depth=3)
    assert not r.is_error, r.text
    nodes = r.structured["data"]["nodes"]
    assert EXT_DECIMAL in {n["urn"] for n in nodes if n["is_external"]}
    assert not [n for n in nodes if n["urn"].startswith(EXT_PREFIXES)]


async def test_hidden_builtins_are_not_counted_in_children_more(real) -> None:  # noqa: F811
    """隠した葉は ``max_children`` の省略件数に数えない（見えない子を ``N more`` と言わない）。"""
    everything = await calls(real, PC, "outgoing", depth=1, exclude_external=False, max_children=500)
    visible = await calls(real, PC, "outgoing", depth=1, max_children=500)
    n_visible = len(visible.structured["data"]["nodes"])
    assert len(everything.structured["data"]["nodes"]) > n_visible
    tight = await calls(real, PC, "outgoing", "compact", depth=1, max_children=n_visible)
    assert "children:" not in tight.text
