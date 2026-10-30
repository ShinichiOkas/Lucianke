"""M1-6（仕様書 §15.2）: ``.mcp.json`` の command と args でサーバーを起動し、台本化したセッション
（骨格 -> 本体 -> 編集 -> 診断ゼロ確認）が完走する。

台本は ``bench/e2e/session.toml``、実行器は ``bench/e2e/run_session.py``。この試験は実行器を 1 回走らせて合否を見る
（数十秒。待ちはすべて条件待ちで、固定 sleep は使わない）。記録は pytest の一時ディレクトリに書く
（``bench/e2e/records/`` に残す記録は、実行器を直接走らせたときのもの）。

止まった段は実行器が「未成立」（``unmet``）と記録して先へ進むので、失敗時のメッセージにどの段が何のエラーで
止まったかが出る。``should`` の事後条件（記録するだけで未成立にしない）の食い違いは、成功時も出力に残す（``-s`` で見える）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
E2E = REPO / "bench" / "e2e"
sys.path.insert(0, str(E2E))
import run_session as rs  # noqa: E402

TIMEOUT_S = 480  # 台本の total_timeout_s（420）より長く。ハングしても試験が戻る


def test_script_is_well_formed() -> None:
    """台本が参照する事後条件・待ち条件がすべて実在し、根拠（spec）と水準（level）が書かれている。"""
    doc = tomllib.loads(rs.SCRIPT.read_text(encoding="utf-8"))
    names = [s["name"] for s in doc["stage"]]
    assert len(names) == len(set(names))
    for st in doc["stage"]:
        assert st.get("call") == "tools/list" or st.get("tool"), st["name"]
        assert all(n in names for n in st.get("needs", [])), st["name"]
        if "wait_until" in st:
            assert st["wait_until"] in rs.WAITS, st["name"]
        assert st.get("checks"), f"{st['name']}: 各段は事後条件を 1 つ以上確かめる"
        for cid in st["checks"]:
            assert cid in rs.CHECKS, f"{st['name']}: unknown check {cid}"
    for cid, meta in doc["check"].items():
        assert meta.get("spec") and meta.get("level") in ("must", "should"), cid
    for cid in {c for st in doc["stage"] for c in st["checks"]}:
        assert cid in doc["check"], f"check {cid} has no [check.{cid}] entry"
    assert set(rs.CHECKS) <= set(doc["check"]), "実装だけあって台本に書かれていない事後条件"


async def test_m1_6_scripted_session_completes(tmp_path: Path) -> None:
    result = await asyncio.wait_for(rs.run(records_dir=tmp_path), TIMEOUT_S)
    report = rs.report(result)
    print("\n" + report)
    if result["soft_findings"]:
        print("soft findings (recorded, not failing): " + json.dumps(result["soft_findings"], ensure_ascii=True))

    assert result["ok"], "M1-6 session did not complete:\n" + report
    assert list(result["stages"]) == [
        "tools_list", "workspace", "skeleton", "bodies", "diag_before", "edit", "reread", "diag_after", "diag_alive",
    ]  # fmt: skip
    assert result["leftover_processes_after_exit"] == []

    # 記録: 先頭が session、末尾が summary（ok）。1 行 1 JSON。
    lines = Path(result["record"]).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(ln) for ln in lines]
    assert rows[0]["type"] == "session" and rows[-1]["type"] == "summary" and rows[-1]["ok"] is True
    assert rows[0]["launch"]["launched"]["command"] == rows[0]["launch"]["mcp_json"]["command"]  # .mcp.json の command そのまま
