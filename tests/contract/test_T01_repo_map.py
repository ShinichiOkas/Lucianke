"""T-01 ``get_repo_map`` の契約テスト（仕様書 §11.2 T-01 の処理規約・事後条件）。

F-POLY を ``tmp`` にコピーし（``.gitignore`` で外すファイルと ``exclude_globs`` で外すファイルを足す）、実 ``Indexer`` で
PaaD に取り込み（``scan`` が最後に PageRank を書く）、パイプライン経由で呼ぶ。構文木と索引だけ（``structural``）の範囲。

サービスは ``db_path``・``lines``・``workspace()`` を持つ偽物（``tools/__init__.py`` と ``app.py`` の結線には依存しない）。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import compact_grammar as g
import pytest

from lucianke.engine.indexer import Indexer
from lucianke.index import rank
from lucianke.index.connection import ReaderPool
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import t01_get_repo_map as t01
from lucianke.tools._base import Services
from lucianke.workspace.types import Workspace

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
WS_ID = "poly"
PAY = "src/shop/services/payment.py"
ROUTES = "src/shop/api/routes.py"
LIMITS = "src/shop/services/limits.py"
IGNORED = "build/generated.py"
EXCLUDED = "vendor/lib.py"
SECRET_VALUE = "SECRET_VALUE_MUST_NOT_LEAK"  # conf/.env に足す

IS_WINDOWS = os.name == "nt"


@dataclass(slots=True)
class FakeServices(Services):
    db_path: Path
    lines: ModuleType
    root: Path
    exclude_globs: tuple[str, ...] = ()

    def workspace(self, workspace_id: str | None = None) -> Workspace:
        return Workspace(
            workspace_id=WS_ID, root=self.root, exclude_globs=self.exclude_globs, case_insensitive=IS_WINDOWS
        )

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        return WS_ID


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Path]]:
    base = tmp_path_factory.mktemp("t01")
    root = Path(os.path.realpath(base / WS_ID))
    shutil.copytree(POLY, root)
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / IGNORED).write_text("def generated():\n    return 1\n", encoding="utf-8")
    (root / "vendor").mkdir()
    (root / EXCLUDED).write_text("def vendored():\n    return 2\n", encoding="utf-8")
    (root / "conf" / ".env").write_text(f"API_KEY={SECRET_VALUE}\n", encoding="utf-8")
    db = base / "db" / "paad.sqlite"

    async def scan() -> None:
        writer = IndexWriter(db)
        readers = ReaderPool(db, size=2)
        try:
            ws = Workspace(
                workspace_id=WS_ID, root=root, exclude_globs=("vendor",), case_insensitive=IS_WINDOWS
            )
            indexer = Indexer(ws, writer, readers, TreeCache())
            report = await indexer.scan()
            assert not report.errors, report.errors
            await indexer.aclose()
        finally:
            readers.close()
            writer.close()

    asyncio.run(scan())
    yield root, db


def _pipeline(root: Path, db: Path) -> Pipeline:
    svc = FakeServices(db_path=db, lines=compact, root=root, exclude_globs=("vendor",))
    registry = ToolRegistry()
    registry.register(t01.SPEC)
    return Pipeline(registry, services=svc)


@pytest.fixture(scope="module")
def pipeline(indexed: tuple[Path, Path]) -> Pipeline:
    return _pipeline(*indexed)


async def repo_map(pipeline: Pipeline, fmt: str = "json", **args: Any):
    return await pipeline.call_tool("get_repo_map", {"format": fmt, **args})


def files_of(r: Any) -> list[dict[str, Any]]:
    return r.structured["data"]["files"]


def paths_of(r: Any) -> list[str]:
    return [f["path"] for f in files_of(r)]


# --- 登録 -------------------------------------------------------------------------------


async def test_t01_in_tools_list(pipeline: Pipeline) -> None:
    descriptors = {d.name: d for d in pipeline.registry.descriptors()}
    assert "get_repo_map" in descriptors
    props = descriptors["get_repo_map"].input_schema["properties"]
    assert {"scope_paths", "include_signatures", "max_symbols_per_file", "ranking", "include_tests"} <= set(
        props
    )


# --- 順位 -------------------------------------------------------------------------------


async def test_pagerank_orders_the_hub_first(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, max_tokens=4000)
    assert not r.is_error, r.text
    assert r.structured["data"]["ranking_used"] == "pagerank"
    paths = paths_of(r)
    # 参照されるサービス層が先頭、誰からも参照されない・シンボルの無いファイルは後ろ
    assert paths[0] in {PAY, LIMITS, "src/shop/models/receipt.py"}
    assert paths.index(PAY) < paths.index(ROUTES)
    assert paths.index(PAY) < paths.index("README.md")


async def test_postcondition_1_rank_is_non_increasing(pipeline: Pipeline) -> None:
    for ranking in ("pagerank", "fan_in", "alpha"):
        r = await repo_map(pipeline, max_tokens=4000, ranking=ranking)
        assert not r.is_error, r.text
        ranks = [f["rank"] for f in files_of(r)]
        assert ranks == sorted(ranks, reverse=True), (ranking, ranks)
        assert r.structured["data"]["ranking_used"] == ranking
    top = (await repo_map(pipeline, max_tokens=4000)).structured["data"]["files"][0]
    assert top["rank"] == 1.0


async def test_ranking_is_deterministic(pipeline: Pipeline) -> None:
    runs = [paths_of(await repo_map(pipeline, max_tokens=4000)) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


async def test_alpha_is_path_order_and_fan_in_prefers_referenced_files(pipeline: Pipeline) -> None:
    alpha = paths_of(await repo_map(pipeline, max_tokens=4000, ranking="alpha"))
    assert alpha == sorted(alpha)
    fan_in = await repo_map(pipeline, max_tokens=4000, ranking="fan_in")
    first = files_of(fan_in)[0]
    assert first["fan_in"] > 0 and first["path"] in {PAY, LIMITS}


async def test_signature_and_fan_in_fields(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, max_tokens=4000)
    pay = next(f for f in files_of(r) if f["path"] == PAY)
    assert pay["language"] == "python" and pay["tier"] == 1 and pay["line_count"] > 0
    assert pay["fan_in"] >= 3  # process_charge は routes.py と tests から呼ばれる
    charge = next(s for s in pay["symbols"] if s["qualified_name"] == "PaymentProcessor.process_charge")
    assert charge["kind"] == "Method" and "pub" in charge["modifiers"] and "async" in charge["modifiers"]
    assert charge["signature"] and charge["urn"] == f"lk://{WS_ID}/{PAY}#PaymentProcessor.process_charge"
    assert {"start_line", "end_line"} <= set(charge["range"])
    assert set(charge) <= {
        "urn",
        "name",
        "qualified_name",
        "kind",
        "modifiers",
        "range",
        "signature",
        "confidence_level",
    }


# --- 事後条件 -----------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 5])
async def test_postcondition_2_symbols_per_file_at_most_n(pipeline: Pipeline, n: int) -> None:
    r = await repo_map(pipeline, max_tokens=4000, max_symbols_per_file=n)
    assert not r.is_error, r.text
    assert files_of(r)
    assert all(len(f["symbols"]) <= n for f in files_of(r))
    if n == 1:  # 各ファイルの代表 1 件は、そのファイルで最も重要な公開シンボル
        pay = next(f for f in files_of(r) if f["path"] == PAY)
        assert len(pay["symbols"]) == 1


async def test_postcondition_2_in_compact(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, "compact", max_tokens=4000, max_symbols_per_file=2)
    blocks = re.split(r"(?m)^=== ", r.text)[1:]
    assert blocks
    for block in blocks:
        top_level_symbols = [line for line in block.splitlines()[1:] if re.match(r"^\s*\[\w+\] ", line)]
        assert len(top_level_symbols) <= 2


async def test_postcondition_3_ignored_and_excluded_paths_never_appear(pipeline: Pipeline) -> None:
    for fmt in ("json", "compact"):
        r = await repo_map(pipeline, fmt, max_tokens=8000, include_tests=True)
        text = r.text
        assert IGNORED not in text and EXCLUDED not in text
        assert "generated" not in text.replace("generated_", "") or fmt == "json"
    assert not any(
        p.startswith(("build/", "vendor/")) for p in paths_of(await repo_map(pipeline, max_tokens=8000))
    )


async def test_postcondition_4_total_estimate_is_the_number_of_target_files(pipeline: Pipeline) -> None:
    full = await repo_map(pipeline, max_tokens=20000)
    all_paths = paths_of(full)
    assert full.structured["page"]["total_estimate"] == len(all_paths)
    assert not full.structured["page"].get("next_cursor")
    small = await repo_map(pipeline, max_tokens=800)
    assert not small.is_error, small.text
    page = small.structured["page"]
    assert len(files_of(small)) < len(all_paths)
    assert page["total_estimate"] == len(all_paths)  # 切り詰めても範囲内の対象ファイル総数
    assert page["next_cursor"]


@pytest.mark.parametrize("max_tokens", [400, 500, 1500, 5000])
async def test_postcondition_5_budget_is_respected(pipeline: Pipeline, max_tokens: int) -> None:
    for fmt in ("compact", "json"):
        r = await repo_map(pipeline, fmt, max_tokens=max_tokens)
        assert not r.is_error, r.text
        assert r.structured is None or r.structured["budget"]["used_tokens_est"] <= max_tokens
        if fmt == "json":
            assert r.structured["budget"]["used_tokens_est"] <= max_tokens


async def test_default_budget_is_1500(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline)
    assert r.structured["budget"]["max_tokens"] == 1500
    assert r.structured["budget"]["used_tokens_est"] <= 1500


# --- 予算を変えても文法に適合・重要なものが先 ----------------------------------------------------


@pytest.mark.parametrize("max_tokens", [200, 300, 600, 1500, 8000])
async def test_compact_conforms_to_the_grammar_at_any_budget(pipeline: Pipeline, max_tokens: int) -> None:
    for kwargs in ({}, {"include_signatures": False}, {"include_tests": True}, {"ranking": "alpha"}):
        r = await repo_map(pipeline, "compact", max_tokens=max_tokens, **kwargs)
        assert not r.is_error, r.text
        assert g.validate(r.text) == [], (max_tokens, kwargs, r.text)
        assert r.text.startswith("# get_repo_map ok ")
        m = re.search(r"~(\d+)/(\d+)tok", r.text.splitlines()[0])
        assert m and int(m.group(1)) <= int(m.group(2)) == max_tokens


@pytest.mark.parametrize("max_tokens", [260, 300, 400])
async def test_compact_uses_its_budget_when_a_degraded_file_is_further_down(pipeline: Pipeline, max_tokens: int) -> None:
    """BUG-0116: 後ろに精度の低いファイル（COBOL の heuristic）があっても、そのページに出ない降格の注意文の分を取り分けない。
    先頭の structural のファイルだけのページには ``! DEGRADED`` が無く、予算の 9 割以上を使う。以前は全ファイルの精度で
    前置きを数え（約 30 トークンの前置きに 106）、260 で ``~179/260``（先頭のファイルはシグネチャもシンボルも削られた）、300 で
    ``~215/300``、400 で ``~318/400`` だった。"""
    r = await repo_map(pipeline, "compact", max_tokens=max_tokens)
    assert g.validate(r.text) == [], r.text
    head = r.text.splitlines()[0]
    m = re.search(r"conf=(\w+) .* ~(\d+)/(\d+)tok$", head)
    assert m and m.group(1) == "structural", head
    assert "! DEGRADED" not in r.text and "# more: cursor=" in r.text, r.text
    assert int(m.group(2)) >= 0.9 * max_tokens, (head, r.text)
    assert int(m.group(2)) <= max_tokens
    # 先頭のファイルはシグネチャつきのシンボルを丸ごと返す（以前の 260 では `retry @L20-24` だけで予算も余っていた）。
    # 260 では概観のパッケージの行を残してシグネチャを外す（小さい予算でも全体の形を先に見せる。BUG-0120）
    want = "[Function] pub retry(attempt: int) -> int @L20-24" if max_tokens >= 300 else "[Function] pub retry @L20-24"
    assert want in r.text, r.text
    assert "- packages: src/shop/ (" in r.text, r.text


async def test_smaller_budget_is_a_prefix_of_the_larger(pipeline: Pipeline) -> None:
    """重要なファイルが先: 小さい予算で出るファイルは、大きい予算で出るファイルの先頭と同じ。"""
    big = paths_of(await repo_map(pipeline, max_tokens=8000))
    for max_tokens in (800, 1000, 1500):
        small = paths_of(await repo_map(pipeline, max_tokens=max_tokens))
        assert small and small == big[: len(small)]


async def test_important_symbols_come_first_within_the_budget(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, "compact", max_tokens=400)
    assert g.validate(r.text) == []
    assert (
        f"=== {PAY} " in r.text or f"=== {LIMITS} " in r.text
    )  # 参照の集まるサービス層が予算の小さい応答にも入る
    assert "README.md" not in r.text


async def test_compact_shape_follows_the_spec_example(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, "compact", max_tokens=3000)
    lines = r.text.splitlines()
    assert lines[0].startswith("# get_repo_map ok conf=")
    tree = next(line for line in lines if line.startswith("- ") and "files)" in line)
    assert re.search(r"src/ \(\d+ files\)", tree) and "./ (" in tree
    header = f"=== {PAY} ("
    i = next(k for k, line in enumerate(lines) if line.startswith(header))
    assert re.fullmatch(r"=== .+ \(\d+ lines\) ===", lines[i])
    assert re.fullmatch(r"- rank: \d\.\d\d fan_in: \d+", lines[i + 1])
    block = []
    for line in lines[i + 2 :]:
        if line.startswith(("=== ", "# ")):
            break
        block.append(line)
    assert 1 <= len([x for x in block if re.match(r"^\s*\[\w+\] ", x)]) <= 5
    cls = next(k for k, x in enumerate(block) if re.match(r"^\[Class\] pub PaymentProcessor @L\d+-\d+$", x))
    method = block[cls + 1]
    assert method.startswith("  [Method] pub async process_charge(")  # 入れ子（選ばれた親の下）と短い名前
    assert "-> Receipt" in method


def _overview_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines()[1:]:
        if line.startswith(("=== ", "# ")):
            break
        if line.startswith("- "):  # ``! DEGRADED`` などの行は概観ではない
            out.append(line)
    return out


async def test_overview_lists_packages_subpackages_and_entry_modules(pipeline: Pipeline) -> None:
    """先頭の概観: コードのパッケージの根（src/ → src/shop/、web/ → web/src/ とつなぐ）、2 段目のサブパッケージ（フルパス・
    ファイル数）、入口のモジュール。文書・データだけのディレクトリ（docs/・conf/）は根にしない（BUG-0120）。"""
    r = await repo_map(pipeline, "compact", max_tokens=3000)
    assert g.validate(r.text) == [], r.text
    ov = _overview_lines(r.text)
    assert ov[0].startswith("- src/ (") and "./ (" in ov[0]  # トップレベルの行（今までどおり）
    packages = next(x for x in ov if x.startswith("- packages: "))
    assert packages.startswith("- packages: src/shop/ (10 files), web/src/ (2 files)"), packages
    assert "docs/" not in packages and "conf/" not in packages
    assert "- entry modules: web/src/app.ts" in ov
    assert "- src/shop/: src/shop/models/ (4), src/shop/services/ (3), src/shop/api/ (2)" in ov
    j = await repo_map(pipeline, max_tokens=3000)
    data = j.structured["data"]
    assert data["packages"][0]["dir"] == "src/shop/" and data["packages"][0]["file_count"] == 10
    assert [c["dir"] for c in data["packages"][0]["children"]] == ["src/shop/models/", "src/shop/services/", "src/shop/api/"]
    assert data["entry_modules"] == ["web/src/app.ts"]


async def test_overview_and_file_blocks_never_shrink_as_max_tokens_grows(pipeline: Pipeline) -> None:
    """概観は ``max_tokens`` に依らない大きさで、予算が足りないときだけ先頭のレコードの縮退で行を外す。予算を上げて
    見えていた概観の行・ファイルが消えることはない（仕様書 §5.6 規則 5。BUG-0067・BUG-0120）。"""
    prev_ov: set[str] = set()
    prev_files: list[str] = []
    for max_tokens in range(180, 901, 15):
        r = await repo_map(pipeline, "compact", max_tokens=max_tokens)
        assert g.validate(r.text) == [], (max_tokens, r.text)
        ov = set(_overview_lines(r.text))
        files = re.findall(r"^=== (\S+) \(", r.text, flags=re.M)
        assert prev_ov <= ov, (max_tokens, prev_ov - ov)
        assert files[: len(prev_files)] == prev_files, (max_tokens, prev_files, files)
        prev_ov, prev_files = ov, files
    assert any(x.startswith("- entry modules: ") for x in prev_ov)


async def test_include_signatures_false_drops_parameters(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, "compact", max_tokens=3000, include_signatures=False)
    line = next(x for x in r.text.splitlines() if "process_charge" in x)
    assert "(" not in line and "->" not in line


async def test_json_tree_lists_directories_with_counts(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, max_tokens=4000)
    tree = {t["dir"]: t for t in r.structured["data"]["tree"]}
    assert tree["src/"]["file_count"] >= 7 and tree["src/"]["line_count"] > 0
    assert "./" in tree and "conf/" in tree
    assert sum(t["file_count"] for t in tree.values()) == r.structured["page"]["total_estimate"]


# --- 絞り込み ---------------------------------------------------------------------------------


async def test_include_tests_default_false(pipeline: Pipeline) -> None:
    off = await repo_map(pipeline, max_tokens=8000)
    on = await repo_map(pipeline, max_tokens=8000, include_tests=True)
    assert "tests/test_payment.py" not in paths_of(off)
    assert "tests/test_payment.py" in paths_of(on)
    assert on.structured["page"]["total_estimate"] == off.structured["page"]["total_estimate"] + 1


async def test_scope_paths_directory_and_glob(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, max_tokens=8000, scope_paths=["src/shop/services"])
    assert set(paths_of(r)) == {
        "src/shop/services/__init__.py",
        LIMITS,
        PAY,
    }
    assert r.structured["page"]["total_estimate"] == 3
    r2 = await repo_map(pipeline, max_tokens=8000, scope_paths=["web/**/*.ts"])
    assert set(paths_of(r2)) == {"web/src/app.ts", "web/src/payment.ts"}
    r3 = await repo_map(
        pipeline, "compact", max_tokens=2000, scope_paths=["src/shop/services", "web/src/app.ts"]
    )
    assert g.validate(r3.text) == []


async def test_scope_outside_root_is_invalid_argument(pipeline: Pipeline) -> None:
    for bad in ("../outside", "/etc", "..\\x"):
        r = await repo_map(pipeline, scope_paths=[bad])
        assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT", (bad, r.text)


async def test_max_symbols_per_file_out_of_range_is_invalid_argument(pipeline: Pipeline) -> None:
    for bad in (0, 51, -1):
        r = await repo_map(pipeline, max_symbols_per_file=bad)
        assert r.is_error and r.structured["error"]["code"] == "E_INVALID_ARGUMENT", (bad, r.text)


async def test_non_working_tree_revision_is_unsupported(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, revision="HEAD")
    assert r.is_error and r.structured["error"]["code"] == "E_UNSUPPORTED"


# --- 内容を出さない -----------------------------------------------------------------------------


async def test_secret_file_content_is_never_shown(pipeline: Pipeline) -> None:
    for fmt in ("compact", "json"):
        r = await repo_map(pipeline, fmt, max_tokens=20000, include_tests=True)
        assert SECRET_VALUE not in r.text and "API_KEY" not in r.text
        if fmt == "compact":
            block = (
                r.text.split("=== conf/.env ", 1)[1].split("===", 1)[0] if "=== conf/.env " in r.text else ""
            )
            assert "SECRET" not in block
    env = next(
        (f for f in files_of(await repo_map(pipeline, max_tokens=20000)) if f["path"] == "conf/.env"), None
    )
    assert env is not None and env["symbols"] == []  # ファイル名と行数だけ


async def test_tier4_heuristic_symbols_carry_conf_and_secret_is_header_only(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, "compact", max_tokens=20000)
    assert g.validate(r.text) == []
    cob = r.text.split("=== legacy/calc.cob ", 1)
    if len(cob) == 2:  # ctags があるとき: 推定のシンボルは conf: heuristic つき
        body = cob[1].split("=== ", 1)[0]
        if "[Section]" in body or "[Function]" in body:
            assert "conf: heuristic" in body


# --- ページング -------------------------------------------------------------------------------


async def test_cursor_walks_all_files_exactly_once(pipeline: Pipeline) -> None:
    full = paths_of(await repo_map(pipeline, max_tokens=20000))
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(60):
        args: dict[str, Any] = {"max_tokens": 800}
        if cursor:
            args["cursor"] = cursor
        r = await repo_map(pipeline, **args)
        assert not r.is_error, r.text
        seen += paths_of(r)
        cursor = r.structured["page"].get("next_cursor")
        if not cursor:
            break
    assert seen == full  # 重複も取りこぼしも無い


async def test_summary_line_counts_remaining_files_by_directory(pipeline: Pipeline) -> None:
    r = await repo_map(pipeline, "compact", max_tokens=300)
    assert g.validate(r.text) == [], r.text
    summary = next(line for line in r.text.splitlines() if line.startswith("# summary: "))
    m = re.match(r"# summary: (\d+) more files \((.+)\)$", summary)
    assert m, summary
    shown = r.text.count("\n=== ") + (1 if r.text.startswith("=== ") else 0)
    total = (await repo_map(pipeline, max_tokens=300)).structured["page"]["total_estimate"]
    assert int(m.group(1)) == total - shown
    assert sum(int(x.split(": ")[1]) for x in m.group(2).split(", ")) == int(m.group(1))
    assert re.search(r"# more: cursor=\S+ remaining≈\d+", r.text)


# --- グラフ未準備 -----------------------------------------------------------------------------


async def test_graph_not_ready_degrades_to_alpha(indexed: tuple[Path, Path], tmp_path: Path) -> None:
    root, db = indexed
    copy = tmp_path / "paad.sqlite"
    shutil.copy(db, copy)
    for suffix in ("-wal", "-shm"):
        if Path(str(db) + suffix).exists():
            shutil.copy(str(db) + suffix, str(copy) + suffix)
    conn = sqlite3.connect(copy)
    conn.execute("DELETE FROM _symbol_rank")
    conn.execute("DELETE FROM relations")
    conn.commit()
    conn.close()
    p = _pipeline(root, copy)
    r = await p.call_tool("get_repo_map", {"format": "json", "max_tokens": 4000})
    assert not r.is_error, r.text
    assert r.structured["data"]["ranking_used"] == "alpha"
    assert "ranking=alpha (graph not ready)" in r.structured["warnings"]
    assert paths_of(r) == sorted(paths_of(r))
    c = await p.call_tool("get_repo_map", {"format": "compact", "max_tokens": 4000})
    assert "! ranking=alpha (graph not ready)" in c.text and g.validate(c.text) == []
    explicit = await p.call_tool("get_repo_map", {"format": "json", "ranking": "alpha"})
    assert "ranking=alpha (graph not ready)" not in explicit.structured.get("warnings", [])


async def test_database_without_symbol_rank_table_still_answers(
    indexed: tuple[Path, Path], tmp_path: Path
) -> None:
    """v1 の DB（``_symbol_rank`` が無い）でも、alpha で答える。"""
    root, db = indexed
    copy = tmp_path / "old.sqlite"
    shutil.copy(db, copy)
    for suffix in ("-wal", "-shm"):
        if Path(str(db) + suffix).exists():
            shutil.copy(str(db) + suffix, str(copy) + suffix)
    conn = sqlite3.connect(copy)
    conn.execute("DROP TABLE _symbol_rank")
    conn.commit()
    conn.close()
    r = await _pipeline(root, copy).call_tool("get_repo_map", {"format": "json", "max_tokens": 4000})
    assert not r.is_error, r.text
    assert r.structured["data"]["ranking_used"] == "alpha"


def test_scan_wrote_the_ranks(indexed: tuple[Path, Path]) -> None:
    _, db = indexed
    pool = ReaderPool(db, size=1)
    try:
        with pool.reader() as conn:
            assert rank.ranked_symbol_count(conn, WS_ID) > 0
            assert conn.execute("SELECT MAX(pagerank_score) FROM files").fetchone()[0] == 1.0
    finally:
        pool.close()
