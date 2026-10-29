"""T-02 ``get_file_skeleton``（仕様書 §11.2）。1〜20 ファイルの骨格（import・シグネチャ・Doc 1 行目）を返す。

縦切りの範囲: Python・TypeScript・JavaScript を構文木だけで（``structural``）。Tier 3 のデータファイル（JSON・JSONL・YAML・TOML・XML）
と ``.ipynb`` はデータ形式アダプタの骨格（``_data_files.py``。``[Key]`` / ``[Tag]`` / ``[Cell]``、``max_depth`` で畳む）。
Markdown・reStructuredText は見出しの入れ子（``[Section]``、``structural``）、それ以外の未知の言語・拡張子なし（Tier 4）は
ブロック推定の骨格（``heuristic`` / ``lexical_fallback``）。種類の振り分けは ``_skeletons.file_skeleton``。

``revision`` は ``WORKING_TREE``・``STAGED``・ブランチ・タグ・SHA・``HEAD~N``。ファイルは ``tools/_rev.py`` を通して Git の
オブジェクト / インデックスから読み、作業ツリーに触れない。解決できない参照は ``E_REVISION_NOT_FOUND``（BUG-0018）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from lucianke.core.errors import LuciankeError
from lucianke.core.limits import BATCH_MAX_ITEMS
from lucianke.core.model import Confidence, EngineChoice, Tier
from lucianke.core.records import Record, ToolOutput
from lucianke.syntax.types import SkeletonOptions
from lucianke.tools._base import CommonArgs, ToolContext, ToolSpec
from lucianke.tools._rev import open_revision
from lucianke.tools._services import batch_warnings, services_of
from lucianke.tools._skeletons import engine_name, file_skeleton


class GetFileSkeletonArgs(CommonArgs):
    paths: list[str] = Field(
        min_length=1,
        max_length=BATCH_MAX_ITEMS,
        description="1-20 file paths (workspace-relative with '/', or absolute inside the workspace).",
    )
    include_private: bool = Field(default=True, description="Include private members (_name, private, non-export).")
    include_doc_summary: bool = Field(default=True, description="Add the first line of the doc comment as 'doc:'.")
    include_urns: Literal["public", "all", "none"] = Field(default="public", description="Which symbols get a 'urn:' line.")
    include_imports: bool = Field(default=True, description="Add the 'imports:' line.")
    max_depth: int = Field(default=4, ge=1, le=10, description="Depth for data files (Tier 3). Unused for code.")


def failure_record(path: str, err: LuciankeError, priority: int, banner: Any) -> Record:
    """項目単位の失敗（仕様書 §5.4.4）。compact ではファイルブロックの位置に ``! <path>: <CODE> <message>`` の 1 行。"""
    return Record(
        kind="note",
        key=f"error:{path}",
        priority=priority,
        lines=tuple(banner(f"{path}: {err.code.value} {err.message}")),
        data={"ok": False, "path": path, "error": err.to_json()},
        confidence=Confidence.STRUCTURAL,
        group=path,
    )


async def handler(ctx: ToolContext, args: GetFileSkeletonArgs) -> ToolOutput:
    svc = services_of(ctx.services)
    ws = svc.workspace(args.workspace_id)
    view = await open_revision(ws, args.revision)  # 解決できなければ E_REVISION_NOT_FOUND（作業ツリーは Git に触れない）
    opts = SkeletonOptions(
        include_private=args.include_private,
        include_doc_summary=args.include_doc_summary,
        include_urns=args.include_urns,
        include_imports=args.include_imports,
        max_depth=args.max_depth,
    )
    records: list[Record] = []
    errors: list[tuple[str, LuciankeError]] = []
    languages: list[str] = []
    tier = Tier.T1
    engines: set[str] = set()
    for i, path in enumerate(args.paths):
        try:
            sk = await file_skeleton(svc, ws, view, path, opts, priority=i, param="paths")
            records.append(sk.record)
            engines.add(sk.engine)
            if sk.language_id not in languages:
                languages.append(sk.language_id)
            tier = max(tier, sk.tier)
        except LuciankeError as err:
            errors.append((path, err))
            records.append(failure_record(path, err, i, svc.lines.banner))
    if errors and len(errors) == len(args.paths):
        first_path, first = errors[0]
        raise LuciankeError(
            first.code,
            first.message,
            details={
                **first.details,
                "items": [{"ok": False, "path": p, "error": e.to_json()} for p, e in errors],
            },
        )
    return ToolOutput(
        records=records,
        max_confidence=Confidence.STRUCTURAL,
        engine=EngineChoice(engine=view.engine_name(engine_name(engines)), confidence=Confidence.STRUCTURAL),
        page_unit="file",
        tier=tier,
        detected_language=",".join(languages) if languages else "n/a",
        warnings=(*view.warnings, *batch_warnings(len(errors), len(args.paths))),
    )


SPEC = ToolSpec(
    name="get_file_skeleton",
    tid="T-02",
    args_model=GetFileSkeletonArgs,
    handler=handler,
    default_max_tokens=2000,
    description=(
        "Return a skeleton of 1-20 files: imports, class/function signatures with line ranges, and the first "
        "line of each doc comment, with bodies folded. Use it instead of reading whole files. Python/TypeScript/JavaScript "
        "(syntax tree only); data files (JSON/JSONL/YAML/TOML/XML) as a key/tag tree with array types, folded at max_depth; "
        ".ipynb as cells (outputs are never shown); Markdown/reStructuredText as a heading tree; other files (unknown "
        "language, no extension) as a heuristic outline marked DEGRADED. Reads any revision (HEAD, HEAD~N, branch, tag, SHA, "
        "STAGED) from Git without touching the working tree. Each file block carries sha= for edit preconditions; a failed "
        "path is reported as '! path: CODE msg'."
    ),
    max_confidence=Confidence.STRUCTURAL,
    milestone="M1",
    page_unit="file",
    records_key="items",
)
