"""参照グラフ上の重要度（PageRank）と fan_in（仕様書 §11 T-01 処理規約 2、設計書 §7.6.3 の ``repo_map``）。

定義（ここで固定する。T-01 と T-03 の並びはこの値に従う）:

* **グラフ**: ``relations`` のうち ``relation_kind`` が ``CALLS`` / ``REFERENCES`` / ``IMPORTS`` / ``INHERITS`` /
  ``IMPLEMENTS`` の行。**ワークスペース内のシンボル**（``symbols.is_external = 0``）を指すものだけを辺にする
  （``lk://_ext/`` の外部シンボルは辺にしない。``Decimal`` や ``Exception`` が上位を占めないため）。
  始点は参照箇所を含むシンボル、またはモジュール直下のコード（セレクタの無い URN。ファイルの代役の頂点として残る）。
  自己ループは捨て、``(始点, 終点)`` の組は種別・参照箇所の数に関わらず 1 本（重み 1）にまとめる。
  テストファイルからの辺も数える（索引上の重要度は ``include_tests`` に依らない。T-01 が表示から外すだけ）。
* **PageRank**: 減衰係数 ``DAMPING``（0.85）、出次数 0 の頂点の質量は全頂点に均等配分、初期値は一様、
  反復は最大 ``MAX_ITERATIONS`` 回で L1 差が ``TOLERANCE`` 未満なら止める。頂点は URN の辞書順に並べ、
  辺も整列してから足すので、同じ入力なら浮動小数の加算順まで同じ（決定的）。numpy は使わない。
* **シンボルのスコア**: ワークスペース内のシンボルの PageRank を、その最大値で割った値（最大 1.0。小数 12 桁に丸める）。
  ファイルの代役の頂点は結果に出さない。
* **fan_in（シンボル）**: そのシンボルを指す辺のうち、始点が **別のファイル** にあるものの数（上の整理後の本数）。
  ファイルの fan_in はファイル内の全シンボルの fan_in の和。
* **ファイルのスコア**: 公開シンボル（``PUBLIC_SYMBOL_SQL``）のスコアの上位 ``max_symbols_per_file`` 件の和。
  ``files.pagerank_score`` には既定の ``DEFAULT_PER_FILE``（5）件の和を、最大のファイルを 1.0 とした値で書く。
  ``max_symbols_per_file`` は T-01 の要求パラメータなので、T-01 は ``_symbol_rank`` から要求の件数で計算し直す。
* **辺が 1 本も無い**（索引前・参照が無い）ときは何も書かず、``files.pagerank_score`` と ``_symbol_rank`` を空にする。
  T-01 はこれを「グラフ未準備」として ``alpha`` に降格する。

書き込み（``store_ranks``）は書き手のトランザクションの中で呼ぶ。計算（``compute_ranks``）は読み取り接続で行えるので、
書き手を数秒止めずに済む（``engine.indexer.Indexer.refresh_ranks`` がその順に呼ぶ）。``data_version`` は進めない
（``files.pagerank_score`` と ``_symbol_rank`` は並び順の付加情報で、カーソルが指す断面を変えるのは再計算の直後だけ）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from lucianke.core.foreground import cooperate

__all__ = [
    "DAMPING",
    "DEFAULT_PER_FILE",
    "EDGE_KINDS",
    "MAX_ITERATIONS",
    "MINOR_KINDS",
    "PUBLIC_SYMBOL_SQL",
    "TOLERANCE",
    "Graph",
    "RankResult",
    "RepoMapFile",
    "compute_ranks",
    "file_symbol_ranks",
    "load_graph",
    "pagerank",
    "rank_graph",
    "ranked_symbol_count",
    "repo_map_files",
    "store_ranks",
]

EDGE_KINDS: Final = ("CALLS", "REFERENCES", "IMPORTS", "INHERITS", "IMPLEMENTS")
DAMPING: Final = 0.85
MAX_ITERATIONS: Final = 200
TOLERANCE: Final = 1e-10
DEFAULT_PER_FILE: Final = 5
SCORE_DIGITS: Final = 12

MINOR_KINDS: Final = ("Field", "Property", "Column", "Cell", "Key", "Tag")
"""ファイルの代表に選ばない種別（クラスやテーブルの中身は親が代表になる。データ形式の最上位の節 ``Key`` / ``Tag`` は構造の地図に出さない）。"""

PUBLIC_SYMBOL_SQL: Final = (
    "s.is_external = 0 AND s.modifiers_json NOT LIKE '%\"priv\"%' AND s.modifiers_json NOT LIKE '%\"prot\"%'"
    " AND s.kind NOT IN (" + ", ".join(f"'{k}'" for k in MINOR_KINDS) + ")"
)
"""``symbols`` のエイリアス ``s`` に対する「ファイルの代表になれる公開シンボル」の条件。
修飾子が空（ctags 由来など可視性の分からないもの）は公開として扱う。"""


# --- 純粋な計算 -------------------------------------------------------------------------


def pagerank(
    edges: Iterable[tuple[str, str]],
    nodes: Iterable[str] = (),
    *,
    damping: float = DAMPING,
    max_iterations: int = MAX_ITERATIONS,
    tolerance: float = TOLERANCE,
) -> dict[str, float]:
    """有向グラフの PageRank（合計 1.0）。同じ入力なら同じ出力（辺の重複・入力の順序に依らない）。

    ``edges`` は ``(始点, 終点)``。重複と自己ループは捨てる。``nodes`` は辺に現れない頂点を足すときに使う。
    """
    unique = sorted({(a, b) for a, b in edges if a != b})
    names = sorted({n for pair in unique for n in pair} | set(nodes))
    n = len(names)
    if n == 0:
        return {}
    index = {name: i for i, name in enumerate(names)}
    out: list[list[int]] = [[] for _ in range(n)]
    for a, b in unique:
        out[index[a]].append(index[b])
    sources = [i for i in range(n) if out[i]]
    dangling = [i for i in range(n) if not out[i]]
    inv = [1.0 / len(out[i]) if out[i] else 0.0 for i in range(n)]
    rank = [1.0 / n] * n
    teleport = (1.0 - damping) / n
    for _ in range(max_iterations):
        cooperate()  # 背景の計算なら、反復ごとに前景のツールの呼び出しに GIL を譲る（BUG-0112）
        base = teleport + damping * sum(rank[i] for i in dangling) / n
        nxt = [base] * n
        for i in sources:
            share = damping * rank[i] * inv[i]
            for j in out[i]:
                nxt[j] += share
        delta = sum(abs(nxt[i] - rank[i]) for i in range(n))
        rank = nxt
        if delta < tolerance:
            break
    return dict(zip(names, rank, strict=True))


# --- 読み取り ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Graph:
    """``load_graph`` の結果。``edges`` は ``(始点 URN, 終点 URN, 別ファイルからなら 1)`` で整理済み。"""

    symbols: tuple[str, ...]
    edges: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True, slots=True)
class RankResult:
    """``compute_ranks`` の結果。``scores`` と ``fan_in`` はワークスペース内のシンボルの URN で引く。"""

    scores: dict[str, float]
    fan_in: dict[str, int]
    edge_count: int


def load_graph(conn: sqlite3.Connection, workspace_id: str) -> Graph:
    """ワークスペースの参照グラフを読む（読み取りだけ）。辺の整理は SQL の ``GROUP BY`` で行う。"""
    symbols = tuple(
        r[0]
        for r in conn.execute(
            "SELECT s.urn FROM symbols s JOIN files f ON f.file_id = s.file_id"
            " WHERE f.workspace_id = ? AND s.is_external = 0 ORDER BY s.urn",
            (workspace_id,),
        )
    )
    kinds = ", ".join("?" for _ in EDGE_KINDS)
    rows = conn.execute(
        "SELECT r.source_urn, r.target_urn, MAX(r.source_file_id <> t.file_id)"
        " FROM relations r"
        " JOIN symbols t ON t.urn = r.target_urn AND t.is_external = 0"
        " JOIN files sf ON sf.file_id = r.source_file_id"
        f" WHERE sf.workspace_id = ? AND r.relation_kind IN ({kinds}) AND r.source_urn <> r.target_urn"
        " GROUP BY r.source_urn, r.target_urn ORDER BY r.source_urn, r.target_urn",
        (workspace_id, *EDGE_KINDS),
    ).fetchall()
    return Graph(symbols, tuple((str(a), str(b), int(c or 0)) for a, b, c in rows))


def compute_ranks(conn: sqlite3.Connection, workspace_id: str) -> RankResult:
    """ワークスペースの全シンボルの重要度と fan_in を計算する（読み取りだけ。書き込みは ``store_ranks``）。"""
    graph = load_graph(conn, workspace_id)
    return rank_graph(graph)


def rank_graph(graph: Graph) -> RankResult:
    """``Graph`` から ``RankResult`` を作る（DB に触れない部分。テストと ``compute_ranks`` が使う）。"""
    if not graph.edges:
        return RankResult({}, {}, 0)
    raw = pagerank(((a, b) for a, b, _ in graph.edges), graph.symbols)
    members = set(graph.symbols)
    top = max((raw[u] for u in graph.symbols), default=0.0)
    scale = 1.0 / top if top > 0 else 0.0
    scores = {u: round(raw[u] * scale, SCORE_DIGITS) for u in graph.symbols}
    fan_in: dict[str, int] = {}
    for _, target, cross in graph.edges:
        if cross and target in members:
            fan_in[target] = fan_in.get(target, 0) + 1
    return RankResult(scores, fan_in, len(graph.edges))


# --- 書き込み ---------------------------------------------------------------------------


def store_ranks(
    conn: sqlite3.Connection,
    workspace_id: str,
    result: RankResult,
    *,
    per_file: int = DEFAULT_PER_FILE,
) -> int:
    """``_symbol_rank`` と ``files.pagerank_score`` を置き換える（書き手のトランザクションの中で呼ぶ）。

    返り値は書いたシンボルの行数。辺が無い結果（``edge_count == 0``）は両方を空（``NULL``）にする。
    計算のあいだに消えたシンボルは読み飛ばす（次の再計算で揃う）。
    """
    if not conn.in_transaction:
        raise RuntimeError("store_ranks must run inside the writer's transaction")
    conn.execute("DELETE FROM _symbol_rank WHERE workspace_id = ?", (workspace_id,))
    if result.edge_count == 0:
        conn.execute("UPDATE files SET pagerank_score = NULL WHERE workspace_id = ?", (workspace_id,))
        return 0
    rows = [(u, workspace_id, s, result.fan_in.get(u, 0)) for u, s in sorted(result.scores.items())]
    conn.executemany(
        "INSERT OR REPLACE INTO _symbol_rank(urn, workspace_id, score, fan_in) VALUES (?, ?, ?, ?)", rows
    )
    conn.execute(
        "UPDATE files SET pagerank_score = COALESCE((SELECT SUM(top.score) FROM ("
        " SELECT r.score AS score FROM _symbol_rank r JOIN symbols s ON s.urn = r.urn"
        f" WHERE s.file_id = files.file_id AND {PUBLIC_SYMBOL_SQL}"
        " ORDER BY r.score DESC, s.start_line, s.urn LIMIT ?) AS top), 0.0)"
        " WHERE workspace_id = ?",
        (per_file, workspace_id),
    )
    top_row = conn.execute(
        "SELECT MAX(pagerank_score) FROM files WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    top = float(top_row[0] or 0.0)
    if top > 0:
        conn.execute(
            "UPDATE files SET pagerank_score = ROUND(pagerank_score / ?, ?) WHERE workspace_id = ?",
            (top, SCORE_DIGITS, workspace_id),
        )
    return len(rows)


# --- 読み取りクエリ（T-01 get_repo_map） ------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoMapFile:
    """T-01 が並べるファイル 1 つ分。``score`` は公開シンボルの上位 N 件のスコアの和、``fan_in_top`` は上位 N 件の fan_in の和
    （N は要求の ``max_symbols_per_file``）、``fan_in`` はファイルの全シンボルの fan_in の和（表示用）。"""

    path: str
    language: str
    tier: int
    line_count: int
    confidence: str
    is_test: bool
    score: float
    fan_in_top: int
    fan_in: int


def ranked_symbol_count(conn: sqlite3.Connection, workspace_id: str) -> int:
    """``_symbol_rank`` の行数（0 ならグラフ未準備）。表の無い古い DB でも 0。"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM _symbol_rank WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0])


