"""LSP の主要要求を通し、応答時間と返る形を JSON に残す共通スイート（S-4 / S-5 から使う）。使い捨て。

使い方: `uv run python spikes/s04_lsp/run_s04.py basedpyright|ruff`、`uv run python spikes/s05_ts/run_s05.py vtsls|tsls`
結果は spikes/<dir>/out/<name>.json（gitignore 済み）と標準出力の要約に出る。
ここには、サーバー別の台本（Spec）を受けて走らせる関数 run_suite だけを置く。
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lsp_probe import TRACE, LspClient, LspError, col_in, norm_uri, path_to_uri, time_requests


@dataclass
class Target:
    file: str
    needle: str  # ファイル内で探す文字列（最初の一致）
    delta: int = 0  # 一致の先頭から何コードポイント進めた位置か
    occurrence: int = 0


@dataclass
class Spec:
    name: str
    cmd: list[str]
    root: Path
    language_id: str
    files: list[str]
    init_options: dict[str, Any] | None = None
    config_provider: Any = None
    env: dict[str, str] | None = None
    document_changes: bool = True
    pull_diagnostics: bool = False
    # 要求の対象
    definition: Target | None = None
    references: Target | None = None
    hovers: list[Target] = field(default_factory=list)
    rename: tuple[Target, str] | None = None
    call_hierarchy: Target | None = None
    code_action_files: list[str] = field(default_factory=list)
    organize_imports_file: str | None = None
    # 診断の版の検査（didChange）
    change_file: str | None = None
    change_old: str = ""
    change_new: str = ""
    change_text_extra: str = ""  # 連続変更の 2 本目に足す文
    n_warm: int = 10
    settle_s: float = 3.0
    ready_timeout_s: float = 90.0
    per_file_timeout_s: float = 6.0
    comment: str = "#"  # 行コメントの記号（言語ごと）


def pos_of(root: Path, t: Target, encoding: str) -> dict[str, int]:
    text = (root / t.file).read_text(encoding="utf-8")
    idx = -1
    for _ in range(t.occurrence + 1):
        idx = text.index(t.needle, idx + 1)
    idx += t.delta
    line = text.count("\n", 0, idx)
    line_start = text.rfind("\n", 0, idx) + 1
    line_text = text[line_start:text.find("\n", line_start)]
    cp = idx - line_start
    return {"line": line, "character": col_in(line_text, cp, encoding)}


def tdp(root: Path, t: Target, enc: str) -> dict[str, Any]:
    return {"textDocument": {"uri": path_to_uri(root / t.file)}, "position": pos_of(root, t, enc)}


def rel(root: Path, uri: str) -> str:
    try:
        from urllib.parse import unquote, urlparse
        p = Path(unquote(urlparse(uri).path.lstrip("/")))
        return p.resolve().relative_to(root.resolve()).as_posix()
    except Exception:  # noqa: BLE001
        return uri


def summarize_locations(root: Path, res: Any) -> list[str]:
    if res is None:
        return []
    if isinstance(res, dict):
        res = [res]
    out = []
    for loc in res:
        uri = loc.get("uri") or loc.get("targetUri")
        r = loc.get("range") or loc.get("targetSelectionRange")
        out.append(f"{rel(root, uri)}:{r['start']['line']}:{r['start']['character']}")
    return out


def summarize_edit(root: Path, we: Any) -> dict[str, Any]:
    if we is None:
        return {"shape": None}
    shape = [k for k in ("changes", "documentChanges", "changeAnnotations") if k in we]
    files: dict[str, int] = {}
    kinds: set[str] = set()
    if "changes" in we:
        for uri, edits in we["changes"].items():
            files[rel(root, uri)] = files.get(rel(root, uri), 0) + len(edits)
    for dc in we.get("documentChanges", []) or []:
        if "textDocument" in dc:
            kinds.add("TextDocumentEdit")
            u = rel(root, dc["textDocument"]["uri"])
            files[u] = files.get(u, 0) + len(dc["edits"])
            kinds.add(f"textDocument.version={dc['textDocument'].get('version')!r}")
        else:
            kinds.add(dc.get("kind", "?"))
    return {"shape": shape, "files": files, "documentChanges_kinds": sorted(kinds)}


def summarize_actions(actions: Any) -> list[dict[str, Any]]:
    out = []
    for a in actions or []:
        if "command" in a and isinstance(a["command"], str):  # Command（CodeAction ではない）
            out.append({"kind": "Command", "title": a.get("title"), "command": a["command"]})
            continue
        out.append({
            "title": a.get("title"), "kind": a.get("kind"),
            "has_edit": "edit" in a, "has_command": "command" in a,
            "command": (a.get("command") or {}).get("command"),
            "has_data": "data" in a, "isPreferred": a.get("isPreferred"),
            "disabled": a.get("disabled"),
        })
    return out


def summarize_hover(res: Any) -> Any:
    if res is None:
        return None
    c = res.get("contents")
    if isinstance(c, dict):
        return {"kind": c.get("kind") or c.get("language"), "value": c.get("value")}
    return c


def full_range(text: str) -> dict[str, Any]:
    n = text.count("\n") + 1
    return {"start": {"line": 0, "character": 0}, "end": {"line": n, "character": 0}}


async def safe(coro: Any) -> Any:
    try:
        return await coro
    except LspError as e:
        return {"__error__": str(e)}
    except TimeoutError:
        return {"__error__": "timeout"}
    except ConnectionError as e:
        return {"__error__": f"connection: {e}"}


def diag_summary(n: Any, base: float | None = None) -> dict[str, Any]:
    p = n.params
    d: dict[str, Any] = {
        "t": round(n.t, 3), "uri": p["uri"].rsplit("/", 1)[-1], "version": p.get("version", "<absent>"),
        "n": len(p["diagnostics"]),
        "items": [f"{x.get('source')}:{x.get('code')}:{x['range']['start']['line']}:{(x.get('severity'))}"
                  for x in p["diagnostics"]][:8],
    }
    if base is not None:
        d["dt_ms"] = round((n.t - base) * 1000, 1)
    return d


def step(msg: str) -> None:
    if TRACE:
        import sys
        print(f"STEP {msg}", file=sys.stderr, flush=True)


async def run_suite(spec: Spec) -> dict[str, Any]:
    R: dict[str, Any] = {"name": spec.name, "cmd": spec.cmd}
    root = spec.root
    c = LspClient(spec.name, spec.cmd, root, config_provider=spec.config_provider, env=spec.env)
    await c.start()
    try:
        init = await c.initialize(init_options=spec.init_options, document_changes=spec.document_changes,
                                  pull_diagnostics=spec.pull_diagnostics)
        caps = init.get("capabilities", {})
        R["server_info"] = init.get("serverInfo")
        R["negotiated_encoding"] = caps.get("positionEncoding", "<absent>")
        R["capabilities_summary"] = {k: caps.get(k) for k in (
            "textDocumentSync", "definitionProvider", "referencesProvider", "hoverProvider", "renameProvider",
            "codeActionProvider", "callHierarchyProvider", "diagnosticProvider", "executeCommandProvider",
            "positionEncoding") if k in caps}
        R["initialize_ms"] = c.timings["initialize_ms"]
        enc = c.encoding

        step('initialized')
        # ---- 開く → 最初の診断 ----
        t_open = c.now()
        for f in spec.files:
            c.did_open(root / f, spec.language_id, 1)
        first_diag: dict[str, Any] = {}
        for i, f in enumerate(spec.files):
            # 診断が 0 件のファイルには publishDiagnostics を送らないサーバー（vtsls）がある。1 本目だけ長く待つ
            n = await c.wait_for("textDocument/publishDiagnostics",
                                 lambda p, f=f: norm_uri(p["uri"]) == norm_uri(path_to_uri(root / f)),
                                 timeout=spec.ready_timeout_s if i == 0 else spec.per_file_timeout_s)
            first_diag[f] = diag_summary(n, t_open) if n else None
        step('first diags')
        R["first_diagnostics"] = first_diag
        raw_first = next((n for n in c.notifications if n.method == "textDocument/publishDiagnostics"), None)
        R["uri_forms"] = {"sent": path_to_uri(root / spec.files[0]),
                          "received_in_publishDiagnostics": raw_first.params["uri"] if raw_first else None}
        R["t_spawn_to_first_diag_s"] = {f: (round(d["t"], 3) if d else None) for f, d in first_diag.items()}
        await asyncio.sleep(spec.settle_s)  # $/progress の完了や後続の診断を待つ
        prog = [n for n in c.notifications if n.method == "$/progress"]
        creates = [n for n in c.server_requests if n.method == "window/workDoneProgress/create"]
        R["progress"] = {
            "workDoneProgress_create_requests": len(creates),
            "progress_notifications": len(prog),
            "events": [{"t": round(n.t, 3), "token": str(n.params.get("token"))[:24],
                        "kind": (n.params.get("value") or {}).get("kind"),
                        "title": (n.params.get("value") or {}).get("title"),
                        "message": (n.params.get("value") or {}).get("message")} for n in prog][:30],
        }
        R["server_requests"] = sorted({n.method for n in c.server_requests})
        R["configuration_sections"] = [it.get("section") for n in c.server_requests
                                       if n.method == "workspace/configuration"
                                       for it in n.params["items"]]
        R["register_capability"] = [
            [r.get("method") for r in n.params.get("registrations", [])]
            for n in c.server_requests if n.method == "client/registerCapability"]
        R["diagnostics_all"] = [diag_summary(n) for n in c.notifications
                                if n.method == "textDocument/publishDiagnostics"][:20]
        R["window_messages"] = [{"t": round(n.t, 3), "m": n.method, "text": str(n.params.get("message"))[:140]}
                                for n in c.notifications
                                if n.method in ("window/logMessage", "window/showMessage")][:15]

        step('settled; requests')
        # ---- 要求 ----
        N = spec.n_warm
        feats: dict[str, Any] = {}
        R["features"] = feats

        async def timed(key: str, method: str, params: Any, summarize: Any, timeout: float = 30) -> Any:
            try:
                r = await time_requests(c, method, params, N, timeout)
            except LspError as e:
                feats[key] = {"error": str(e)}
                return None
            except TimeoutError:
                feats[key] = {"error": "timeout"}
                return None
            res = r.pop("result")
            feats[key] = {**r, "result": summarize(res)}
            return res

        if spec.definition:
            await timed("definition", "textDocument/definition", tdp(root, spec.definition, enc),
                        lambda r: summarize_locations(root, r))
        if spec.references:
            p = tdp(root, spec.references, enc)
            p["context"] = {"includeDeclaration": True}
            await timed("references", "textDocument/references", p, lambda r: summarize_locations(root, r))
        for i, h in enumerate(spec.hovers):
            await timed(f"hover[{i}:{h.needle!r}+{h.delta}]", "textDocument/hover", tdp(root, h, enc), summarize_hover)
        if spec.rename:
            tgt, new = spec.rename
            await timed("prepareRename", "textDocument/prepareRename", tdp(root, tgt, enc), lambda r: r)
            p = tdp(root, tgt, enc)
            p["newName"] = new
            await timed("rename", "textDocument/rename", p, lambda r: summarize_edit(root, r), timeout=60)
            # 返った WorkspaceEdit の実物（1 本目の編集）を残す
            raw = await safe(c.request("textDocument/rename", p, timeout=60))
            if isinstance(raw, dict):
                sample = None
                if "changes" in raw:
                    u = next(iter(raw["changes"]))
                    sample = {"changes_key": rel(root, u), "edit0": raw["changes"][u][0]}
                elif raw.get("documentChanges"):
                    sample = {"documentChanges0": raw["documentChanges"][0]}
                feats["rename"]["raw_sample"] = sample
        if spec.call_hierarchy:
            p = tdp(root, spec.call_hierarchy, enc)
            items = await timed("callHierarchy/prepare", "textDocument/prepareCallHierarchy", p,
                                lambda r: [{"name": i.get("name"), "kind": i.get("kind"),
                                            "file": rel(root, i["uri"]), "keys": sorted(i.keys())} for i in (r or [])])
            if items:
                item = items[0]
                await timed("callHierarchy/incomingCalls", "callHierarchy/incomingCalls", {"item": item},
                            lambda r: [f"{x['from']['name']}@{rel(root, x['from']['uri'])}" for x in (r or [])])
                await timed("callHierarchy/outgoingCalls", "callHierarchy/outgoingCalls", {"item": item},
                            lambda r: [f"{x['to']['name']}@{rel(root, x['to']['uri'])}" for x in (r or [])])

        # codeAction: 診断ごと + organizeImports
        ca: dict[str, Any] = {}
        feats["codeAction"] = ca
        for f in spec.code_action_files:
            uri = path_to_uri(root / f)
            latest = [n for n in c.notifications if n.method == "textDocument/publishDiagnostics"
                      and norm_uri(n.params["uri"]) == norm_uri(uri)]
            diags = latest[-1].params["diagnostics"] if latest else []
            text = (root / f).read_text(encoding="utf-8")
            entries = []
            seen = set()
            for d in diags:
                key = (d.get("code"), d["range"]["start"]["line"])
                if key in seen:
                    continue
                seen.add(key)
                params = {"textDocument": {"uri": uri}, "range": d["range"], "context": {"diagnostics": [d]}}
                r = await time_requests(c, "textDocument/codeAction", params, N, 30) if len(entries) < 2 else None
                if r is None:
                    res = await safe(c.request("textDocument/codeAction", params, timeout=30))
                    timing = None
                else:
                    res = r.pop("result")
                    timing = r
                entry: dict[str, Any] = {"diag": f"{d.get('source')}:{d.get('code')} L{d['range']['start']['line']}"
                                                 f" sev={d.get('severity')} tags={d.get('tags')}",
                                         "timing": timing}
                if isinstance(res, dict) and "__error__" in res:
                    entry["error"] = res["__error__"]
                else:
                    entry["actions"] = summarize_actions(res)
                    # edit が無く data / command だけなら resolve を試す
                    for a in res or []:
                        if "command" in a and isinstance(a["command"], str):
                            continue
                        if "edit" not in a:
                            rr = await safe(c.request("codeAction/resolve", a, timeout=30))
                            entry.setdefault("resolve", []).append({
                                "title": a.get("title"),
                                "resolved_has_edit": isinstance(rr, dict) and "edit" in rr,
                                "result": rr if isinstance(rr, dict) and "__error__" in rr else None})
                        else:
                            entry.setdefault("sample_edit", summarize_edit(root, a["edit"]))
                entries.append(entry)
            # 診断なしの全文範囲 + only=quickfix
            res = await safe(c.request("textDocument/codeAction", {
                "textDocument": {"uri": uri}, "range": full_range(text),
                "context": {"diagnostics": diags, "only": ["quickfix"]}}, timeout=30))
            entries.append({"diag": "<全文範囲 + 全診断 + only=quickfix>",
                            "actions": summarize_actions(res) if not isinstance(res, dict) else res})
            ca[f] = entries
        if spec.organize_imports_file:
            f = spec.organize_imports_file
            uri = path_to_uri(root / f)
            text = (root / f).read_text(encoding="utf-8")
            params = {"textDocument": {"uri": uri}, "range": full_range(text),
                      "context": {"diagnostics": [], "only": ["source.organizeImports"]}}
            r = await time_requests(c, "textDocument/codeAction", params, N, 30)
            res = r.pop("result")
            acts = summarize_actions(res)
            samp = None
            for a in res or []:
                if "edit" in a:
                    samp = summarize_edit(root, a["edit"])
                    samp["first_edit"] = (list(a["edit"].get("changes", {}).values()) or [None])[0]
                    if samp["first_edit"] is None and a["edit"].get("documentChanges"):
                        samp["first_edit"] = a["edit"]["documentChanges"][0]
            resolved = None
            for a in res or []:
                if "edit" not in a:
                    rr = await safe(c.request("codeAction/resolve", a, timeout=30))
                    if isinstance(rr, dict) and "edit" in rr:
                        resolved = summarize_edit(root, rr["edit"])
                        fe = (list(rr["edit"].get("changes", {}).values()) or [None])[0]
                        if fe is None and rr["edit"].get("documentChanges"):
                            fe = rr["edit"]["documentChanges"][0]
                        resolved["first_edit"] = fe
                    else:
                        resolved = rr
            ca["organizeImports"] = {"file": f, "timing": r, "actions": acts, "edit": samp, "resolved": resolved}
            # source.fixAll も
            params["context"]["only"] = ["source.fixAll"]
            res2 = await safe(c.request("textDocument/codeAction", params, timeout=30))
            ca["fixAll"] = summarize_actions(res2) if not isinstance(res2, dict) else res2

        # ---- didChange と診断の版 ----
        if spec.change_file:
            f = spec.change_file
            path = root / f
            uri = path_to_uri(path)
            base_text = path.read_text(encoding="utf-8")
            assert spec.change_old in base_text, "change_old が本文に無い"
            chg: dict[str, Any] = {}
            R["didChange"] = chg
            # 1 本だけ変更
            t_send = c.did_change(path, 2, base_text.replace(spec.change_old, spec.change_new))
            await asyncio.sleep(0)
            n = await c.wait_for("textDocument/publishDiagnostics", lambda p: norm_uri(p["uri"]) == norm_uri(uri),
                                 timeout=20, after=t_send)
            chg["single_change"] = {"sent_version": 2, "first_diag": diag_summary(n, t_send) if n else None}
            await asyncio.sleep(spec.settle_s)
            chg["single_change"]["all_diags_after"] = [
                diag_summary(x, t_send) for x in c.notifications if x.method == "textDocument/publishDiagnostics"
                and norm_uri(x.params["uri"]) == norm_uri(uri) and x.t >= t_send]
            # 連続 3 本（v3, v4, v5）を間を空けずに送る
            mark = len(c.notifications)
            t_send = c.now()
            texts = {
                3: base_text,
                4: base_text.replace(spec.change_old, spec.change_new) + spec.change_text_extra,
                5: base_text + spec.change_text_extra,
            }
            for v in (3, 4, 5):
                c.did_change(path, v, texts[v])
            await asyncio.sleep(spec.settle_s + 1.5)
            chg["burst_v3_v4_v5"] = {"all_diags_after": [
                diag_summary(x, t_send) for x in c.notifications[mark:]
                if x.method == "textDocument/publishDiagnostics" and norm_uri(x.params["uri"]) == norm_uri(uri)]}
            # 診断が 0 件から 0 件のとき（内容は変わるが診断は変わらない変更）で再送されるか
            mark = len(c.notifications)
            t_send = c.did_change(path, 6, base_text + f"\n{spec.comment} comment only\n")
            await asyncio.sleep(spec.settle_s)
            chg["comment_only_v6"] = {"all_diags_after": [
                diag_summary(x, t_send) for x in c.notifications[mark:]
                if x.method == "textDocument/publishDiagnostics" and norm_uri(x.params["uri"]) == norm_uri(uri)]}
            # 診断が変わらない変更（コメントだけ）を 2 回続けて送ると、版つきで再送されるか
            mark = len(c.notifications)
            t_send = c.did_change(path, 7, base_text + f"\n{spec.comment} noop A\n")
            await asyncio.sleep(spec.settle_s)
            mark2 = len(c.notifications)
            t_send2 = c.did_change(path, 8, base_text + f"\n{spec.comment} noop B (different comment)\n")
            await asyncio.sleep(spec.settle_s)
            chg["noop_v7_then_v8"] = {"after_v7": [
                diag_summary(x, t_send) for x in c.notifications[mark:mark2]
                if x.method == "textDocument/publishDiagnostics" and norm_uri(x.params["uri"]) == norm_uri(uri)],
                "after_v8": [diag_summary(x, t_send2) for x in c.notifications[mark2:]
                             if x.method == "textDocument/publishDiagnostics" and norm_uri(x.params["uri"]) == norm_uri(uri)]}
            # 10 回計測（毎回 1 文字変えて、版つき診断が届くまで）
            lat: list[float] = []
            ver = 20
            for i in range(N):
                ver += 1
                txt = base_text.replace(spec.change_old, spec.change_new if i % 2 == 0 else spec.change_old)
                txt += f"\n{spec.comment} {i}\n"
                mark = len(c.notifications)
                ts = c.did_change(path, ver, txt)
                n = await c.wait_for("textDocument/publishDiagnostics",
                                     lambda p: norm_uri(p["uri"]) == norm_uri(uri), timeout=20, after=ts, start=mark)
                if n is not None and (n.params.get("version") in (None, ver) or "version" not in n.params):
                    lat.append((n.t - ts) * 1000)
                else:
                    # 版が合うまで待つ
                    n2 = await c.wait_for("textDocument/publishDiagnostics",
                                          lambda p, ver=ver: norm_uri(p["uri"]) == norm_uri(uri)
                                          and p.get("version") == ver, timeout=20, after=ts, start=mark)
                    if n2 is not None:
                        lat.append((n2.t - ts) * 1000)
                await asyncio.sleep(0.3)
            chg["latency_ms_each"] = [round(x, 1) for x in lat]
            if lat:
                chg["latency_median_ms"] = round(statistics.median(lat), 1)
            chg["latency_note"] = "診断なし→なしでも毎回届く場合のみ n=10。届かない回は欠落"

        R["stderr_head"] = [f"{t:.2f} {line}" for t, line in c.stderr_lines[:12]]
    finally:
        R["shutdown"] = await c.shutdown()
    return R


def dump(R: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")


def now_s() -> float:
    return time.perf_counter()
