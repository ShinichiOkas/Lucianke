"""M2・M3 のツールの入口（設計書 §12.2 の注、§7.1.1）。入力スキーマと説明文を ``tools/list`` に載せ、呼ばれたら ``E_UNSUPPORTED``。

エージェントが「そのツールが存在する」ことを知れるようにするための置き場で、本実装の役はこの 1 つの呼び出し
（``planned_tool``）を自分のハンドラに置き換えるだけでよい（引数モデルはそのまま使える形で仕様書どおりに書いてある）。

二重の守り:

* 既定のパイプライン（``current_milestone="M1"``）は、``spec.milestone`` が現在のマイルストーンより先のツールを
  ハンドラの前（引数の検証より前）で ``E_UNSUPPORTED``（``details.reason: "not_in_milestone"``, ``details.milestone``）にする。
  このときハンドラは呼ばれないので、``details.hint`` はパイプラインの応答には載らない。代わりに ``description`` の
  末尾に「いつから使えるか」と「それまでに使える M1 のツール」を書き、``tools/list`` の時点で読めるようにする。
* ``current_milestone`` をそのマイルストーンまで進めたパイプライン（本実装がまだ入っていない間）では、ここのハンドラが
  呼ばれる。同じ ``reason`` / ``milestone`` に ``hint`` を足した ``E_UNSUPPORTED`` を返す。

``tools`` は ``mcp`` を import しない（設計書 §4.2）。このファイルは ``tNN_`` で始まらないので自動登録の対象ではない。
"""

from __future__ import annotations

from typing import Any, NoReturn

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.tools._base import CommonArgs, ToolContext, ToolHandler, ToolSpec

__all__ = ["NOT_IN_MILESTONE", "planned_tool", "unsupported_handler"]

NOT_IN_MILESTONE = "not_in_milestone"


def unsupported_handler(*, name: str, milestone: str, hint: str | None = None) -> ToolHandler:
    """呼ばれたら ``E_UNSUPPORTED`` を送出するハンドラ。``hint`` があれば ``details.hint`` に入れる。"""
    message = f"{name} is planned for {milestone} and is not available yet"
    if hint:
        message += f" (meanwhile: {hint})"
    details: dict[str, Any] = {"reason": NOT_IN_MILESTONE, "milestone": milestone}
    if hint:
        details["hint"] = hint

    async def handler(ctx: ToolContext, args: Any) -> NoReturn:
        raise LuciankeError(ErrorCode.E_UNSUPPORTED, message, details=dict(details))

    return handler


def planned_tool(
    *,
    name: str,
    tid: str,
    args_model: type[CommonArgs],
    milestone: str,
    default_max_tokens: int,
    purpose: str,
    hint: str | None = None,
    **spec_fields: Any,
) -> ToolSpec:
    """まだ実装のないツールの ``ToolSpec``。``purpose`` は仕様書の「目的」を短くした 1〜2 文（英語）。

    ``description`` は ``purpose`` に「``milestone`` から使える。それまでは ``E_UNSUPPORTED``」と、``hint``（それまでに
    使える M1 のツール。無ければ書かない）を足したもの。``spec_fields`` は ``allows_graph`` / ``writes`` /
    ``accepts_revision`` / ``max_confidence`` / ``page_unit`` / ``records_key`` など ``ToolSpec`` の残りの項目。
    """
    description = f"{purpose} Planned for {milestone}: not available yet, calls return E_UNSUPPORTED."
    if hint:
        description += f" Meanwhile: {hint}"
    return ToolSpec(
        name=name,
        tid=tid,
        args_model=args_model,
        handler=unsupported_handler(name=name, milestone=milestone, hint=hint),
        default_max_tokens=default_max_tokens,
        description=description,
        milestone=milestone,
        **spec_fields,
    )
