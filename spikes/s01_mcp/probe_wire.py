"""S-1 の 1・2・4 を、SDK を通さない生の JSON-RPC 行で確かめる（外から stdout のバイト列を記録して検査する）。
使い方: uv run --frozen python spikes/s01_mcp/probe_wire.py     （各項目を PASS / FAIL で表示。FAIL があっても最後まで走る）
  1 tools/list のスキーマ素通し / 2 結果の content・structuredContent・isError / 4 fd 退避と stdout の純度（5 モード）
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rawclient import RawServer
from schemas import ARGS10_SCHEMA, EMPTY_SCHEMA

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def stdout_report(s: RawServer) -> dict[str, Any]:
    """本物の stdout に出た行の検査: 全行が JSON-RPC 2.0 か、junk が混ざっていないか、CR が無いか。"""
    bad: list[bytes] = []
    for line in s.stdout_lines:
        try:
            ok = json.loads(line).get("jsonrpc") == "2.0"
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            bad.append(line[:80])
    return {
        "lines": len(s.stdout_lines),
        "non_jsonrpc_lines": bad,
        "has_junk": any(b"junk" in line and b"jsonrpc" not in line for line in s.stdout_lines),
        "has_cr": any(line.endswith(b"\r\n") for line in s.stdout_lines),
    }


def part_1_2() -> None:
    print("== 1/2: tools/list と tools/call（divert=manual）")
    s = RawServer(["--divert=manual"])
    init = s.initialize()
    check("initialize 応答", "result" in init, f"protocolVersion={init['result']['protocolVersion']}")
    tl = s.request("tools/list")["result"]["tools"]
    by_name = {t["name"]: t for t in tl}
    check("1a tools/list: 全ツールが inputSchema を持つ", all("inputSchema" in t for t in tl), f"{len(tl)} tools")
    check("1b noop の inputSchema が完全一致（additionalProperties:false 含む）",
          by_name["noop"]["inputSchema"] == EMPTY_SCHEMA, json.dumps(by_name["noop"]["inputSchema"]))  # fmt: skip
    got = by_name["validated"]["inputSchema"]
    check("1c pydantic 生成スキーマ（10 フィールド, $defs なし）が完全一致", got == ARGS10_SCHEMA, f"keys={sorted(got)}")
    check("1d additionalProperties:false が届く", got.get("additionalProperties") is False)
    check("1e `default: null` / anyOf / enum / minimum が失われない",
          got["properties"]["workspace_id"].get("default", "MISSING") is None
          and "anyOf" in got["properties"]["workspace_id"]
          and got["properties"]["format"]["enum"] == ["compact", "json", "dot", "mermaid"]
          and got["properties"]["max_tokens"]["minimum"] == 200,
          json.dumps(got["properties"]["workspace_id"]))  # fmt: skip

    r = s.call("structured")["result"]
    check("2a content[0] は type=text", r["content"][0]["type"] == "text")
    check("2b structuredContent がそのまま届く（null を含む入れ子も）",
          r["structuredContent"] == {"ok": True, "tool": "structured", "data": {"items": [1, 2, 3], "nested": {"a": None}}, "warnings": []})  # fmt: skip
    check("2c 成功時の isError は false", r["isError"] is False)
    resp = s.call("fail")
    r = resp["result"]
    check("2d isError:true の通常結果（JSON-RPC error ではない）", "error" not in resp and r["isError"] is True)
    check("2e isError:true でも content と structuredContent が載る",
          r["content"][0]["text"].startswith("# fail error") and r["structuredContent"]["error"]["code"] == "E_TARGET_NOT_FOUND")  # fmt: skip
    resp = s.call("raise")
    check("2f ハンドラ内例外 → 自前ラッパで isError に変換できる", resp["result"]["isError"] is True)
    resp = s.call("raise_uncaught")
    print("     (参考) on_call_tool から例外が漏れたとき:", json.dumps(resp, ensure_ascii=False))
    resp = s.call("no_such_tool")
    check("2g 未知のツール名 → JSON-RPC エラー(-32602)", resp.get("error", {}).get("code") == -32602, json.dumps(resp["error"]))  # fmt: skip
    resp = s.call("validated", {"path": "x", "bogus": 1})
    print("     (参考) validated に未知の引数 bogus: SDK は検証しない →", json.dumps(resp["result"]["structuredContent"]))
    resp = s.call("noop", {"anything": 1})
    print("     (参考) noop(additionalProperties:false) に余計な引数: SDK は弾かない →", "result" in resp)
    rep = stdout_report(s)
    check("2h 以上の往復で stdout は JSON-RPC の行のみ・LF のみ",
          not rep["non_jsonrpc_lines"] and not rep["has_cr"], f"{rep['lines']} lines")  # fmt: skip
    rc, dt = s.close()
    check("2i stdin を閉じるとプロセスが終了する（ADR-16）", rc == 0, f"exit={rc} {dt * 1000:.0f} ms")


def part_4() -> None:
    print("== 4: fd 退避と stdout の純度（ツール内で print / os.write / CRT / WriteFile / 子プロセスを実行）")
    markers = ["junk-print", "junk-os-write", "junk-sys-dunder-stdout", "junk-crt-puts",
               "junk-winapi-writefile", "junk-child-inherit"]  # fmt: skip
    for mode in ("manual", "sdk", "manual-nohandle", "naive", "none"):
        s = RawServer([f"--divert={mode}"], watchdog=20)
        try:
            s.initialize()
            r = s.call("junk")["result"]
            done = r["structuredContent"]["done"]
            s.call("noop")
            if mode in ("manual", "sdk", "manual-nohandle"):
                ci = s.call("child_stdin")["result"]["structuredContent"]["child_stdin"]
            else:
                ci = "(skip)"
        except Exception as exc:  # noqa: BLE001
            done, ci = [], f"(失敗: {type(exc).__name__})"
        s.close(timeout=5)
        rep = stdout_report(s)
        leaked = [m for m in markers if m.encode() in b"".join(s.stdout_lines)]
        in_err = [m for m in markers if m in s.stderr]
        jsonrpc_in_err = '"jsonrpc"' in s.stderr
        print(f"  [{mode}] stdout {rep['lines']} 行 / 非JSON-RPC行={len(rep['non_jsonrpc_lines'])} / "
              f"漏れたjunk={leaked} / stderr に出たjunk={len(in_err)}/{len(markers)} / "
              f"JSON-RPC が stderr 側へ={jsonrpc_in_err} / CR={rep['has_cr']} / 子の stdin 読取={ci}")  # fmt: skip
        if mode == "naive":
            print(f"     naive の stderr 先頭: {s.stderr[:160]!r}")
        pure = not rep["non_jsonrpc_lines"] and not rep["has_junk"] and rep["lines"] > 0
        expect_pure = mode in ("manual", "sdk", "manual-nohandle")
        check(f"4 [{mode}] stdout 純度", pure == expect_pure,
              f"期待={'純粋' if expect_pure else '汚染/不通'} 実際={'純粋' if pure else '汚染/不通'} done={len(done)}経路")  # fmt: skip


if __name__ == "__main__":
    part_1_2()
    part_4()
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} PASS")
    sys.exit(1 if bad else 0)
