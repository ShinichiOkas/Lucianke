"""BUG-0047: 参照元になりうるファイルを LSP に開かせる部品（``prime_candidates`` / ``features.open_documents``）。偽の入れ物で確かめる。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.model import Confidence, FileInfo, RelPath, Tier
from lucianke.lsp import features
from lucianke.lsp.pool import LspState
from lucianke.tools import _lsp_exact as lx
from lucianke.workspace.types import Workspace


def info(path: str, *, language: str = "typescript", tier: Tier = Tier.T1, test: bool = False) -> FileInfo:
    return FileInfo(
        file_id=1,
        workspace_id="w",
        path=RelPath(path),
        language=language,
        tier=tier,
        sha256="",
        line_count=1,
        byte_size=1,
        is_test=test,
        confidence=Confidence.STRUCTURAL,
    )


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    files = {
        "source/guard.ts": "export function isThing() {}",
        "source/use.ts": "import {isThing} from './guard'; isThing()",
        "source/other.ts": "const isThingy = 1; const x_isThing = 2; const $isThing = 3;",  # 単語境界の外
        "test/a.test.ts": "isThing()",
        "test/b.test.tsx": "<div>{isThing()}</div>",
        "test/c.test.ts": "// nothing",
        "scripts/run.js": "isThing()",
        "README.md": "isThing",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Workspace(workspace_id="w", root=tmp_path.resolve())


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> list[FileInfo]:
    files = [
        info("source/guard.ts"),
        info("source/use.ts"),
        info("source/other.ts"),
        info("test/a.test.ts", test=True),
        info("test/b.test.tsx", language="tsx", test=True),
        info("test/c.test.ts", test=True),
        info("scripts/run.js", language="javascript", tier=Tier.T2),
        info("README.md", language="markdown", tier=Tier.T3),
    ]
    monkeypatch.setattr(lx.queries, "files_in_scope", lambda conn, ws_id, globs=(), **kw: list(files))
    return files


def test_candidates_are_ts_family_files_that_mention_the_name_tests_first(
    ws: Workspace, listing: list[FileInfo]
) -> None:
    got = lx.prime_candidates(None, ws, name="isThing", exclude="source/guard.ts", is_open=lambda rel: False)  # type: ignore[arg-type]
    assert [(str(p), lid) for p, lid in got.files] == [
        ("test/a.test.ts", "typescript"),
        ("test/b.test.tsx", "typescriptreact"),  # tsx は languageId を typescriptreact にして開く
        ("scripts/run.js", "javascript"),
        ("source/use.ts", "typescript"),
    ]
    assert got.skipped == 0 and got.warning("isThing") is None


def test_candidates_skip_files_the_lsp_already_has_open(ws: Workspace, listing: list[FileInfo]) -> None:
    got = lx.prime_candidates(
        None,  # type: ignore[arg-type]
        ws,
        name="isThing",
        exclude="source/guard.ts",
        is_open=lambda rel: str(rel) == "test/a.test.ts",
    )
    assert "test/a.test.ts" not in [str(p) for p, _ in got.files]


def test_candidates_are_capped_and_the_overflow_is_reported(ws: Workspace, listing: list[FileInfo]) -> None:
    got = lx.prime_candidates(
        None,
        ws,
        name="isThing",
        exclude="source/guard.ts",
        is_open=lambda rel: False,
        limit=2,  # type: ignore[arg-type]
    )
    assert [str(p) for p, _ in got.files] == ["test/a.test.ts", "test/b.test.tsx"]
    assert got.skipped == 2
    assert "2 more file(s) mention 'isThing'" in (got.warning("isThing") or "")


def test_priming_applies_to_the_ts_family_only() -> None:
    assert all(lx.needs_priming(lang) for lang in ("typescript", "tsx", "javascript"))
    assert not any(lx.needs_priming(lang) for lang in ("python", "rust", "go", "c_sharp"))


class FakeDocs:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    def is_open(self, rel: RelPath) -> bool:
        return any(r == rel for r, _ in self.opened)

    def did_open(self, rel: RelPath, text: str, language_id: str) -> int:
        self.opened.append((str(rel), language_id))
        return 1


class FakeClient:
    is_connected = True

    def on_request(self, *args: object, **kwargs: object) -> None:
        return None


def fake_handle(ws: Workspace, state: LspState = LspState.READY) -> SimpleNamespace:
    client = FakeClient()
    return SimpleNamespace(
        ws=ws,
        state=state,
        client=client,
        documents=FakeDocs(),
        touch=lambda: None,
        server_name="fake",
        language_id="typescript",
    )


async def test_open_documents_opens_each_file_once_and_skips_unreadable_ones(ws: Workspace) -> None:
    handle = fake_handle(ws)
    files = [
        (RelPath("test/a.test.ts"), "typescript"),
        (RelPath("missing.ts"), "typescript"),
        (RelPath("test/b.test.tsx"), "typescriptreact"),
    ]
    assert await features.open_documents(handle, files) == 2  # type: ignore[arg-type]
    assert handle.documents.opened == [
        ("test/a.test.ts", "typescript"),
        ("test/b.test.tsx", "typescriptreact"),
    ]
    assert await features.open_documents(handle, files) == 0  # type: ignore[arg-type]  開いたものは二度開かない


async def test_open_documents_needs_a_ready_lsp(ws: Workspace) -> None:
    handle = fake_handle(ws, LspState.STARTING)
    with pytest.raises(LuciankeError) as exc:
        await features.open_documents(handle, [(RelPath("test/a.test.ts"), "typescript")])  # type: ignore[arg-type]
    assert exc.value.code is ErrorCode.E_LSP_UNAVAILABLE and exc.value.details.get("reason") == "indexing"
    assert handle.documents.opened == []
