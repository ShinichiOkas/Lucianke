"""T-10 ``replace_body`` に、複数行のデコレータ・属性を持つ定義全体を渡す契約テスト（BUG-0045、仕様書 T-10 処理規約 1・2・3、EP-6）。

定義の範囲はデコレータ・属性を含む（§10.8）ので、``new_code`` も付帯要素ごと渡す。2 行目以降が ``("a", "b"),`` や ``Debug,`` の
ように見える複数行のデコレータ・属性を、行頭の文字で見分けて「定義で始まらない」と拒否してはならない
（入れ子の括弧・文字列の中の括弧・コメント混じりも通る）。本体の文だけを渡したときの拒否は変わらない。

パイプライン経由で呼ぶ。``tests/fixtures/poly`` を ``tmp_path`` に写し、対象のファイルを書き足してから編集する。
置換と同じ入口・出口の検査は ``test_T10_edit_body.py`` が受け持つ。Rust の属性はシンボル抽出が未対応（``E_UNSUPPORTED``）で
ここでは通せないので、``tests/unit/test_edit_syntax_check.py`` の ``starts_with_definition`` が受け持つ。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_T10_edit_body import (
    EditServices,
    apply_unified_diff,
    data_of,
    edit,
    error_node_count,
    error_of,
    sha,
    snapshot,
)

from lucianke.app import build_services
from lucianke.edit.lock import WorkspaceLocks
from lucianke.index.writer import IndexWriter
from lucianke.mcp.pipeline import Pipeline
from lucianke.mcp.registry import ToolRegistry
from lucianke.tools import register_tools

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"
PY = "src/shop/decorated.py"
TS = "web/src/decorated.ts"


# --- 土台 ---------------------------------------------------------------------------------


@pytest.fixture
async def index_writer(tmp_path: Path) -> AsyncIterator[IndexWriter]:
    w = IndexWriter(tmp_path / "cache" / "paad.sqlite")
    try:
        yield w
    finally:
        await asyncio.to_thread(w.close)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    dst = tmp_path / "poly"
    shutil.copytree(POLY, dst)
    return Path(os.path.realpath(dst))


@pytest.fixture
def pipeline(root: Path, tmp_path: Path, index_writer: IndexWriter) -> Pipeline:
    base = build_services(lambda: [str(root)])
    services = EditServices(
        roots=base.roots,
        trees=base.trees,
        lines=base.lines,
        locks=WorkspaceLocks(),
        writer_index=index_writer,
        journal_dir=tmp_path / "cache" / "journal",
    )
    registry = ToolRegistry()
    register_tools(registry)
    return Pipeline(registry, services=services)


def put(root: Path, rel: str, text: str) -> bytes:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return data


def replace(pipeline: Pipeline, rel: str, name: str, new_code: str, **kw: Any):
    return edit(
        pipeline, target={"path": rel, "symbol_name": name}, operation="replace_body", new_code=new_code, **kw
    )


# --- Python -------------------------------------------------------------------------------

# 1 行目だけがデコレータに見えて、2 行目以降は式の続きになっている定義（httpx の tests/test_utils.py の形）
PARAMETRIZE = """\
@pytest.mark.parametrize(
    ("encoding", "expected"),
    (
        ("utf-8", "a)b"),
        ("latin-1", "(x"),
    ),
)
def test_multi(encoding, expected):
    assert encoding
    return expected
"""
# コメント混じり（行の途中・行全体・閉じ括弧を含むコメント）と、積み重ねたデコレータ、async
COMMENTED = """\
@decorator(  # open ( here
    arg=[1, 2,
         3],
    # a whole-line comment )
    other={"k": (1, 2)},  # trailing ) comment
)
@second
async def commented(a):
    return a
"""
DECORATED_CLASS = """\
@dataclass_like(
    frozen=True,
    slots=(
        True
    ),
)
class Decorated:
    value: int = 0
"""
# メソッド（クラスの中の字下げされた定義）
HOLDER_METHOD = """\
    @pytest.mark.parametrize(
        ("a", "b"),
        [(1, 2), (3, 4)],
    )
    def method(self, a, b):
        return a + b
