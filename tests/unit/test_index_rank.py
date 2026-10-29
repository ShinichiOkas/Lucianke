"""index.rank のテスト（参照グラフ上の PageRank・fan_in・ファイルのスコア。仕様書 §11 T-01 処理規約 2）。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from lucianke.engine.indexer import Indexer
from lucianke.index import rank
from lucianke.index.connection import ReaderPool, read_data_version
from lucianke.index.writer import IndexWriter
from lucianke.syntax.parsers import TreeCache
from lucianke.workspace.types import Workspace

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS = "poly"
PAY = "src/shop/services/payment.py"
LIMITS = "src/shop/services/limits.py"
PROC = f"lk://{WS}/{PAY}#PaymentProcessor"
CHARGE = f"lk://{WS}/{PAY}#PaymentProcessor.process_charge"


# --- 純粋な PageRank ------------------------------------------------------------------------


def test_pagerank_sums_to_one_and_hub_wins() -> None:
    edges = [("a", "hub"), ("b", "hub"), ("c", "hub"), ("hub", "a")]
    r = rank.pagerank(edges)
    assert sum(r.values()) == pytest.approx(1.0, abs=1e-9)
    assert max(r, key=lambda k: (r[k], k)) == "hub"
    assert r["a"] > r["b"] == r["c"]  # hub が返す先の a だけが多い


def test_pagerank_matches_a_hand_computed_fixed_point() -> None:
    """a -> b, b -> a, c -> a。c は誰からも参照されないので teleport 分 (1-d)/3 だけ。残りを a と b が分け合う。"""
    d = rank.DAMPING
    r = rank.pagerank([("a", "b"), ("b", "a"), ("c", "a")])
    assert r["c"] == pytest.approx((1 - d) / 3, abs=1e-9)
    # a = (1-d)/3 + d*(b + c)、b = (1-d)/3 + d*a/1（a の出先は b だけ）
    assert r["b"] == pytest.approx((1 - d) / 3 + d * r["a"], abs=1e-8)
    assert r["a"] == pytest.approx((1 - d) / 3 + d * (r["b"] + r["c"]), abs=1e-8)
    assert sum(r.values()) == pytest.approx(1.0, abs=1e-9)


def test_pagerank_is_deterministic_and_order_independent() -> None:
    edges = [(f"n{i}", f"n{(i * 7 + 3) % 40}") for i in range(200)] + [("n1", "n1")]
    forward = rank.pagerank(edges)
    again = rank.pagerank(edges)
    shuffled = rank.pagerank(list(reversed(edges)) + edges[:10])  # 順序と重複が違っても同じ
    assert forward == again == shuffled


def test_pagerank_drops_self_loops_and_handles_empty() -> None:
    assert rank.pagerank([]) == {}
    r = rank.pagerank([("a", "a")], nodes=["a", "b"])
    assert r == {"a": pytest.approx(0.5), "b": pytest.approx(0.5)}


def test_pagerank_ties_are_exactly_equal() -> None:
    r = rank.pagerank([("x", "t1"), ("x", "t2")])
    assert r["t1"] == r["t2"]


def test_pagerank_is_fast_enough_for_ten_thousand_files() -> None:
    """1 万ファイル・10 万シンボル・40 万本の辺を数秒で（揺れないよう、落とす上限は余裕を見て 30 秒）。"""
    n_files, per_file = 10_000, 10
    nodes = [f"lk://w/f{i}.py#s{j}" for i in range(n_files) for j in range(per_file)]
    edges = [
        (src, nodes[(k * 31 + step * 977) % len(nodes)])
        for k, src in enumerate(nodes)
        for step in (1, 7, 31, 101)
    ]
    started = time.perf_counter()
    r = rank.pagerank(edges, nodes)
    elapsed = time.perf_counter() - started
    assert len(r) == len(nodes) and sum(r.values()) == pytest.approx(1.0, abs=1e-6)
    assert elapsed < 30, f"pagerank took {elapsed:.1f}s"


# --- 索引した F-POLY の上で ---------------------------------------------------------------


@dataclass
class Env:
    root: Path
    indexer: Indexer
    writer: IndexWriter
    readers: ReaderPool


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "poly"
    shutil.copytree(POLY, root)
    db = tmp_path / "db" / "paad.sqlite"
    writer = IndexWriter(db)
    readers = ReaderPool(db, size=2)
    ws = Workspace(workspace_id=WS, root=root.resolve())
    indexer = Indexer(ws, writer, readers, TreeCache())
    await indexer.scan()
    yield Env(root, indexer, writer, readers)
    await indexer.aclose()
    readers.close()
    writer.close()


async def _store(env: Env, **kw: int) -> rank.RankResult:
    with env.readers.reader() as conn:
        result = rank.compute_ranks(conn, WS)
    await env.writer.asubmit(
        lambda c: rank.store_ranks(c, WS, result, **kw), bumps_version=False, label="rank"
    )
    return result


async def test_graph_has_only_workspace_symbols_as_targets(env: Env) -> None:
    with env.readers.reader() as conn:
        graph = rank.load_graph(conn, WS)
    members = set(graph.symbols)
    assert all(not u.startswith("lk://_ext/") for u in graph.symbols)
    assert graph.edges and all(
        t in members for _, t, _ in graph.edges
    )  # 外部（Decimal・Exception）は辺にならない
    assert all(a != b for a, b, _ in graph.edges)
    assert len({(a, b) for a, b, _ in graph.edges}) == len(graph.edges)  # 組は 1 本にまとまっている


async def test_scores_are_normalized_and_referenced_symbols_rank_higher(env: Env) -> None:
    result = await _store(env)
    assert result.edge_count > 0
    assert max(result.scores.values()) == 1.0 and all(0.0 <= v <= 1.0 for v in result.scores.values())
    # PaymentProcessor は routes・tests・app から参照される。誰からも参照されない retry_order より上
    assert result.scores[PROC] > result.scores[f"lk://{WS}/src/shop/api/routes.py#retry_order"]
    top = max(result.scores, key=lambda u: (result.scores[u], u))
    assert top.startswith(f"lk://{WS}/src/shop/")  # テストや Tier 4 ではなく、本体のコード


async def test_fan_in_counts_edges_from_other_files(env: Env) -> None:
    result = await _store(env)
    # process_charge: routes.py の create_order / retry_order と tests の test_charge_ok から CALLS
    assert result.fan_in[CHARGE] == 3
    # 同じファイルの中だけから参照されるシンボル（retry <-> schedule）は fan_in に数えない
    assert result.fan_in.get(f"lk://{WS}/{LIMITS}#retry", 0) == 0


async def test_store_writes_symbol_rank_and_file_scores(env: Env) -> None:
    result = await _store(env)
    with env.readers.reader() as conn:
        n = conn.execute("SELECT COUNT(*) FROM _symbol_rank WHERE workspace_id = ?", (WS,)).fetchone()[0]
        scores = dict(conn.execute("SELECT path, pagerank_score FROM files WHERE workspace_id = ?", (WS,)))
        assert rank.ranked_symbol_count(conn, WS) == n == len(result.scores)
    assert max(scores.values()) == 1.0  # 最大のファイルが 1.0
    assert scores[PAY] > scores["README.md"] == 0.0  # シンボルの無いファイルは 0（NULL ではない）
    assert all(v is not None for v in scores.values())


async def test_store_is_replaceable_and_idempotent(env: Env) -> None:
    await _store(env)
    with env.readers.reader() as conn:
        first = conn.execute("SELECT urn, score, fan_in FROM _symbol_rank ORDER BY urn").fetchall()
        files_first = conn.execute("SELECT path, pagerank_score FROM files ORDER BY path").fetchall()
    await _store(env)
    with env.readers.reader() as conn:
        assert conn.execute("SELECT urn, score, fan_in FROM _symbol_rank ORDER BY urn").fetchall() == first
        assert conn.execute("SELECT path, pagerank_score FROM files ORDER BY path").fetchall() == files_first


def _eligible_scores(env: Env, result: rank.RankResult, path: str) -> list[float]:
    """ファイルの代表になれるシンボル（priv / prot / Field / Property を除く）のスコア（降順）。"""
    with env.readers.reader() as conn:
        rows = conn.execute(
            "SELECT s.urn, s.modifiers_json, s.kind FROM symbols s JOIN files f ON f.file_id = s.file_id"
            " WHERE f.path = ?",
            (path,),
        ).fetchall()
    out = [
        result.scores[u]
        for u, mods, kind in rows
        if u in result.scores
        and not ({"priv", "prot"} & set(json.loads(mods)))
        and kind not in rank.MINOR_KINDS
    ]
    return sorted(out, reverse=True)


async def test_file_score_is_sum_of_top_n_public_symbols(env: Env) -> None:
    result = await _store(env)
    eligible = _eligible_scores(env, result, PAY)
    assert len(eligible) >= 3
    with env.readers.reader() as conn:
        by_n = {
            n: {f.path: f for f in rank.repo_map_files(conn, WS, per_file=n)}[PAY].score for n in (1, 2, 50)
        }
    assert by_n[1] == pytest.approx(eligible[0], abs=1e-9)
    assert by_n[2] == pytest.approx(sum(eligible[:2]), abs=1e-9)
    assert by_n[50] == pytest.approx(sum(eligible), abs=1e-9)


async def test_private_symbols_do_not_count_for_the_file_score(env: Env) -> None:
    result = await _store(env)
    with env.readers.reader() as conn:
        score = {f.path: f for f in rank.repo_map_files(conn, WS, per_file=50)}[PAY].score
    everything = sum(v for u, v in result.scores.items() if u.startswith(f"lk://{WS}/{PAY}#"))
    assert score < everything  # priv の _validate_limit と Field を除いている


async def test_stored_file_score_uses_the_default_five_and_is_normalized(env: Env) -> None:
    await _store(env)
    with env.readers.reader() as conn:
        stored = dict(conn.execute("SELECT path, pagerank_score FROM files WHERE workspace_id = ?", (WS,)))
        raw = {f.path: f.score for f in rank.repo_map_files(conn, WS, per_file=rank.DEFAULT_PER_FILE)}
    top = max(raw.values())
    for path, value in stored.items():
        assert value == pytest.approx(raw[path] / top, abs=1e-9)


async def test_repo_map_files_without_ranks_returns_zero_scores(env: Env) -> None:
    await env.writer.asubmit(
        lambda c: rank.store_ranks(c, WS, rank.RankResult({}, {}, 0)), bumps_version=False, label="rank"
    )
    with env.readers.reader() as conn:
        files = rank.repo_map_files(conn, WS)
    assert files and all(f.score == 0.0 and f.fan_in == 0 for f in files)
    assert [f.path for f in files] == sorted(f.path for f in files)


async def test_repo_map_files_can_skip_test_files(env: Env) -> None:
    with env.readers.reader() as conn:
        with_tests = {f.path for f in rank.repo_map_files(conn, WS)}
        without = {f.path for f in rank.repo_map_files(conn, WS, include_tests=False)}
    assert "tests/test_payment.py" in with_tests and "tests/test_payment.py" not in without
    assert without < with_tests


async def test_no_edges_clears_ranks(env: Env) -> None:
    await _store(env)
    await env.writer.asubmit(
        lambda c: rank.store_ranks(c, WS, rank.RankResult({}, {}, 0)), bumps_version=False, label="rank"
    )
    with env.readers.reader() as conn:
        assert rank.ranked_symbol_count(conn, WS) == 0
        assert conn.execute("SELECT COUNT(*) FROM files WHERE pagerank_score IS NOT NULL").fetchone()[0] == 0


def test_store_requires_a_transaction() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(RuntimeError):
        rank.store_ranks(conn, WS, rank.RankResult({}, {}, 0))


# --- Indexer の結線（scan の最後に 1 回） ---------------------------------------------------------


async def test_scan_writes_the_ranks_without_a_separate_call(env: Env) -> None:
    """env は scan しただけ（``_store`` を呼んでいない）。それでもシンボルの重要度とファイルのスコアが入っている。"""
    with env.readers.reader() as conn:
        assert rank.ranked_symbol_count(conn, WS) > 0
        assert conn.execute("SELECT MAX(pagerank_score) FROM files").fetchone()[0] == 1.0


async def test_refresh_ranks_does_not_bump_data_version(env: Env) -> None:
    with env.readers.reader() as conn:
        before = read_data_version(conn)
        first = conn.execute("SELECT urn, score, fan_in FROM _symbol_rank ORDER BY urn").fetchall()
    assert await env.indexer.refresh_ranks() == len(first)
    with env.readers.reader() as conn:
        assert read_data_version(conn) == before
        assert conn.execute("SELECT urn, score, fan_in FROM _symbol_rank ORDER BY urn").fetchall() == first


async def test_scan_after_a_change_recomputes_the_ranks(env: Env) -> None:
    with env.readers.reader() as conn:
        before = dict(conn.execute("SELECT urn, fan_in FROM _symbol_rank"))
    (env.root / "src/shop/extra.py").write_text(
        "from shop.services.payment import PaymentProcessor\n\n\ndef build(limits):\n    return PaymentProcessor(limits)\n",
        encoding="utf-8",
    )
    report = await env.indexer.scan()
    assert report.indexed == 1 and not report.errors
    with env.readers.reader() as conn:
        after = dict(conn.execute("SELECT urn, fan_in FROM _symbol_rank"))
    assert f"lk://{WS}/src/shop/extra.py#build" in after
    assert (
        after[PROC] == before[PROC] + 2
    )  # 別ファイルからの辺が 2 本増えた（import 文の IMPORTS と build の CALLS）


async def test_scan_without_changes_keeps_the_ranks(env: Env) -> None:
    with env.readers.reader() as conn:
        first = conn.execute("SELECT urn, score, fan_in FROM _symbol_rank ORDER BY urn").fetchall()
    report = await env.indexer.scan()
    assert report.indexed == 0
    with env.readers.reader() as conn:
        assert conn.execute("SELECT urn, score, fan_in FROM _symbol_rank ORDER BY urn").fetchall() == first
