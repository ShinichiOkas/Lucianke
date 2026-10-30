"""F-PY（httpx）・F-TS（ky）をワークスペースにして、結線済みのサーバーをプロセス内に組む土台（BUG-0036、M1-1）。

* フィクスチャの原本は ``bench/.cache/<name>/``（``bench/fetch_fixtures.py`` が SHA 固定で取得する）。**原本には触れない**:
  試験は ``tmp_path`` に複製（``.git`` と ``node_modules`` を除く）した作業ツリーに対して走る。編集系も複製に対して行う。
* 結線は本番の合成ルート（``app.run_stdio``）と同じ: ``RuntimeHub`` + ``LspPool(on_diagnostics=hub.on_diagnostics)`` +
  ``WiredServices`` + ``Pipeline``（全ツールを ``register_tools`` で登録）。索引（差分索引の背景ジョブ）の完了を待ってから渡す。
* LSP は ``.venv`` の basedpyright と ruff（Python）。TypeScript の LSP（typescript-language-server / vtsls）は、見つかれば使い、
  無ければ構文層（Tree-sitter）の降格応答で事後条件を確かめる（``Rig.lsp_ready``）。
* 取得されていなければ ``FixtureMissing``。``conftest.py`` の ``fetched_fixtures`` が取得しにいき、失敗なら赤にする（BUG-0062）。
  ``LUCIANKE_NO_FIXTURE_FETCH=1`` のときだけ、これを理由（NOT CHECKED）に skip する。
* exact 水準の事後条件（BUG-0080）: ``require_exact_lsp`` が前提を確かめる。skip するのは型解決の LSP がこの環境に **入っていない**
  ときだけ（理由は事実どおり）。入っているのに ready でなければ fail。``ask_exact`` は exact の答えを得るまで聞き直し、得られなければ
  fail にする（負荷で 1 問い合わせの LSP の予算を使い切って ``structural`` に降格しても、検査を黙って外さない）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
import tomllib
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _t11_harness import snapshot  # tests/contract の共通部品（conftest が sys.path に足す）

from lucianke.core.model import LspStatus, RelPath
from lucianke.core.procs import ProcessRegistry
from lucianke.lsp import features
from lucianke.lsp.pool import LspPool
from lucianke.lsp.servers import LspServerDef, find_command, servers_for
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.mcp.render import compact
from lucianke.syntax.parsers import TreeCache
from lucianke.tools import _lsp_exact, register_tools
from lucianke.tools._runtime import RuntimeHub, WiredServices
from lucianke.workspace.types import Workspace

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "bench"
CACHE = BENCH / ".cache"
FIXTURES_TOML = BENCH / "fixtures.toml"

# 索引・LSP の準備を待つ上限。条件待ち（完了したら即戻る）なので、共有の作業ツリーで CPU が飽和していても足りる長さ。
INDEX_WAIT_S = 300.0
READY_WAIT_MS = 120_000
FIRST_ANALYSIS_TIMEOUT_MS = 120_000

COPY_IGNORE = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache")

# exact の事後条件の答え・真値を取る間だけ使う期限（BUG-0080）。製品の既定（``LSP_TIMEOUT_*``・``EXACT_BUDGET_S``）は変えない。
PATIENT_REQUEST_MS = 120_000
PATIENT_BUDGET_S = 180.0
PATIENT_TARGETS: tuple[tuple[Any, str, float], ...] = (
    (features, "LSP_TIMEOUT_DEFINITION_MS", PATIENT_REQUEST_MS),
    (features, "LSP_TIMEOUT_REFERENCES_MS", PATIENT_REQUEST_MS),
    (features, "LSP_TIMEOUT_HOVER_MS", PATIENT_REQUEST_MS),
    (features, "LSP_TIMEOUT_CALL_HIERARCHY_MS", PATIENT_REQUEST_MS),
    (_lsp_exact, "EXACT_BUDGET_S", PATIENT_BUDGET_S),
)
EXACT_RETRIES = 3
"""製品の期限で降格したあと、延ばした期限で聞き直す回数の上限（LSP が 0 件と答えた ``indexing`` の降格も聞き直しで抜ける）。"""

LANGUAGE_LABEL = {"python": "Python", "typescript": "TypeScript"}


class FixtureMissing(Exception):
    """取得済みのフィクスチャが無い（``bench/fetch_fixtures.py`` を走らせていない）。"""


def fixture_spec(key: str) -> dict[str, Any]:
    data = tomllib.loads(FIXTURES_TOML.read_text(encoding="utf-8"))
    return data[key]


def fixture_dir(key: str) -> Path:
    """取得済みの原本の場所。無い・SHA が違うときは ``FixtureMissing``（理由つき）。"""
    spec = fixture_spec(key)
    d = CACHE / str(spec["name"])
    if not (d / ".git").exists():
        raise FixtureMissing(
            f"{key} ({spec['name']}) is not fetched: run `uv run --no-project python bench/fetch_fixtures.py`"
        )
    try:
        proc = subprocess.run(  # noqa: S603  固定の引数列
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            cwd=d,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FixtureMissing(f"{key}: cannot read the fixture HEAD ({exc})") from exc
    head = proc.stdout.strip()
    if proc.returncode != 0 or not head:
        # git init の後で取得が失敗した残骸（コミットが無い）
        raise FixtureMissing(
            f"{key} ({spec['name']}) is not fetched (incomplete clone, no commit): "
            "run `uv run --no-project python bench/fetch_fixtures.py`"
        )
    if head != spec["sha"]:
        raise FixtureMissing(
            f"{key} ({spec['name']}) is at {head[:12]}, fixtures.toml pins {str(spec['sha'])[:12]}"
        )
    return d


def missing_reason(key: str) -> str | None:
    """``None`` なら取得済み。そうでなければ skip の理由。"""
    try:
        fixture_dir(key)
    except FixtureMissing as exc:
        return str(exc)
    return None


def _lsp_command(defn: LspServerDef, root: Path) -> list[str] | None:
    return find_command(defn, workspace_root=root, venv=Path(sys.prefix))


def available_servers(language_id: str, root: Path) -> list[LspServerDef]:
    """``language_id`` の定義のうち、この環境で起動コマンドが見つかるもの（役ごとに先頭 1 つ）。"""
    out: list[LspServerDef] = []
    seen_roles: set[str] = set()
    for defn in servers_for(language_id):
        if defn.role in seen_roles:
            continue
        if _lsp_command(defn, root) is not None:
            out.append(defn)
            seen_roles.add(defn.role)
    return out


@dataclass(slots=True)
class Rig:
    key: str  # "F-PY" | "F-TS"
    language: str  # "python" | "typescript"
    root: Path  # 複製した作業ツリー（実パス）
    ws: Workspace
    hub: RuntimeHub
    pool: LspPool
    pipeline: Pipeline
    procs: ProcessRegistry
    types_ready: bool = False
    lint_ready: bool = False
    servers: list[str] = field(default_factory=list)
    types_server: str | None = None
    """この環境で起動コマンドが見つかった型解決の LSP の名前。``None`` は入っていない（探した名前は ``types_candidates``）。"""
    types_candidates: list[str] = field(default_factory=list)

    @property
    def lsp_ready(self) -> bool:
        """型解決の LSP が ready（exact の応答を期待できる）。"""
        return self.types_ready

    async def call(self, tool: str, fmt: str = "json", **args: Any) -> Any:
        return await self.pipeline.call_tool(tool, {"format": fmt, **args})

    async def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        """成功を期待して呼び、``structured`` を返す（失敗は本文つきで assert）。"""
        r = await self.call(tool, **args)
        assert not r.is_error, f"{tool} {args}: {r.text[:800]}"
        return r.structured

    def read(self, rel: str) -> str:
        return (self.root / rel).read_bytes().decode("utf-8")

    def snapshot(self) -> dict[str, str]:
        return snapshot(self.root)


def copy_fixture(key: str, dst_parent: Path) -> Path:
    src = fixture_dir(key)
    dst = dst_parent / str(fixture_spec(key)["name"])
    shutil.copytree(src, dst, ignore=COPY_IGNORE)
    return Path(os.path.realpath(dst))


async def open_rig(key: str, tmp_path: Path) -> Rig:
    """``key`` の複製を ``tmp_path`` に作り、結線済みのサーバーを組んで、索引の完了と LSP の ready まで待つ。"""
    spec = fixture_spec(key)
    root = copy_fixture(key, tmp_path)
    language = str(spec["language"])
    procs = ProcessRegistry()
    hub = RuntimeHub(tmp_path / "cache", TreeCache())
    servers = available_servers(language, root)
    pool = LspPool(
        procs,
        on_diagnostics=hub.on_diagnostics,
        servers=servers,
        resolver=lambda defn, _ws: _lsp_command(defn, root),
        ready_fallback_s=1.0,
        grace_ms=300,
    )
    hub.lsp_pool = pool
    try:
        (ws,) = await hub.sync_roots([str(root)])
        runtime = hub.runtime()
        assert runtime is not None and runtime.scan_job_id is not None, hub.failed
        await asyncio.wait_for(runtime.indexer.wait_job(runtime.scan_job_id), INDEX_WAIT_S)
        registry = ToolRegistry()
        register_tools(registry)
        services = WiredServices(roots=lambda: [str(root)], trees=hub.trees, lines=compact, hub=hub)
        rig = Rig(
            key, language, root, ws, hub, pool, Pipeline(registry, services=services), procs,
            servers=[s.name for s in servers],
            types_server=next((s.name for s in servers if s.role == "types"), None),
            types_candidates=[d.name for d in servers_for(language, "types")],
        )  # fmt: skip
        for defn in servers:
            ok = False
            try:
                await pool.ensure_started(defn.language_id, ws)
                ok = await pool.wait_ready(defn.language_id, ws, READY_WAIT_MS, role=defn.role)
            except Exception:  # noqa: BLE001  起動できないサーバーは「LSP なし」として降格応答で確かめる
                ok = False
            if defn.role == "types":
                rig.types_ready = ok
            else:
                rig.lint_ready = ok
    except BaseException:
        # 索引の待ちの時間切れなどで組み立てが途中で落ちても、書き手のスレッド・LSP を残さない（BUG-0076）
        await _close_parts(pool, hub, procs)
        raise
    return rig


async def _close_parts(pool: LspPool, hub: RuntimeHub, procs: ProcessRegistry) -> None:
    """LSP・監視・索引器・保存先・子プロセスを閉じる。どれかが失敗しても残りは必ず閉じる（BUG-0076）。"""
    async with contextlib.AsyncExitStack() as stack:
        stack.push_async_callback(procs.terminate_all, 200, close=True)
        stack.push_async_callback(hub.close_storage)
        stack.push_async_callback(hub.stop_indexers)
        stack.push_async_callback(hub.stop_watchers)
        stack.push_async_callback(pool.shutdown_all, 300)


async def close_rig(rig: Rig) -> None:
    await _close_parts(rig.pool, rig.hub, rig.procs)


async def warm_up(rig: Rig, rels: list[str]) -> None:
    """型解決の LSP に文書を開かせ、最初の診断（= 解析の完了の合図）が届くまで待つ（BUG-0012 と同じ。固定 sleep なし）。"""
    handle = rig.pool.get(rig.language, rig.ws)
    if handle is None or not rig.types_ready:
        return
    for rel in rels:
        handle.documents.sync(RelPath(rel), rig.read(rel), rig.language)
    for rel in rels:
        sent_at = handle.documents.sent_at_of(RelPath(rel))
        if sent_at is not None:
            await handle.diagnostics.wait_for(
                RelPath(rel), min_version=1, sent_at=sent_at, timeout_ms=FIRST_ANALYSIS_TIMEOUT_MS
            )


# --- exact 水準の事後条件（BUG-0080） ------------------------------------------------------------------


def require_exact_lsp(rig: Rig, what: str) -> None:
    """exact 水準の事後条件 ``what`` を確かめる前提（型解決の LSP が ready）。満たさなければ skip か fail で、戻らない。

    skip するのは、その言語の型解決の LSP がこの環境に **入っていない** ときだけ（この環境の F-TS: TypeScript の LSP が無い）。
    理由はそのとおりに書く。入っているのに ready にならなかったのは fail（skip にすると、検査していないのに
    「passed + skipped」に見え、しかも理由が事実と違う）。
    """
    if rig.lsp_ready:
        return
    label = LANGUAGE_LABEL.get(rig.language, rig.language)
    if rig.types_server is None:
        pytest.skip(
            reason=f"no {label} LSP installed ({' / '.join(rig.types_candidates) or 'no server defined'} not found): "
            f"{rig.key} exact postconditions unverified ({what})"
        )
    pytest.fail(
        f"{rig.key}: the {label} LSP {rig.types_server} is installed but did not become ready within "
        f"{READY_WAIT_MS} ms, so the exact postconditions cannot be checked ({what})",
        pytrace=False,
    )


@contextmanager
def patient_lsp() -> Iterator[None]:
    """この間だけ、LSP の要求の期限と T-07 の 1 問い合わせの予算を延ばす（``PATIENT_TARGETS``。製品の既定は変えない）。

    exact の答えの中身は期限に依らない（LSP の答えそのもの）。期限で変わるのは「exact で答えきれたか」だけなので、
    exact の事後条件を確かめるための問い合わせと、試験が LSP に直に聞く真値にだけ使う。真値を製品の期限で聞くと、
    負荷で切れたときに試験が落ちるうえ、その時間切れが ``LspHandle`` の連続時間切れ（3 回で ``unavailable``）に数えられる。
    試験は 1 つのイベントループで順に問い合わせるので、モジュールの値を差し替えて戻すだけでよい。
    """
    saved = [(mod, name, getattr(mod, name)) for mod, name, _ in PATIENT_TARGETS]
    for mod, name, value in PATIENT_TARGETS:
        setattr(mod, name, value)
    try:
        yield
    finally:
        for mod, name, value in saved:
            setattr(mod, name, value)


def is_exact(r: dict[str, Any]) -> bool:
    return r["_resolution_meta"]["confidence_level"] == "exact"


async def ask_exact(
    rig: Rig,
    tool: str,
    *,
    what: str,
    exact: Callable[[dict[str, Any]], bool] = is_exact,
    product_first: bool = True,
    **args: Any,
) -> dict[str, Any]:
    """exact 水準の事後条件 ``what`` を確かめるための答えを得る（BUG-0080）。戻り値は ``exact(r)`` が真の ``structured``。

    1. 前提を確かめる（``require_exact_lsp``: LSP が入っていなければ事実どおりの理由で skip、入っていて ready でなければ fail）。
    2. 製品の期限のまま聞く（``product_first``）。exact ならそれを返す。
    3. 降格したら（負荷の高いときや取得直後の 1 回目は、1 問い合わせの LSP の予算 ``EXACT_BUDGET_S``・要求の期限を使い切る）、
       LSP が ready のままか確かめてから、期限を延ばして（``patient_lsp``）聞き直す（最大 ``EXACT_RETRIES`` 回）。
    4. それでも exact でなければ **fail**（skip しない）。各回の水準・エンジン・``lsp_status``・警告・所要を書く。

    ``product_first=False`` は最初から延ばした期限で聞く。製品の期限で要求が取り消されると、同じ LSP のプロセスでその後の
    同じ種類の要求が失敗し続ける組み合わせ（basedpyright の references。票 BUG-0083）に使う。
    """
    require_exact_lsp(rig, what)
    tries: list[str] = []
    for attempt in range(1 + EXACT_RETRIES):
        st = rig.pool.status(rig.language, rig.ws, "types")
        if st.status is not LspStatus.READY:
            pytest.fail(
                f"{what}: the {rig.types_server} LSP is {st.status.value} (reason: {st.reason}), so {tool} cannot "
                f"answer exact. Tries: {'; '.join(tries) or 'none'}",
                pytrace=False,
            )
        product = attempt == 0 and product_first
        started = time.monotonic()
        if product:
            r = await rig.ok(tool, **args)
        else:
            with patient_lsp():
                r = await rig.ok(tool, **args)
        if exact(r):
            if tries:  # 製品の期限では降格した（検査はした。降格したことは隠さない: 票 BUG-0084）
                warnings.warn(
                    f"{rig.key} {what}: exact only after a patient re-ask; {'; '.join(tries)}", stacklevel=2
                )
            return r
        m = r["_resolution_meta"]
        tries.append(
            f"#{attempt + 1} ({'product' if product else 'patient'} budgets, {time.monotonic() - started:.1f}s): "
            f"confidence={m['confidence_level']} engine={m['engine_used']} lsp_status={m['lsp_status']} "
            f"warning={m.get('accuracy_warning')!r}"
        )
    pytest.fail(
        f"{what}: the {rig.types_server} LSP is ready but {tool} did not answer exact after {len(tries)} tries: "
        f"{'; '.join(tries)}",
        pytrace=False,
    )
