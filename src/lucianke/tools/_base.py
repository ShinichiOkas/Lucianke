"""ツール層の共通部品（設計書 §4.1 の ``tools/_base.py``、§7.1.1、§7.1.3）。

``ToolSpec``・``CommonArgs``・``ToolContext`` をここに置く。設計書 §4.2 は「``tools`` は ``mcp`` を import しない」を
MUST としており、各ツールの引数モデルは ``CommonArgs`` を継承し、ツールファイルが自分の ``ToolSpec`` を宣言するため、
これらは ``tools`` 側にないと依存の向きが逆転する。``lucianke.mcp.registry`` は同じ名前を再公開する。

``ToolContext.call`` は ``mcp.types.CallInfo`` と同じ属性を持つ Protocol（``CallView``）で型付ける。
``CallInfo`` は構造的にこれを満たす（``tools`` から ``mcp`` を import できないため）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from lucianke.core.model import Confidence
from lucianke.core.records import ToolOutput

FormatName = Literal["compact", "json", "dot", "mermaid"]
GRAPH_FORMATS: tuple[str, ...] = ("dot", "mermaid")
WORKING_TREE = "WORKING_TREE"


class CommonArgs(BaseModel):
    """全ツール共通のパラメータ（仕様書 §5.2）。各ツールの引数モデルはこれを継承する。

    ``max_tokens`` の既定はツールごと（``ToolSpec.default_max_tokens``）。未指定は ``None`` で受け、パイプラインが埋める。
    最小値（200）は ``tools/list`` のスキーマには ``minimum`` として載せ、検査はパイプラインが ``E_INVALID_ARGUMENT`` で行う。
    """

    model_config = ConfigDict(extra="forbid")

    # tools/list のトークンを抑えるため、説明は最小（名前と型・enum・既定値で足りるものは付けない。tests/unit/test_schema_budget.py）
    workspace_id: str | None = Field(default=None)
    revision: str = Field(default=WORKING_TREE, description="Git ref or WORKING_TREE.")
    format: FormatName = Field(default="compact")
    max_tokens: int | None = Field(default=None)
    cursor: str | None = Field(default=None)


class CallView(Protocol):
    """``ToolContext.call`` の型。``lucianke.mcp.types.CallInfo`` がこれを満たす。"""

    @property
    def tool(self) -> str: ...
    @property
    def revision(self) -> str: ...
    @property
    def workspace_id(self) -> str | None: ...
    @property
    def format(self) -> str: ...
    @property
    def max_tokens(self) -> int: ...
    @property
    def timing_ms(self) -> int: ...


@dataclass(slots=True)
class Services:
    """ハンドラが使うサービスの入れ物。後でワークスペース・索引・LSP などが入る（合成ルートが組み立てる）。"""

    def resolve_workspace_id(self, workspace_id: str | None) -> str | None:
        """引数の ``workspace_id`` を、解決後のワークスペース ID にする（設計書 §7.1.2 手順 3）。

        ワークスペースを知らない入れ物は引数をそのまま返す。知っている入れ物（``ToolServices``）は上書きし、
        省略（``None``）を実在の ID にする。解決できなければ ``E_WORKSPACE_NOT_CONFIGURED``。
        """
        return workspace_id


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolContext:
    """ハンドラに渡す文脈（設計書 §7.1.2 の手順 3）。

    Attributes:
        call: 呼び出しの情報（ツール名、解決済みの revision / format / max_tokens、workspace_id）。
        services: サービスの入れ物。
        args: 検証済みの引数（``max_tokens`` は既定を埋めたもの）。
        log: ``request_id`` を付けた呼び出し用ロガー。
        request_id: 呼び出しの相関 ID。ログと ``E_INTERNAL`` の ``details.log_ref`` に使う。
        cursor_offset: ``cursor`` を復号した先頭オフセット（なければ 0）。パッカーが同じ値で ``records`` を切る。
    """

    call: CallView
    services: Services
    args: CommonArgs
    log: logging.LoggerAdapter[logging.Logger]
    request_id: str
    cursor_offset: int = 0


ToolHandler = Callable[[ToolContext, Any], Awaitable[ToolOutput]]
"""``async def handler(ctx, args) -> ToolOutput``。出力形式は知らない（レコード列を返すだけ。ADR-4）。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSpec:
    """ツールの宣言（設計書 §7.1.1）。1 ツール 1 つ。``tools/list`` とパイプラインの唯一の根拠。

    設計書の項目に ``description``（``tools/list`` に必須）と ``records_key``（json の ``data`` のレコード配列名。
    ``RenderInput.records_key`` へ渡す）を足してある。
    """

    name: str  # "get_file_skeleton"
    tid: str  # "T-02"
    args_model: type[CommonArgs]
    handler: ToolHandler
    default_max_tokens: int
    description: str = ""
    allows_graph: bool = False  # dot / mermaid 可否（仕様書 §6.4）
    writes: bool = False  # カテゴリ D（revision は WORKING_TREE のみ）
    max_confidence: Confidence = Confidence.EXACT
    milestone: str = "M1"  # "M1" | "M2" | "M3"
    accepts_revision: bool = True  # False なら revision を無視して warnings
    page_unit: str = "record"
    records_key: str = "items"