"""
PY_SOURCE = f"""\
import pytest


{PARAMETRIZE}

{COMMENTED}

{DECORATED_CLASS}

class Holder:
    x = 1

{HOLDER_METHOD}
    def other(self):
        return 2
"""

PY_DEFINITIONS = [
    pytest.param("test_multi", PARAMETRIZE, id="parametrize"),
    pytest.param("commented", COMMENTED, id="comments-in-decorator"),
    pytest.param("Decorated", DECORATED_CLASS, id="decorated-class"),
    pytest.param("Holder.method", HOLDER_METHOD, id="indented-method"),
    pytest.param("Holder.method", "".join(x[4:] for x in HOLDER_METHOD.splitlines(keepends=True)), id="dedented-method"),
]  # fmt: skip


@pytest.mark.parametrize("name, code", PY_DEFINITIONS)
async def test_ep6_identity_replace_of_a_multiline_decorated_python_definition_keeps_every_byte(
    pipeline: Pipeline, root: Path, name: str, code: str
) -> None:
    before = put(root, PY, PY_SOURCE)
    mtime = (root / PY).stat().st_mtime_ns
    data = data_of(await replace(pipeline, PY, name, code))
    assert data["applied"] is True and data["diff"] == "" and data["edit_id"] is None
    assert data["files_changed"][0]["sha256_after"] == sha(before)
    assert (root / PY).read_bytes() == before and (root / PY).stat().st_mtime_ns == mtime


@pytest.mark.parametrize(
    "name, code, old, new",
    [
        ("test_multi", PARAMETRIZE, "    assert encoding\n", "    assert expected\n"),
        ("commented", COMMENTED, "    arg=[1, 2,\n         3],\n", "    arg=[4],\n"),  # デコレータ側を変える
        ("Decorated", DECORATED_CLASS, "    value: int = 0\n", "    value: int = 1\n"),
        ("Holder.method", HOLDER_METHOD, "return a + b", "return a * b"),
    ],
)
async def test_replace_with_a_changed_body_or_decorator_touches_only_that_part(
    pipeline: Pipeline, root: Path, name: str, code: str, old: str, new: str
) -> None:
    before = put(root, PY, PY_SOURCE)
    assert old in code and PY_SOURCE.count(old) == 1
    data = data_of(await replace(pipeline, PY, name, code.replace(old, new)))
    after = (root / PY).read_bytes()
    assert data["applied"] is True and data["syntax_check"] == "ok"
    assert after == PY_SOURCE.replace(old, new).encode()
    assert error_node_count(after) == 0
    assert data["files_changed"][0]["sha256_after"] == sha(after)
    assert apply_unified_diff(before, data["diff"]) == after


async def test_a_multiline_decorator_with_crlf_in_new_code_still_matches_the_definition(
    pipeline: Pipeline, root: Path
) -> None:
    before = put(root, PY, PY_SOURCE)
    data = data_of(await replace(pipeline, PY, "test_multi", PARAMETRIZE.replace("\n", "\r\n")))
    assert data["diff"] == "" and (root / PY).read_bytes() == before  # 改行は対象ファイルに合わせて直る


@pytest.mark.parametrize(
    "new_code",
    [
        # 本体の文だけ（複数行のデコレータは付けても、定義が無い）
        "return a + b\n",
        "x = (\n    1,\n    2,\n)\n",
        # 複数行のデコレータだけで定義が続かない
        "@pytest.mark.parametrize(\n    ('a', 'b'),\n    [(1, 2)],\n)\n",
        # デコレータの後に定義でない文
        "@dec(\n    1,\n)\nx = 1\n",
        # 複数行のデコレータの括弧が閉じていない
        "@pytest.mark.parametrize(\n    ('a', 'b'),\ndef method(self, a, b):\n    return a\n",
    ],
)
async def test_python_new_code_that_is_not_a_whole_definition_is_still_refused(
    pipeline: Pipeline, root: Path, new_code: str
) -> None:
    put(root, PY, PY_SOURCE)
    before = snapshot(root)
    err = error_of(await replace(pipeline, PY, "Holder.method", new_code))
    assert err["code"] == "E_SYNTAX_INVALID"
    assert "シグネチャを含む定義全体" in err["details"]["hint"]
    assert snapshot(root) == before


async def test_body_only_python_new_code_says_it_does_not_start_with_a_definition(
    pipeline: Pipeline, root: Path
) -> None:
    put(root, PY, PY_SOURCE)
    err = error_of(await replace(pipeline, PY, "Holder.method", "# note\nreturn a + b\n"))
    assert err["code"] == "E_SYNTAX_INVALID" and "does not start with a definition" in err["message"]


# --- TypeScript -------------------------------------------------------------------------------

TS_CLASS = """\
@Component({
  selector: 'app-decorated',
  template: `<p>{{ title }}</p>`,
  styles: [
    ':host { display: block; }', // trailing ) comment
  ],
})
export class DecoratedComponent {
  @Input({
    required: true,
    alias: 'ttl',
  })
  title!: string;

