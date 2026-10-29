"""F-PY（httpx）・F-TS（ky）の索引（PaaD）に対して、M1 の T-19 ``get_project_db_schema`` と T-20 ``query_project_db`` の事後条件を
確かめる契約テスト（BUG-0036、仕様書 §15.2 M1-1、§11 の T-19・T-20）。

DB は ``bench/.cache`` の F-PY・F-TS を複製して本物の索引器で作ったもの（合成の DB ではない）。結線・skip の扱いは
``test_fx_read_T00_T08.py`` と同じ。DB のバイト列とビューの行数は、索引が落ち着いた（``_rig.open_rig`` が差分索引の完了を待った）あとに比べる。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from _rig import Rig
from _t11_harness import sha

from lucianke.tools import t19_get_project_db_schema as t19

WRITES = [
    "INSERT INTO symbols(urn) VALUES ('x')",
    "UPDATE symbols SET name = 'x'",
    "DELETE FROM symbols",
    "CREATE TABLE lk_t(a)",
    "DROP TABLE symbols",
    "ALTER TABLE symbols ADD COLUMN lk_x TEXT",
    "ATTACH DATABASE ':memory:' AS lk_m",
    "PRAGMA writable_schema = 1",
    "SELECT 1; SELECT 2",
    "SELECT 1; DELETE FROM symbols",
]


def db_path(rig: Rig) -> Path:
    rt = rig.hub.runtime()
    assert rt is not None
    return rt.db_path


def counts(path: Path, views: list[str]) -> dict[str, int]:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
        return {v: conn.execute(f"SELECT COUNT(*) FROM {v}").fetchone()[0] for v in views}


def columns(path: Path, view: str) -> set[str]:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({view})")}


async def query(rig: Rig, sql: str, **args: Any) -> Any:
    return await rig.call("query_project_db", query=sql, **args)


async def test_t19_postcondition_1_2_3_4_5_schema(rig: Rig) -> None:
    """PC:T-19#1 PC:T-19#2 PC:T-19#3 PC:T-19#4 PC:T-19#5 — 公開ビュー 10 個・列名・サンプル SQL の実行・行数・coverage の和。"""
    path = db_path(rig)
    r = await rig.ok("get_project_db_schema", include_stats=True, include_sample_queries=True)
    data = r["data"]
    tables = data["tables"]
    assert {t["name"] for t in tables} == set(t19.PUBLIC_VIEWS) and len(tables) == 10  # 事後条件 1
    for t in tables:  # 事後条件 2
        assert {c["name"] for c in t["columns"]} == columns(path, t["name"]), t["name"]
    assert data["sample_queries"]
    for q in data["sample_queries"]:  # 事後条件 3
        res = await query(rig, q["sql"], params=q.get("params_example") or {})
        assert not res.is_error and res.structured["ok"] is True, (q["id"], res.text[:300])
    expected = counts(path, [t["name"] for t in tables])
    for t in tables:  # 事後条件 4
        assert t["row_count_exact"] is True
        assert t["row_count"] == expected[t["name"]], t["name"]
    assert expected["symbols"] > 100 and expected["relations"] > 100  # 本物の索引（合成の少数行ではない）
    cov = data["coverage"]  # 事後条件 5
    assert set(cov) == {"exact", "structural", "heuristic", "lexical_fallback"}
    assert round(sum(cov.values()), 3) == 1.0


async def test_t20_postcondition_1_2_writes_are_rejected_and_the_db_is_untouched(rig: Rig) -> None:
    """PC:T-20#1 PC:T-20#2 — 書き込み文・複文は E_QUERY_REJECTED で、DB ファイルの SHA-256 と公開ビューの行数は変わらない。"""
    path = db_path(rig)
    views = list(t19.PUBLIC_VIEWS)
    before_hash, before_counts = sha(path.read_bytes()), counts(path, views)
    for sql in WRITES:
        r = await query(rig, sql)
        assert r.is_error and r.structured["error"]["code"] == "E_QUERY_REJECTED", (sql, r.text[:300])
    ok = await query(rig, "SELECT count(*) AS n FROM symbols")
    assert ok.structured["ok"] is True
    assert sha(path.read_bytes()) == before_hash and counts(path, views) == before_counts


async def test_t20_postcondition_3_4_5_limits_timeout_and_params(rig: Rig) -> None:
    """PC:T-20#3 PC:T-20#4 PC:T-20#5 — row_count <= limit と truncated、elapsed_ms <= timeout_ms、params と :name の対応。"""
    total = (await rig.ok("query_project_db", query="SELECT COUNT(*) AS n FROM symbols"))["data"]["rows"][0][
        "n"
    ]
    assert total > 100
    for limit in (1, 7, 100, total - 1, total, total + 1, 2000):
        if not 1 <= limit <= 2000:
            continue
        d = (await rig.ok("query_project_db", query="SELECT urn FROM symbols ORDER BY urn", limit=limit))[
            "data"
        ]
        assert d["row_count"] <= limit  # 事後条件 3（rows[] は max_tokens で切れうる。件数は row_count）
        assert d["truncated"] is (total > limit), (limit, total)
    r = await rig.ok("query_project_db", query="SELECT count(*) AS n FROM relations", timeout_ms=5000)
    assert r["data"]["elapsed_ms"] <= 5000  # 事後条件 4
    sql = "SELECT name FROM symbols WHERE kind = :kind AND name <> :skip ORDER BY name LIMIT 3"
    ok = await rig.ok("query_project_db", query=sql, params={"kind": "Function", "skip": "__none__"})
    assert ok["data"]["rows"]  # 事後条件 5
    for params in ({"kind": "Function", "skip": "x", "unused": 1}, {"kind": "Function"}, None):
        bad = await query(rig, sql, **({} if params is None else {"params": params}))
        assert bad.is_error and bad.structured["error"]["code"] == "E_INVALID_ARGUMENT", params