def repo_map_files(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    per_file: int = DEFAULT_PER_FILE,
    include_tests: bool = True,
) -> list[RepoMapFile]:
    """ワークスペースの全ファイルと、T-01 の順位付けに使う値（パス順）。``_symbol_rank`` が空なら 0。"""
    test_clause = "" if include_tests else " AND f.is_test = 0"
    sql = (
        "WITH sr AS ("
        " SELECT s.file_id AS file_id, r.score AS score, r.fan_in AS fan_in,"
        " ROW_NUMBER() OVER (PARTITION BY s.file_id ORDER BY r.score DESC, s.start_line, s.urn) AS by_score,"
        " ROW_NUMBER() OVER (PARTITION BY s.file_id ORDER BY r.fan_in DESC, r.score DESC, s.start_line, s.urn)"
        " AS by_fan_in"
        " FROM _symbol_rank r JOIN symbols s ON s.urn = r.urn"
        f" WHERE r.workspace_id = :ws AND {PUBLIC_SYMBOL_SQL}),"
        " agg AS (SELECT file_id,"
        " SUM(CASE WHEN by_score <= :n THEN score END) AS score_top,"
        " SUM(CASE WHEN by_fan_in <= :n THEN fan_in END) AS fan_in_top FROM sr GROUP BY file_id),"
        " fin AS (SELECT s2.file_id AS file_id, SUM(r2.fan_in) AS fan_in FROM _symbol_rank r2"
        " JOIN symbols s2 ON s2.urn = r2.urn WHERE r2.workspace_id = :ws GROUP BY s2.file_id)"
        " SELECT f.path, f.language, f.tier, f.line_count, f.confidence_level, f.is_test,"
        " COALESCE(agg.score_top, 0.0), COALESCE(agg.fan_in_top, 0), COALESCE(fin.fan_in, 0)"
        " FROM files f LEFT JOIN agg ON agg.file_id = f.file_id LEFT JOIN fin ON fin.file_id = f.file_id"
        f" WHERE f.workspace_id = :ws{test_clause} ORDER BY f.path"
    )
    params = {"ws": workspace_id, "n": max(1, int(per_file))}
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:  # _symbol_rank の無い古い DB: スコアなしで返す
        rows = conn.execute(
            "SELECT f.path, f.language, f.tier, f.line_count, f.confidence_level, f.is_test, 0.0, 0, 0"
            f" FROM files f WHERE f.workspace_id = ?{test_clause} ORDER BY f.path",
            (workspace_id,),
        ).fetchall()
    return [
        RepoMapFile(str(p), str(lang), int(t), int(lc), str(conf), bool(it), float(sc), int(fit), int(fi))
        for p, lang, t, lc, conf, it, sc, fit, fi in rows
    ]


def file_symbol_ranks(conn: sqlite3.Connection, workspace_id: str, path: str) -> dict[str, tuple[float, int]]:
    """ファイルのシンボルの ``{urn: (score, fan_in)}``（``_symbol_rank`` に無いものは含まない）。

    ファイル → そのシンボル → ``_symbol_rank`` の順に引く（``CROSS JOIN`` で順序を固定）。以前は SQLite が ``_symbol_rank`` を
    総なめにしてから結んでいて、1 回 0.15〜0.2 秒、T-01 はこれを表示するファイルごとに呼ぶので 4,221 ファイルの索引で
    ``max_tokens=5000`` の地図の 11.7 秒のうち 9.8 秒を使っていた（BUG-0120）。
    """
    try:
        rows = conn.execute(
            "SELECT r.urn, r.score, r.fan_in FROM files f CROSS JOIN symbols s ON s.file_id = f.file_id"
            " CROSS JOIN _symbol_rank r ON r.urn = s.urn"
            " WHERE f.workspace_id = ? AND f.path = ? AND r.workspace_id = ?",
            (workspace_id, path, workspace_id),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(u): (float(sc), int(fi)) for u, sc, fi in rows}