  @HostListener('click', [
    '$event',
  ])
  onClick(event: Event): void {
    console.log(event);
  }
}
"""
TS_METHOD = """\
  @HostListener('click', [
    '$event',
  ])
  onClick(event: Event): void {
    console.log(event);
  }
"""
TS_PROPERTY = """\
  @Input({
    required: true,
    alias: 'ttl',
  })
  title!: string;
"""


@pytest.mark.parametrize(
    "name, code",
    [
        pytest.param("DecoratedComponent.onClick", TS_METHOD, id="decorated-method"),
        pytest.param("DecoratedComponent.title", TS_PROPERTY, id="decorated-property"),
        pytest.param(
            "DecoratedComponent.onClick",
            "".join(x[2:] for x in TS_METHOD.splitlines(keepends=True)),
            id="dedented-method",
        ),
    ],
)
async def test_ep6_identity_replace_of_a_multiline_decorated_typescript_member_keeps_every_byte(
    pipeline: Pipeline, root: Path, name: str, code: str
) -> None:
    before = put(root, TS, TS_CLASS)
    data = data_of(await replace(pipeline, TS, name, code))
    assert data["applied"] is True and data["diff"] == ""
    assert (root / TS).read_bytes() == before


async def test_typescript_decorated_class_identity_and_changed_body(pipeline: Pipeline, root: Path) -> None:
    before = put(root, TS, TS_CLASS)
    start = TS_CLASS.index("@Component(")
    whole = TS_CLASS[start:]
    data = data_of(await replace(pipeline, TS, "DecoratedComponent", whole))
    assert data["diff"] == "" and (root / TS).read_bytes() == before
    changed = whole.replace("console.log(event);", "console.warn(event);").replace(
        "selector: 'app-decorated'", "selector: 'app-changed'"
    )
    data = data_of(await replace(pipeline, TS, "DecoratedComponent", changed))
    after = (root / TS).read_bytes()
    assert after == before.replace(whole.encode(), changed.encode())
    assert data["syntax_check"] == "ok" and error_node_count(after, "typescript") == 0


async def test_typescript_changed_body_of_a_decorated_member(pipeline: Pipeline, root: Path) -> None:
    put(root, TS, TS_CLASS)
    new = TS_METHOD.replace("console.log(event);", "console.warn(event);")
    data_of(await replace(pipeline, TS, "DecoratedComponent.onClick", new))
    after = (root / TS).read_bytes()
    assert after == TS_CLASS.replace(TS_METHOD, new).encode()
    assert error_node_count(after, "typescript") == 0


async def test_typescript_decorator_without_a_member_is_refused(pipeline: Pipeline, root: Path) -> None:
    put(root, TS, TS_CLASS)
    before = snapshot(root)
    err = error_of(
        await replace(
            pipeline, TS, "DecoratedComponent.onClick", "@HostListener('click', [\n  '$event',\n])\n"
        )
    )
    assert err["code"] == "E_SYNTAX_INVALID"
    assert snapshot(root) == before
