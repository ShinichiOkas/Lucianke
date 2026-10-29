"""エンジン選択（フォールバック・ラダー。設計書 ADR-6・ADR-10、§7.8、仕様書 §8.2〜§8.4）。

ツールは ``select`` が返す ``EngineChoice`` に従ってエンジンを呼ぶだけで、ラダーの判断を書かない。
降格の挙動を変えるときは、このモジュールの ``_TABLE`` を直す（1 か所）。

* ``_TABLE`` は設計書 §7.8.2 の判定表そのもの（能力 12 × 列 4）。``select`` は表引きで返す。
* 列は「文法あり・LSP ``ready``」「文法あり・LSP 起動/索引中」「文法あり・LSP ``unavailable`` / 定義なし」
  「文法なし（Tier 4）」。ファイル単位の判定で、列はファイルの状態から 1 つに決まる。
* 待つ／待たない（ADR-6、§7.8.3）は ``DEFAULT_WAIT``。``WAIT_SHORT`` で上限（``LSP_TIMEOUT_MS``）を
  超えたら、呼び手が ``on_lsp_timeout`` で右隣の列へ 1 段降格する（その要求に限る）。
* LSP 機能（``lsp.features``）の失敗も同じ右隣の列へ降格する: ``TimeoutError`` と ``E_LSP_UNAVAILABLE``
  （``position_clamped`` を含む）は ``on_lsp_failure`` が受ける（``is_lsp_degradation`` で降格かどうかを先に引ける）。
* Tier 4 の字句降格（表の ``→``。ctags で何も取れなかったとき）は ``on_nothing_found``。
* 警告文の雛形は ``warning_text``（仕様書 §8.4 の 3 雛形 + 設計書 §7.8.4 の理由別）。

副作用は持たない（純関数）。LSP の遅延起動（``LspPool.ensure_started``）は呼び手が行い、
その要否は ``starts_lsp`` で引く。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Final

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import (
    LSP_TIMEOUT_CALL_HIERARCHY_MS,
    LSP_TIMEOUT_CODE_ACTION_MS,
    LSP_TIMEOUT_DEFINITION_MS,
    LSP_TIMEOUT_REFERENCES_MS,
    LSP_TIMEOUT_RENAME_MS,
)
from lucianke.core.model import Capability, Confidence, EngineChoice, FileInfo, LspStatus, Tier

__all__ = [
    "DEFAULT_WAIT",
    "ENGINE_EXCLUDED",
    "ENGINE_LEXICAL_GREP",
    "ENGINE_LSP",
    "ENGINE_LSP_CACHE",
    "ENGINE_LSP_UNAVAILABLE",
    "ENGINE_NONE",
    "ENGINE_UNAVAILABLE",
    "LEVEL_TEMPLATES",
    "LSP_TIMEOUT_MS",
    "NOT_PROVIDED_ENGINES",
    "POSITION_CLAMPED_WARNING",
    "Column",
    "WaitPolicy",
    "WarningReason",
    "is_degraded",
    "is_lsp_degradation",
    "on_lsp_failure",
    "on_lsp_timeout",
    "on_nothing_found",
    "select",
    "starts_lsp",
    "warning_text",
]

# --- エンジン名 ---------------------------------------------------------------------------

ENGINE_LSP: Final = "lsp"
"""LSP に要求を送る（``lsp_name`` を渡すと ``<lsp名>+tree-sitter`` になる。仕様書 §8.4 の ``engine_used``）。"""
ENGINE_LSP_UNAVAILABLE: Final = "lsp_unavailable"
"""LSP が要るのに使えない（RENAME / CODE_ACTION）。ツールは ``E_LSP_UNAVAILABLE`` で返す。"""
ENGINE_LEXICAL_GREP: Final = "internal_ripgrep_word_boundary"
"""段 4（仕様書 §8.4）。単語境界の字句一致。"""
ENGINE_UNAVAILABLE: Final = "unavailable"
"""提供しない（IMPLEMENTATIONS の Tier 4。設計書 §7.8.2 の ``(unavailable)``）。"""
ENGINE_NONE: Final = "none"
"""エンジンなし（DIAGNOSTICS の Tier 4。0 件）。"""
ENGINE_EXCLUDED: Final = "excluded"
"""対象外（STRUCTURAL_EDIT の Tier 4。``warnings`` に除外件数）。"""

NOT_PROVIDED_ENGINES: Final = frozenset(
    {ENGINE_LSP_UNAVAILABLE, ENGINE_UNAVAILABLE, ENGINE_NONE, ENGINE_EXCLUDED}
)
"""この能力はこの状態では提供されない。レコードを作らず、ツールが ``E_*`` か除外件数で応える。水準は ``lexical_fallback``。"""

ENGINE_LSP_CACHE: Final = "lsp-cache+tree-sitter"
"""LSP の結果を PaaD に書き戻したもの（``relations`` の exact 行・``_hover_cache``）から答える。DIAGNOSTICS の ``ready`` 列と、
T-04 の 2 回目以降（設計書 R-11）が使う。LSP が動いていなくても ``exact``。"""
_ENGINE_LSP_CACHE: Final = ENGINE_LSP_CACHE

_E = Confidence.EXACT
_S = Confidence.STRUCTURAL
_H = Confidence.HEURISTIC
_L = Confidence.LEXICAL_FALLBACK


# --- 待つ／待たない（ADR-6、設計書 §7.8.3） -----------------------------------------------


class WaitPolicy(StrEnum):
    WAIT_SHORT = "wait_short"
    """LSP が ready なら要求を送り、``LSP_TIMEOUT_*`` 内で待つ。超えたら降格（``on_lsp_timeout``）。"""
    NO_WAIT = "no_wait"
    """LSP に同期要求を送らない。PaaD と構文木で答える。"""


DEFAULT_WAIT: Final[Mapping[Capability, WaitPolicy]] = MappingProxyType(
    {
        Capability.SKELETON: WaitPolicy.NO_WAIT,
        Capability.SEARCH: WaitPolicy.NO_WAIT,
        Capability.BODY: WaitPolicy.NO_WAIT,
        Capability.DEFINITION: WaitPolicy.WAIT_SHORT,
        Capability.REFERENCES: WaitPolicy.WAIT_SHORT,
        Capability.IMPLEMENTATIONS: WaitPolicy.WAIT_SHORT,
        Capability.CALL_HIERARCHY: WaitPolicy.WAIT_SHORT,
        Capability.DIAGNOSTICS: WaitPolicy.NO_WAIT,  # 受信済みキャッシュを返す。診断待ちは DiagnosticsWaiter が別に担う
        Capability.RENAME: WaitPolicy.WAIT_SHORT,
        Capability.CODE_ACTION: WaitPolicy.WAIT_SHORT,
        Capability.STRUCTURAL_EDIT: WaitPolicy.NO_WAIT,
        Capability.SYMBOL_EDIT: WaitPolicy.NO_WAIT,
    }
)
"""能力ごとの既定の待ち方（設計書 §7.8.3 の表）。"""

LSP_TIMEOUT_MS: Final[Mapping[Capability, int]] = MappingProxyType(
    {
        Capability.DEFINITION: LSP_TIMEOUT_DEFINITION_MS,
        Capability.REFERENCES: LSP_TIMEOUT_REFERENCES_MS,
        Capability.IMPLEMENTATIONS: LSP_TIMEOUT_REFERENCES_MS,
        Capability.CALL_HIERARCHY: LSP_TIMEOUT_CALL_HIERARCHY_MS,
        Capability.RENAME: LSP_TIMEOUT_RENAME_MS,
        Capability.CODE_ACTION: LSP_TIMEOUT_CODE_ACTION_MS,
    }
)
"""``WAIT_SHORT`` の能力が LSP を待つ上限（ミリ秒。設計書 §7.8.3 の表）。呼び手の ``asyncio.wait_for`` に渡す。"""


# --- 警告文の雛形 -------------------------------------------------------------------------


class WarningReason(StrEnum):
    """警告文を選ぶ理由（設計書 §7.8.4 の表）。"""

    LSP_INDEXING = "lsp_indexing"
    LSP_UNAVAILABLE = "lsp_unavailable"
    LSP_TIMEOUT = "lsp_timeout"
    NO_GRAMMAR_CTAGS = "no_grammar_ctags"
    NO_GRAMMAR_LEXICAL = "no_grammar_lexical"
    MIXED = "mixed"


LEVEL_TEMPLATES: Final[Mapping[Confidence, str]] = MappingProxyType(
    {
        Confidence.STRUCTURAL: (
            "LSP が利用できないため構文木のみで解決しました。"
            "型・オーバーロード・再エクスポート越しの参照は区別されていません。"
        ),
        Confidence.HEURISTIC: (
            "言語の文法が無いため ctags とブロック推定で境界を求めました。"
            "終了行は推定であり、編集前に `get_symbol_bodies` で範囲を確かめてください。"
        ),
        Confidence.LEXICAL_FALLBACK: (
            "未知の言語のため単語境界 grep に降格しました。同名の誤検知と動的参照の見落としが含まれます。"
        ),
    }
)
"""仕様書 §8.4「警告文の雛形」。``exact`` は降格ではないので雛形を持たない。"""


def warning_text(
    reason: WarningReason | str,
    *,
    confidence: Confidence | None = None,
    engine: str | None = None,
    language: str | None = None,
    progress: int | None = None,
    timeout_ms: int | None = None,
    file_count: int | None = None,
) -> str:
    """``reason`` に対応する警告文（日本語）。設計書 §7.8.4 の表。

    * ``LSP_INDEXING``: ``progress``（0〜100。``None`` なら括弧ごと省く。``starting`` は進捗なし）。
    * ``LSP_UNAVAILABLE``: 仕様書 §8.4 の structural 雛形 + ``configure_workspace status`` の案内。
    * ``LSP_TIMEOUT``: ``timeout_ms`` が必要（``LSP_TIMEOUT_MS`` の値）。
    * ``NO_GRAMMAR_CTAGS`` / ``NO_GRAMMAR_LEXICAL``: 仕様書 §8.4 の heuristic / lexical_fallback 雛形。
    * ``MIXED``: ``confidence``（最低水準。``exact`` は不可）の雛形 + 「{file_count} 件のファイルがこの水準です」。

    ``engine`` / ``language`` は雛形に差し込み口が無いので文面には使わない（compact の ``! DEGRADED:``
    行は ``mcp/envelope.py`` が別に組み立てる）。呼び出しの形を設計書と揃えるための引数。
    不足する引数や未知の ``reason`` は ``ValueError``。
    """
    del engine, language  # 文面に使わない（上記）
    r = WarningReason(reason)
    if r is WarningReason.LSP_INDEXING:
        pct = "" if progress is None else f"（{max(0, min(100, int(progress)))}%）"
        return (
            f"LSP が起動・索引中{pct}のため構文木で解決しました。"
            "型・オーバーロード・再エクスポート越しの参照は区別されていません。"
            "完了後に呼び直すと `exact` になります。"
        )
    if r is WarningReason.LSP_UNAVAILABLE:
        return (
            LEVEL_TEMPLATES[Confidence.STRUCTURAL]
            + "`configure_workspace status` で LSP の状態を確認してください。"
        )
    if r is WarningReason.LSP_TIMEOUT:
        if timeout_ms is None:
            raise ValueError("lsp_timeout needs timeout_ms")
        return f"LSP の応答が {timeout_ms} ms 内に得られなかったため構文木で解決しました。"
    if r is WarningReason.NO_GRAMMAR_CTAGS:
        return LEVEL_TEMPLATES[Confidence.HEURISTIC]
    if r is WarningReason.NO_GRAMMAR_LEXICAL:
        return LEVEL_TEMPLATES[Confidence.LEXICAL_FALLBACK]
    # MIXED
    if confidence is None or file_count is None:
        raise ValueError("mixed needs confidence and file_count")
    base = LEVEL_TEMPLATES.get(Confidence(confidence))
    if base is None:
        raise ValueError(f"mixed has no template for {confidence!r}")
    return base + f"{file_count} 件のファイルがこの水準です（内訳は `confidence_breakdown`）。"


def is_degraded(choice_confidence: Confidence, tool_max_confidence: Confidence) -> bool:
    """選んだ水準がツールの最高水準より低いか（仕様書 §8.3、設計書 §7.8.4）。

    最高水準が ``structural`` のツール（T-12）は ``structural`` でも ``False``。
    """
    return Confidence(choice_confidence) < Confidence(tool_max_confidence)


# --- 判定表（設計書 §7.8.2） --------------------------------------------------------------


class Column(IntEnum):
    """判定表の列。``on_lsp_timeout`` の「右隣」はこの順の次。"""

    READY = 0  # 文法あり・LSP ready
    INDEXING = 1  # 文法あり・LSP starting / indexing
    UNAVAILABLE = 2  # 文法あり・LSP unavailable / 定義なし
    NO_GRAMMAR = 3  # 文法なし（Tier 4）


@dataclass(frozen=True, slots=True)
class _Cell:
    """判定表のセル 1 つ。``engine / confidence`` と、警告文の理由。

    * ``fallback``: 表の ``→``（Tier 4 で何も取れなかったときの次の段。``on_nothing_found``）。
    * ``without_ctags``: ctags が無いときの代替（``None`` なら ctags を要さない）。
    """

    engine: str
    confidence: Confidence
    warning: WarningReason | None = None
    fallback: _Cell | None = None
    without_ctags: _Cell | None = None


type _Row = tuple[_Cell, _Cell, _Cell, _Cell]

_R = WarningReason

_GREP = _Cell(ENGINE_LEXICAL_GREP, _L, _R.NO_GRAMMAR_LEXICAL)
_LSP_DOWN = _Cell(
    ENGINE_LSP_UNAVAILABLE, _L
)  # 警告なし: ツールが E_LSP_UNAVAILABLE で返す（lsp_status が理由を持つ）
_NOT_UNAVAILABLE = _Cell(ENGINE_UNAVAILABLE, _L)
_NOT_NONE = _Cell(ENGINE_NONE, _L)
_NOT_EXCLUDED = _Cell(ENGINE_EXCLUDED, _L)


def _row(ready: _Cell, lower_engine: str, no_grammar: _Cell) -> _Row:
    """列 2・3 が同じエンジン・同じ水準（structural）で、警告の理由だけ違う行。"""
    return (
        ready,
        _Cell(lower_engine, _S, _R.LSP_INDEXING),
        _Cell(lower_engine, _S, _R.LSP_UNAVAILABLE),
        no_grammar,
    )


def _ctags(engine: str, *, without: _Cell = _GREP) -> _Cell:
    """Tier 4 の ctags 系セル（heuristic）。何も取れなければ grep、ctags が無ければ ``without``。"""
    return _Cell(engine, _H, _R.NO_GRAMMAR_CTAGS, fallback=_GREP, without_ctags=without)


_BLOCK_HEURISTIC = _Cell("block-heuristic", _H, _R.NO_GRAMMAR_CTAGS, fallback=_GREP)
_AST_GREP = _Cell("ast-grep", _S)  # 構文一致が本来の精度なので警告なし（T-12 の最高水準は structural）

_TABLE: Final[Mapping[Capability, _Row]] = MappingProxyType(
    {
        Capability.SKELETON: _row(
            _Cell("tree-sitter", _E), "tree-sitter", _ctags("ctags", without=_BLOCK_HEURISTIC)
        ),
        Capability.SEARCH: _row(_Cell("fts5", _E), "fts5", _ctags("fts5")),
        Capability.BODY: _row(_Cell("tree-sitter", _E), "tree-sitter", _ctags("ctags+block")),
        Capability.DEFINITION: _row(_Cell(ENGINE_LSP, _E), "tree-sitter+imports", _ctags("ctags")),
        Capability.REFERENCES: _row(_Cell(ENGINE_LSP, _E), "relations", _GREP),
        Capability.IMPLEMENTATIONS: _row(
            _Cell(ENGINE_LSP, _E), "relations(IMPLEMENTS, INHERITS)", _NOT_UNAVAILABLE
        ),
        Capability.CALL_HIERARCHY: _row(_Cell(ENGINE_LSP, _E), "relations(CALLS)", _GREP),
        Capability.DIAGNOSTICS: _row(_Cell(_ENGINE_LSP_CACHE, _E), "tree-sitter", _NOT_NONE),
        Capability.RENAME: (
            _Cell(ENGINE_LSP, _E),
            _LSP_DOWN,
            _LSP_DOWN,
            _Cell("lexical-replace", _L, _R.NO_GRAMMAR_LEXICAL),  # インターロック下（仕様書 §10.5）
        ),
        Capability.CODE_ACTION: (_Cell(ENGINE_LSP, _E), _LSP_DOWN, _LSP_DOWN, _LSP_DOWN),
        Capability.STRUCTURAL_EDIT: (_AST_GREP, _AST_GREP, _AST_GREP, _NOT_EXCLUDED),
        Capability.SYMBOL_EDIT: _row(_Cell("tree-sitter", _E), "tree-sitter", _ctags("ctags+block")),
    }
)

_STARTS_LSP: Final = frozenset(
    cap for cap, row in _TABLE.items() if row[Column.READY].engine in (ENGINE_LSP, _ENGINE_LSP_CACHE)
)
_REQUESTS_LSP: Final = frozenset(cap for cap, row in _TABLE.items() if row[Column.READY].engine == ENGINE_LSP)

_STATUS_COLUMN: Final[Mapping[LspStatus, Column]] = MappingProxyType(
    {
        LspStatus.READY: Column.READY,
        LspStatus.INDEXING: Column.INDEXING,
        LspStatus.STANDBY: Column.INDEXING,  # 遅延起動の契機になる能力のとき（``_locate`` が補正する）
        LspStatus.UNAVAILABLE: Column.UNAVAILABLE,
        LspStatus.NOT_APPLICABLE: Column.UNAVAILABLE,  # LSP 定義なし
    }
)


def starts_lsp(cap: Capability) -> bool:
    """この能力の呼び出しが LSP の遅延起動の契機になるか（設計書 §7.8.1 の副作用）。

    ``True`` の能力で LSP が ``standby`` なら、呼び手は ``LspPool.ensure_started`` を投げる（待たない）。
    ``select`` はこのとき ``standby`` を ``indexing`` の列（``lsp_status="indexing"``）に入れる。
    """
    return cap in _STARTS_LSP


def _locate(
    file: FileInfo, cap: Capability, status: LspStatus, has_grammar: bool
) -> tuple[Column, LspStatus]:
    """列と、``EngineChoice.lsp_status`` に書く状態。"""
    if not has_grammar:
        return Column.NO_GRAMMAR, LspStatus.NOT_APPLICABLE
    if file.tier == Tier.T3:  # Tier 3 のアダプタは常に structural（設計書 §7.8.2 の注）。LSP は無い
        return Column.UNAVAILABLE, LspStatus.NOT_APPLICABLE
    if status is LspStatus.STANDBY:
        if cap in _STARTS_LSP:
            return Column.INDEXING, LspStatus.INDEXING  # starting は indexing と表示（進捗は null）
        return Column.UNAVAILABLE, LspStatus.STANDBY  # 起動の契機にならない能力: 未起動のまま構文木で答える
    return _STATUS_COLUMN[status], status


def _make_choice(
    cell: _Cell,
    status: LspStatus,
    progress: int | None,
    *,
    language: str | None = None,
    lsp_name: str | None = None,
    timeout_ms: int | None = None,
) -> EngineChoice:
    engine = f"{lsp_name}+tree-sitter" if cell.engine == ENGINE_LSP and lsp_name else cell.engine
    warning = (
        None
        if cell.warning is None
        else warning_text(
            cell.warning,
            confidence=cell.confidence,
            engine=engine,
            language=language,
            progress=progress,
            timeout_ms=timeout_ms,
        )
    )
    return EngineChoice(
        engine=engine,
        confidence=cell.confidence,
        lsp_status=status.value,
        lsp_progress=progress,
        warning=warning,
    )


def select(
    file: FileInfo,
    cap: Capability,
    *,
    lsp_status: LspStatus,
    lsp_progress: int | None,
    has_grammar: bool,
    has_ctags: bool,
    wait_policy: WaitPolicy | None = None,
    lsp_name: str | None = None,
) -> EngineChoice:
    """``file`` に ``cap`` を適用するエンジンと水準を、判定表から引く（設計書 §7.8.2）。

    * ``lsp_status`` / ``lsp_progress``: ``LspPool.status`` の結果。``starting`` は ``INDEXING``・進捗 ``None``。
      LSP 定義のない言語は ``NOT_APPLICABLE``。進捗は ``indexing`` の列でだけ ``EngineChoice`` に載る（0〜100）。
    * ``has_grammar``: Tree-sitter 文法または ``FormatAdapter`` がある（Tier 2 のアダプタも ``True``）。
      ``False`` なら Tier 4 の列。Tier 3 のファイルは LSP の状態によらず常に structural の列。
    * ``has_ctags``: Tier 4 のとき ctags の定義があるか。無ければ ctags 系のセルは字句（grep）へ直接降りる
      （SKELETON だけは ``block-heuristic`` の heuristic）。
    * ``wait_policy``: ``None`` なら ``DEFAULT_WAIT[cap]``。``NO_WAIT`` で ``ready`` の列に落ちた LSP 要求の能力は、
      右隣の列のセル（構文木・``relations`` の structural）を返す。``lsp_status`` は ``ready`` のまま、警告なし。
    * ``lsp_name``: 指定すると LSP を使うセルの ``engine`` を ``<lsp_name>+tree-sitter`` にする（仕様書 §8.4）。

    ``engine`` が ``NOT_PROVIDED_ENGINES`` のセルは提供しない（レコードを作らない）。``standby`` の扱いは ``starts_lsp``。
    副作用は無い（``ensure_started`` は呼び手）。
    """
    status = LspStatus(lsp_status)
    column, reported = _locate(file, cap, status, has_grammar)
    row = _TABLE[cap]
    cell = row[column]
    progress: int | None = None
    if column is Column.INDEXING and reported is LspStatus.INDEXING and lsp_progress is not None:
        progress = max(0, min(100, int(lsp_progress)))
    if (
        column is Column.READY
        and cap in _REQUESTS_LSP
        and (wait_policy or DEFAULT_WAIT[cap]) is WaitPolicy.NO_WAIT
    ):
        # LSP に要求を送らない: 右隣の列のセル。構文木・索引で答えており、列 2 の「起動・索引中」の警告は当たらない
        cell = replace(row[Column.INDEXING], warning=None)
    elif column is Column.NO_GRAMMAR and not has_ctags and cell.without_ctags is not None:
        cell = cell.without_ctags
    return _make_choice(cell, reported, progress, language=file.language, lsp_name=lsp_name)


def on_lsp_timeout(choice: EngineChoice, cap: Capability) -> EngineChoice:
    """``WAIT_SHORT`` の LSP 要求が上限（``LSP_TIMEOUT_MS[cap]``）を超えたとき、その要求に限り右隣の列へ降格する。

    ``lsp_status`` は ``ready`` のまま（LSP プロセスは落とさない。連続 3 回で ``unavailable`` にするのは ``LspPool``）。
    警告は ``lsp_timeout`` の雛形。降格先が ``lsp_unavailable``（RENAME / CODE_ACTION）のときは警告なしで、
    ツールが ``E_LSP_UNAVAILABLE`` を返す。LSP を待っていない選択（``ready`` 以外、``exact`` でない、
    ``LSP_TIMEOUT_MS`` に無い能力）には何もせず ``choice`` を返す。
    """
    timeout_ms = LSP_TIMEOUT_MS.get(cap)
    if (
        timeout_ms is None
        or choice.lsp_status != LspStatus.READY.value
        or choice.confidence is not Confidence.EXACT
    ):
        return choice
    cell = _TABLE[cap][Column.INDEXING]
    warning = None if cell.engine in NOT_PROVIDED_ENGINES else WarningReason.LSP_TIMEOUT
    return _make_choice(replace(cell, warning=warning), LspStatus.READY, None, timeout_ms=timeout_ms)


POSITION_CLAMPED_WARNING: Final = (
    "解決する位置が文字の境界に当たらないため LSP に要求を送らず構文木で解決しました。"
    "型・オーバーロード・再エクスポート越しの参照は区別されていません。"
)
"""``lsp.features`` が丸めの要る位置を送らずに返した ``E_LSP_UNAVAILABLE``（``details.reason="position_clamped"``）の降格の警告文。
LSP は動いているので ``lsp_unavailable`` の案内（``configure_workspace status``）は付けない。設計書 §7.8.4 の表には行が無い（理由別の警告ではなく
この規則の定数）。"""

_INDEXING_REASONS: Final = frozenset({"indexing"})
_DOWN_REASONS: Final = frozenset({"standby", "unavailable", "disconnected"})
_DEGRADING_REASONS: Final = _INDEXING_REASONS | _DOWN_REASONS | {"position_clamped", "lsp_error"}


def _lsp_reason(exc: BaseException) -> str | None:
    """``E_LSP_UNAVAILABLE`` の ``details.reason``（それ以外の例外は ``None``）。"""
    if isinstance(exc, LuciankeError) and exc.code is ErrorCode.E_LSP_UNAVAILABLE:
        return str(exc.details.get("reason", ""))
    return None


def is_lsp_degradation(exc: BaseException) -> bool:
    """この例外は「LSP が使えなかった」降格の合図か。

    * ``TimeoutError``（``lsp.features`` が上限で打ち切ったもの）。
    * ``LuciankeError(E_LSP_UNAVAILABLE)`` で ``details.reason`` が ``indexing`` / ``standby`` / ``unavailable`` / ``disconnected`` /
      ``position_clamped`` / ``lsp_error``。
    それ以外（``prepare_rename_rejected`` など LSP の判断の結果、``E_TARGET_NOT_FOUND`` など入力の誤り）は降格ではない。
    """
    if isinstance(exc, TimeoutError):
        return True
    reason = _lsp_reason(exc)
    return reason is not None and reason in _DEGRADING_REASONS


def on_lsp_failure(choice: EngineChoice, cap: Capability, exc: BaseException) -> EngineChoice:
    """LSP に要求を送った結果が失敗（降格の合図）だったとき、その要求に限り右隣の列（構文木・索引）へ降格する。

    * ``TimeoutError``: ``on_lsp_timeout``（``lsp_status`` は ``ready`` のまま、``lsp_timeout`` の警告）。
    * ``E_LSP_UNAVAILABLE`` の ``reason``:
        - ``indexing``: 列 2（起動・索引中）。``lsp_status`` は ``indexing``、警告は ``lsp_indexing``（進捗は不明なので省く）。
        - ``standby`` / ``unavailable`` / ``disconnected``: 列 3（利用不可）。``lsp_status`` は ``unavailable``、警告は ``lsp_unavailable``。
          選択（``select``）の後に LSP が落ちた競合で起きる。
        - ``position_clamped``: 位置が文字境界に当たらず要求を送らなかった。LSP は動いているので ``lsp_status`` は ``ready`` のまま、
          警告は ``POSITION_CLAMPED_WARNING``。
        - ``lsp_error``: サーバーがエラー応答を返した。``lsp_status`` は ``ready`` のまま、警告は ``lsp_unavailable``。
    * 降格先が ``lsp_unavailable``（RENAME / CODE_ACTION）のときは警告なしで、ツールが ``E_LSP_UNAVAILABLE`` を返す
      （``on_lsp_timeout`` と同じ）。
    * LSP に要求を送らない選択（``ready`` 以外、``exact`` でない、LSP に要求しない能力）には何もせず ``choice`` を返す（降格は 1 段だけ）。
    * 降格の合図でない例外（``is_lsp_degradation`` が偽）は、そのまま ``raise exc``。
    """
    if not is_lsp_degradation(exc):
        raise exc
    if isinstance(exc, TimeoutError):
        return on_lsp_timeout(choice, cap)
    if (
        cap not in _REQUESTS_LSP
        or choice.lsp_status != LspStatus.READY.value
        or choice.confidence is not Confidence.EXACT
    ):
        return choice
    reason = _lsp_reason(exc) or ""
    if reason in _INDEXING_REASONS:
        column, status = Column.INDEXING, LspStatus.INDEXING
    elif reason in _DOWN_REASONS:
        column, status = Column.UNAVAILABLE, LspStatus.UNAVAILABLE
    else:  # position_clamped / lsp_error
        column, status = Column.INDEXING, LspStatus.READY
    cell = _TABLE[cap][column]
    if cell.engine in NOT_PROVIDED_ENGINES:
        return _make_choice(replace(cell, warning=None), status, None)
    if reason == "position_clamped":
        warning_cell = replace(cell, warning=None)
        made = _make_choice(warning_cell, status, None)
        return replace(made, warning=POSITION_CLAMPED_WARNING)
    why = {
        "indexing": WarningReason.LSP_INDEXING,
        "lsp_error": WarningReason.LSP_UNAVAILABLE,
    }.get(reason, WarningReason.LSP_UNAVAILABLE)
    return _make_choice(replace(cell, warning=why), status, None)


def on_nothing_found(choice: EngineChoice, cap: Capability) -> EngineChoice:
    """Tier 4 の ctags 系（heuristic）で何も取れなかったとき、表の ``→`` の字句降格（grep）へ 1 段降りる。

    heuristic でない選択や、降格先の無い能力は ``choice`` をそのまま返す。``lsp_status`` は引き継ぐ。
    """
    cell = _TABLE[cap][Column.NO_GRAMMAR]
    if choice.confidence is not Confidence.HEURISTIC or cell.fallback is None:
        return choice
    return _make_choice(cell.fallback, LspStatus(choice.lsp_status), choice.lsp_progress)
