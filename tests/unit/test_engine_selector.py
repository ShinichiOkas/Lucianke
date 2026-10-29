"""engine.selector のテスト（設計書 ADR-6・ADR-10、§7.8.2〜§7.8.4、仕様書 §8.2〜§8.4）。

期待値は設計書 §7.8.2 の表から **テスト側で別に起こした** リテラル（モジュールの表を写さない）。
警告文は仕様書 §8.4 と設計書 §7.8.4 の文面を文書から読んで突き合わせる。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from lucianke.core import limits
from lucianke.core.model import Capability, Confidence, EngineChoice, FileInfo, LspStatus, RelPath, Tier
from lucianke.engine import selector
from lucianke.engine.selector import (
    DEFAULT_WAIT,
    LEVEL_TEMPLATES,
    LSP_TIMEOUT_MS,
    NOT_PROVIDED_ENGINES,
    WaitPolicy,
    WarningReason,
    is_degraded,
    on_lsp_timeout,
    on_nothing_found,
    select,
    starts_lsp,
    warning_text,
)

DOCS = Path(__file__).resolve().parents[2] / "docs"
SPEC = (DOCS / "Lucianke仕様書.md").read_text(encoding="utf-8")
DESIGN = (DOCS / "Lucianke アーキテクチャ設計書.md").read_text(encoding="utf-8")

C = Capability
E, S, H, L = Confidence.EXACT, Confidence.STRUCTURAL, Confidence.HEURISTIC, Confidence.LEXICAL_FALLBACK
GREP = "internal_ripgrep_word_boundary"
LSP_DOWN = "lsp_unavailable"

# 警告の理由 -> 文面（progress=50 のとき）。仕様書 §8.4・設計書 §7.8.4 のリテラル
SPEC_STRUCTURAL = "LSP が利用できないため構文木のみで解決しました。型・オーバーロード・再エクスポート越しの参照は区別されていません。"
TEXT = {
    None: None,
    "lsp_indexing": (
        "LSP が起動・索引中（50%）のため構文木で解決しました。"
        "型・オーバーロード・再エクスポート越しの参照は区別されていません。完了後に呼び直すと `exact` になります。"
    ),
    "lsp_unavailable": SPEC_STRUCTURAL + "`configure_workspace status` で LSP の状態を確認してください。",
    "no_grammar_ctags": (
        "言語の文法が無いため ctags とブロック推定で境界を求めました。"
        "終了行は推定であり、編集前に `get_symbol_bodies` で範囲を確かめてください。"
    ),
    "no_grammar_lexical": (
        "未知の言語のため単語境界 grep に降格しました。同名の誤検知と動的参照の見落としが含まれます。"
    ),
}
IDX, UNA, CT, LX = "lsp_indexing", "lsp_unavailable", "no_grammar_ctags", "no_grammar_lexical"

READY, INDEXING, UNAVAILABLE, NO_GRAMMAR = range(4)
COLUMN_NAMES = ["ready", "indexing", "unavailable", "no_grammar"]


def _down(engine: str) -> list[tuple[str, Confidence, str | None]]:
    return [(engine, S, IDX), (engine, S, UNA)]


# 設計書 §7.8.2 の判定表。能力 -> [列0: ready, 列1: indexing, 列2: unavailable, 列3: Tier 4（ctags あり）]
# セル = (engine, confidence, 警告の理由)
GRID: dict[Capability, list[tuple[str, Confidence, str | None]]] = {
    C.SKELETON: [("tree-sitter", E, None), *_down("tree-sitter"), ("ctags", H, CT)],
    C.SEARCH: [("fts5", E, None), *_down("fts5"), ("fts5", H, CT)],
    C.BODY: [("tree-sitter", E, None), *_down("tree-sitter"), ("ctags+block", H, CT)],
    C.DEFINITION: [("lsp", E, None), *_down("tree-sitter+imports"), ("ctags", H, CT)],
    C.REFERENCES: [("lsp", E, None), *_down("relations"), (GREP, L, LX)],
    C.IMPLEMENTATIONS: [
        ("lsp", E, None),
        *_down("relations(IMPLEMENTS, INHERITS)"),
        ("unavailable", L, None),
    ],
    C.CALL_HIERARCHY: [("lsp", E, None), *_down("relations(CALLS)"), (GREP, L, LX)],
    C.DIAGNOSTICS: [("lsp-cache+tree-sitter", E, None), *_down("tree-sitter"), ("none", L, None)],
    C.RENAME: [("lsp", E, None), (LSP_DOWN, L, None), (LSP_DOWN, L, None), ("lexical-replace", L, LX)],
    C.CODE_ACTION: [("lsp", E, None), (LSP_DOWN, L, None), (LSP_DOWN, L, None), (LSP_DOWN, L, None)],
    C.STRUCTURAL_EDIT: [
        ("ast-grep", S, None),
        ("ast-grep", S, None),
        ("ast-grep", S, None),
        ("excluded", L, None),
    ],
    C.SYMBOL_EDIT: [("tree-sitter", E, None), *_down("tree-sitter"), ("ctags+block", H, CT)],
}


def _file(tier: Tier = Tier.T1, language: str = "python") -> FileInfo:
    return FileInfo(
        file_id=1,
        workspace_id="ws",
        path=RelPath("a/b.py"),
        language=language,
        tier=tier,
        sha256="0" * 64,
        line_count=10,
        byte_size=100,
        is_test=False,
        confidence=Confidence.EXACT,
    )


def _select_col(cap: Capability, column: int, *, has_ctags: bool = True, **kw: object) -> EngineChoice:
    """列ごとの入力でそのセルに落とす。"""
    args: dict[str, object] = {
        READY: {"file": _file(), "lsp_status": LspStatus.READY, "lsp_progress": None, "has_grammar": True},
        INDEXING: {
            "file": _file(),
            "lsp_status": LspStatus.INDEXING,
            "lsp_progress": 50,
            "has_grammar": True,
        },
        UNAVAILABLE: {
            "file": _file(),
            "lsp_status": LspStatus.UNAVAILABLE,
            "lsp_progress": None,
            "has_grammar": True,
        },
        NO_GRAMMAR: {
            "file": _file(Tier.T4, "cobol"),
            "lsp_status": LspStatus.NOT_APPLICABLE,
            "lsp_progress": None,
            "has_grammar": False,
        },
    }[column]
    return select(cap=cap, has_ctags=has_ctags, **{**args, **kw})  # type: ignore[arg-type]


# --- 判定表（設計書 §7.8.2）の全セル ------------------------------------------------------


def test_grid_covers_every_capability() -> None:
    assert set(GRID) == set(Capability)
    assert len(GRID) * 4 == 48


@pytest.mark.parametrize("column", range(4), ids=COLUMN_NAMES)
@pytest.mark.parametrize("cap", list(Capability), ids=lambda c: c.value)
def test_decision_table_cell(cap: Capability, column: int) -> None:
    engine, confidence, reason = GRID[cap][column]
    choice = _select_col(cap, column)
    assert (choice.engine, choice.confidence) == (engine, confidence)
    assert choice.warning == TEXT[reason]
    assert choice.lsp_status == ["ready", "indexing", "unavailable", "n/a"][column]
    assert choice.lsp_progress == (50 if column == INDEXING else None)
    assert (choice.engine in NOT_PROVIDED_ENGINES) == (confidence is L and reason is None)


def test_not_provided_engines() -> None:
    assert NOT_PROVIDED_ENGINES == {"lsp_unavailable", "unavailable", "none", "excluded"}


# --- LSP の状態の写し方 -------------------------------------------------------------------


def test_status_accepts_plain_strings() -> None:
    status: Any = "ready"  # LspPool が文字列で返しても受ける
    choice = select(
        _file(), C.DEFINITION, lsp_status=status, lsp_progress=None, has_grammar=True, has_ctags=True
    )
    assert (choice.engine, choice.confidence) == ("lsp", E)


def test_starting_is_shown_as_indexing_without_progress() -> None:
    choice = select(
        _file(),
        C.REFERENCES,
        lsp_status=LspStatus.INDEXING,
        lsp_progress=None,
        has_grammar=True,
        has_ctags=True,
    )
    assert choice.lsp_status == "indexing"
    assert choice.lsp_progress is None
    assert choice.warning is not None
    assert "（" not in choice.warning.split("のため")[0]  # 進捗が無いときは括弧ごと省く
    assert choice.warning.startswith("LSP が起動・索引中のため構文木で解決しました。")


@pytest.mark.parametrize(("raw", "shown"), [(0, 0), (42, 42), (100, 100), (-5, 0), (250, 100)])
def test_progress_is_clamped_and_shown(raw: int, shown: int) -> None:
    choice = _select_col(C.DEFINITION, INDEXING, lsp_progress=raw)
    assert choice.lsp_progress == shown
    assert choice.warning is not None
    assert f"（{shown}%）" in choice.warning


@pytest.mark.parametrize("status", [LspStatus.READY, LspStatus.UNAVAILABLE, LspStatus.NOT_APPLICABLE])
def test_progress_is_dropped_unless_indexing(status: LspStatus) -> None:
    choice = select(
        _file(), C.DEFINITION, lsp_status=status, lsp_progress=77, has_grammar=True, has_ctags=True
    )
    assert choice.lsp_progress is None


def test_no_lsp_definition_goes_to_unavailable_column_with_na_status() -> None:
    choice = select(
        _file(language="mermaid"),
        C.DEFINITION,
        lsp_status=LspStatus.NOT_APPLICABLE,
        lsp_progress=None,
        has_grammar=True,
        has_ctags=True,
    )
    assert (choice.engine, choice.confidence) == ("tree-sitter+imports", S)
    assert choice.lsp_status == "n/a"
    assert choice.warning == TEXT[UNA]


def test_standby_starts_lsp_and_shows_indexing_for_lsp_capabilities() -> None:
    """設計書 §8.3 の 1 回目: standby -> ensure_started の契機 -> indexing の列（進捗 null）。"""
    for cap in (
        C.DEFINITION,
        C.REFERENCES,
        C.IMPLEMENTATIONS,
        C.CALL_HIERARCHY,
        C.DIAGNOSTICS,
        C.RENAME,
        C.CODE_ACTION,
    ):
        assert starts_lsp(cap)
        choice = select(
            _file(), cap, lsp_status=LspStatus.STANDBY, lsp_progress=None, has_grammar=True, has_ctags=True
        )
        engine, confidence, _ = GRID[cap][INDEXING]
        assert (choice.engine, choice.confidence) == (engine, confidence)
        assert choice.lsp_status == "indexing"
        assert choice.lsp_progress is None


def test_standby_does_not_pretend_to_be_indexing_for_other_capabilities() -> None:
    for cap in (C.SKELETON, C.SEARCH, C.BODY, C.STRUCTURAL_EDIT, C.SYMBOL_EDIT):
        assert not starts_lsp(cap)
        choice = select(
            _file(), cap, lsp_status=LspStatus.STANDBY, lsp_progress=None, has_grammar=True, has_ctags=True
        )
        engine, confidence, reason = GRID[cap][UNAVAILABLE]
        assert (choice.engine, choice.confidence) == (engine, confidence)
        assert choice.lsp_status == "standby"
        assert choice.warning == TEXT[reason]


def test_tier3_adapter_is_always_structural_whatever_the_lsp_status() -> None:
    for status in LspStatus:
        choice = select(
            _file(Tier.T3, "yaml"),
            C.DEFINITION,
            lsp_status=status,
            lsp_progress=9,
            has_grammar=True,
            has_ctags=True,
        )
        assert (choice.engine, choice.confidence) == ("tree-sitter+imports", S)
        assert choice.lsp_status == "n/a"
        assert choice.lsp_progress is None


def test_tier2_adapter_follows_the_lsp_column() -> None:
    choice = select(
        _file(Tier.T2, "ipynb"),
        C.DEFINITION,
        lsp_status=LspStatus.READY,
        lsp_progress=None,
        has_grammar=True,
        has_ctags=False,
    )
    assert (choice.engine, choice.confidence) == ("lsp", E)


def test_grammar_less_file_ignores_lsp_status_and_tier() -> None:
    choice = select(
        _file(Tier.T1, "weird"),
        C.REFERENCES,
        lsp_status=LspStatus.READY,
        lsp_progress=None,
        has_grammar=False,
        has_ctags=True,
    )
    assert (choice.engine, choice.confidence, choice.lsp_status) == (GREP, L, "n/a")


def test_lsp_name_is_used_in_engine_used() -> None:
    choice = _select_col(C.DEFINITION, READY, lsp_name="pyright")
    assert choice.engine == "pyright+tree-sitter"
    assert choice.confidence is E
    assert _select_col(C.DEFINITION, READY).engine == "lsp"
    assert _select_col(C.SKELETON, READY, lsp_name="pyright").engine == "tree-sitter"  # LSP を使わない能力
    assert _select_col(C.DEFINITION, UNAVAILABLE, lsp_name="pyright").engine == "tree-sitter+imports"


def test_selection_has_no_side_effects_and_is_deterministic() -> None:
    assert _select_col(C.DEFINITION, INDEXING) == _select_col(C.DEFINITION, INDEXING)


# --- 待つ／待たない（設計書 §7.8.3、ADR-6） -----------------------------------------------


def test_default_wait_policy_table() -> None:
    wait = {C.DEFINITION, C.REFERENCES, C.IMPLEMENTATIONS, C.CALL_HIERARCHY, C.RENAME, C.CODE_ACTION}
    assert set(DEFAULT_WAIT) == set(Capability)
    for cap in Capability:
        assert DEFAULT_WAIT[cap] is (WaitPolicy.WAIT_SHORT if cap in wait else WaitPolicy.NO_WAIT), cap


def test_timeout_limits_come_from_core_limits() -> None:
    assert dict(LSP_TIMEOUT_MS) == {
        C.DEFINITION: 3000,
        C.REFERENCES: 3000,
        C.IMPLEMENTATIONS: 3000,
        C.CALL_HIERARCHY: 5000,
        C.RENAME: 10000,
        C.CODE_ACTION: 5000,
    }
    assert LSP_TIMEOUT_MS[C.DEFINITION] == limits.LSP_TIMEOUT_DEFINITION_MS
    assert LSP_TIMEOUT_MS[C.REFERENCES] == limits.LSP_TIMEOUT_REFERENCES_MS
    assert LSP_TIMEOUT_MS[C.CALL_HIERARCHY] == limits.LSP_TIMEOUT_CALL_HIERARCHY_MS
    assert LSP_TIMEOUT_MS[C.RENAME] == limits.LSP_TIMEOUT_RENAME_MS
    assert LSP_TIMEOUT_MS[C.CODE_ACTION] == limits.LSP_TIMEOUT_CODE_ACTION_MS
    assert {cap for cap in Capability if DEFAULT_WAIT[cap] is WaitPolicy.WAIT_SHORT} == set(LSP_TIMEOUT_MS)


@pytest.mark.parametrize("cap", [C.DEFINITION, C.REFERENCES, C.IMPLEMENTATIONS, C.CALL_HIERARCHY])
def test_no_wait_override_answers_from_the_syntax_tree_without_lsp(cap: Capability) -> None:
    choice = _select_col(cap, READY, wait_policy=WaitPolicy.NO_WAIT)
    engine, confidence, _ = GRID[cap][INDEXING]
    assert (choice.engine, choice.confidence) == (engine, confidence)
    assert choice.lsp_status == "ready"  # LSP は ready のまま、ただ待っていない
    assert choice.warning is None  # 「起動・索引中」の警告は当たらない


@pytest.mark.parametrize("cap", [C.RENAME, C.CODE_ACTION])
def test_no_wait_override_cannot_provide_rename_or_code_action(cap: Capability) -> None:
    choice = _select_col(cap, READY, wait_policy=WaitPolicy.NO_WAIT)
    assert choice.engine == "lsp_unavailable"


@pytest.mark.parametrize(
    "cap", [C.SKELETON, C.SEARCH, C.BODY, C.DIAGNOSTICS, C.STRUCTURAL_EDIT, C.SYMBOL_EDIT]
)
def test_wait_short_override_changes_nothing_for_capabilities_that_never_call_lsp(cap: Capability) -> None:
    assert _select_col(cap, READY, wait_policy=WaitPolicy.WAIT_SHORT) == _select_col(cap, READY)


def test_explicit_default_policy_equals_none() -> None:
    for cap in Capability:
        assert _select_col(cap, READY, wait_policy=DEFAULT_WAIT[cap]) == _select_col(cap, READY)


# --- タイムアウトの降格（右隣の列へ 1 段） ------------------------------------------------


@pytest.mark.parametrize("cap", list(LSP_TIMEOUT_MS), ids=lambda c: c.value)
def test_timeout_demotes_to_the_right_neighbour_column(cap: Capability) -> None:
    choice = on_lsp_timeout(_select_col(cap, READY), cap)
    engine, confidence, _ = GRID[cap][INDEXING]
    assert (choice.engine, choice.confidence) == (engine, confidence)
    assert (
        choice.lsp_status == "ready"
    )  # LSP プロセスは落とさない（連続 3 回で unavailable にするのは LspPool）
    assert choice.lsp_progress is None
    if engine == LSP_DOWN:  # RENAME / CODE_ACTION: 待たなければ提供できない
        assert choice.warning is None
    else:
        assert (
            choice.warning
            == f"LSP の応答が {LSP_TIMEOUT_MS[cap]} ms 内に得られなかったため構文木で解決しました。"
        )


def test_timeout_warning_uses_each_limit() -> None:
    assert "3000 ms" in (on_lsp_timeout(_select_col(C.DEFINITION, READY), C.DEFINITION).warning or "")
    assert "5000 ms" in (on_lsp_timeout(_select_col(C.CALL_HIERARCHY, READY), C.CALL_HIERARCHY).warning or "")


def test_timeout_keeps_the_pyright_style_engine_name_out_of_the_demoted_choice() -> None:
    demoted = on_lsp_timeout(_select_col(C.DEFINITION, READY, lsp_name="pyright"), C.DEFINITION)
    assert demoted.engine == "tree-sitter+imports"


@pytest.mark.parametrize(
    "cap", [C.SKELETON, C.SEARCH, C.BODY, C.DIAGNOSTICS, C.STRUCTURAL_EDIT, C.SYMBOL_EDIT]
)
def test_timeout_is_a_no_op_for_no_wait_capabilities(cap: Capability) -> None:
    choice = _select_col(cap, READY)
    assert on_lsp_timeout(choice, cap) is choice


def test_timeout_is_a_no_op_when_lsp_was_not_waited_for() -> None:
    for column in (INDEXING, UNAVAILABLE, NO_GRAMMAR):
        choice = _select_col(C.DEFINITION, column)
        assert on_lsp_timeout(choice, C.DEFINITION) is choice
    skipped = _select_col(C.DEFINITION, READY, wait_policy=WaitPolicy.NO_WAIT)
    assert on_lsp_timeout(skipped, C.DEFINITION) is skipped


def test_timeout_demotion_is_one_step_only() -> None:
    once = on_lsp_timeout(_select_col(C.REFERENCES, READY), C.REFERENCES)
    assert on_lsp_timeout(once, C.REFERENCES) is once


# --- Tier 4: ctags の有無と「何も取れなければ」の字句降格 -------------------------------------


@pytest.mark.parametrize(
    ("cap", "engine", "confidence", "reason"),
    [
        (C.SKELETON, "block-heuristic", H, CT),  # ctags が無くてもインデント/括弧のアウトライン
        (C.SEARCH, GREP, L, LX),
        (C.BODY, GREP, L, LX),
        (C.DEFINITION, GREP, L, LX),
        (C.SYMBOL_EDIT, GREP, L, LX),
        (C.REFERENCES, GREP, L, LX),  # ctags を要さないものは変わらない
        (C.CALL_HIERARCHY, GREP, L, LX),
        (C.RENAME, "lexical-replace", L, LX),
        (C.IMPLEMENTATIONS, "unavailable", L, None),
        (C.DIAGNOSTICS, "none", L, None),
        (C.CODE_ACTION, "lsp_unavailable", L, None),
        (C.STRUCTURAL_EDIT, "excluded", L, None),
    ],
    ids=lambda v: getattr(v, "value", v) if not isinstance(v, str) else v,
)
def test_tier4_without_ctags(
    cap: Capability, engine: str, confidence: Confidence, reason: str | None
) -> None:
    choice = _select_col(cap, NO_GRAMMAR, has_ctags=False)
    assert (choice.engine, choice.confidence, choice.warning) == (engine, confidence, TEXT[reason])


@pytest.mark.parametrize("cap", [C.SKELETON, C.SEARCH, C.BODY, C.DEFINITION, C.SYMBOL_EDIT])
def test_nothing_found_falls_from_heuristic_to_lexical(cap: Capability) -> None:
    first = _select_col(cap, NO_GRAMMAR)
    assert first.confidence is H
    second = on_nothing_found(first, cap)
    assert (second.engine, second.confidence, second.warning) == (GREP, L, TEXT[LX])
    assert second.lsp_status == "n/a"
    assert on_nothing_found(second, cap) is second  # もう降りない


def test_nothing_found_from_block_heuristic_goes_to_grep() -> None:
    first = _select_col(C.SKELETON, NO_GRAMMAR, has_ctags=False)
    assert first.engine == "block-heuristic"
    assert on_nothing_found(first, C.SKELETON).engine == GREP


@pytest.mark.parametrize("cap", [C.REFERENCES, C.CALL_HIERARCHY, C.RENAME, C.IMPLEMENTATIONS, C.DIAGNOSTICS])
def test_nothing_found_leaves_capabilities_without_a_lower_rung_alone(cap: Capability) -> None:
    choice = _select_col(cap, NO_GRAMMAR)
    assert on_nothing_found(choice, cap) is choice


def test_nothing_found_does_not_touch_grammar_choices() -> None:
    choice = _select_col(C.SKELETON, UNAVAILABLE)
    assert on_nothing_found(choice, C.SKELETON) is choice


# --- is_degraded（仕様書 §8.3） ------------------------------------------------------------


@pytest.mark.parametrize(
    ("got", "tool_max", "expected"),
    [
        (E, E, False),
        (S, E, True),
        (H, E, True),
        (L, E, True),
        (S, S, False),  # T-12: 最高水準が structural のツールは structural でも降格ではない
        (E, S, False),
        (H, S, True),
        (L, S, True),
        (L, L, False),
    ],
)
def test_is_degraded(got: Confidence, tool_max: Confidence, expected: bool) -> None:
    assert is_degraded(got, tool_max) is expected


def test_t12_structural_edit_is_never_degraded() -> None:
    for column in (READY, INDEXING, UNAVAILABLE):
        assert not is_degraded(_select_col(C.STRUCTURAL_EDIT, column).confidence, S)


def test_is_degraded_follows_the_selected_cell() -> None:
    assert not is_degraded(_select_col(C.DEFINITION, READY).confidence, E)
    assert is_degraded(_select_col(C.DEFINITION, INDEXING).confidence, E)
    assert is_degraded(on_lsp_timeout(_select_col(C.DEFINITION, READY), C.DEFINITION).confidence, E)


# --- 警告文の雛形（仕様書 §8.4、設計書 §7.8.4） --------------------------------------------


def _spec_templates() -> dict[str, str]:
    """仕様書 §8.4 の「警告文の雛形」表から {水準: 文面} を読む。"""
    start = SPEC.index("警告文の雛形（日本語")
    section = SPEC[start : SPEC.index("## 8.5", start)]
    found = dict(re.findall(r"^\| `(\w+)` \| 「(.+)」 \|$", section, flags=re.MULTILINE))
    assert set(found) == {"structural", "heuristic", "lexical_fallback"}
    return found


def _design_rows() -> dict[str, tuple[str, str]]:
    """設計書 §7.8.4 の表から {reason: (水準, 行全体)} を読む。"""
    start = DESIGN.index("### 7.8.4")
    section = DESIGN[start : DESIGN.index("## 7.9", start)]
    rows = re.findall(r"^\| `(\w+)` \| ([^|]+?) \| (.+) \|$", section, flags=re.MULTILINE)
    return {reason: (level, rest) for reason, level, rest in rows if reason != "reason"}  # ヘッダ行を除く


def test_level_templates_match_spec_section_8_4() -> None:
    spec = _spec_templates()
    assert {level.value: text for level, text in LEVEL_TEMPLATES.items()} == spec
    assert LEVEL_TEMPLATES[S] == SPEC_STRUCTURAL == spec["structural"]


def test_reason_texts_match_design_section_7_8_4() -> None:
    spec = _spec_templates()
    rows = _design_rows()
    assert set(rows) == {r.value for r in WarningReason}

    indexing = re.search(r"「(.+)」", rows["lsp_indexing"][1])
    assert indexing
    assert warning_text(WarningReason.LSP_INDEXING, progress=37) == indexing[1].replace("{progress}", "37")

    timeout = re.search(r"「(.+)」", rows["lsp_timeout"][1])
    assert timeout
    assert warning_text(WarningReason.LSP_TIMEOUT, timeout_ms=3000) == timeout[1].replace("{timeout}", "3000")

    unavailable_suffix = re.findall(r"「([^」]+)」", rows["lsp_unavailable"][1])[-1]
    assert warning_text(WarningReason.LSP_UNAVAILABLE) == spec["structural"] + unavailable_suffix

    assert warning_text(WarningReason.NO_GRAMMAR_CTAGS) == spec["heuristic"]
    assert warning_text(WarningReason.NO_GRAMMAR_LEXICAL) == spec["lexical_fallback"]

    mixed = re.search(r"「(.+)」", rows["mixed"][1])
    assert mixed
    assert warning_text(WarningReason.MIXED, confidence=S, file_count=4) == spec["structural"] + mixed[
        1
    ].replace("{n}", "4")

    levels = {
        "lsp_indexing": S,
        "lsp_unavailable": S,
        "lsp_timeout": S,
        "no_grammar_ctags": H,
        "no_grammar_lexical": L,
    }
    for reason, level in levels.items():
        assert rows[reason][0] == level.value


def test_selected_warnings_use_the_documented_levels() -> None:
    for cap, cells in GRID.items():
        for column, (_, confidence, reason) in enumerate(cells):
            if reason is None:
                continue
            level = {"lsp_indexing": S, "lsp_unavailable": S, "no_grammar_ctags": H, "no_grammar_lexical": L}[
                reason
            ]
            assert confidence is level, (cap, column)


def test_mixed_uses_the_lowest_level_template() -> None:
    for level in (S, H, L):
        text = warning_text("mixed", confidence=level, file_count=2)
        assert text.startswith(LEVEL_TEMPLATES[level])
        assert text.endswith("2 件のファイルがこの水準です（内訳は `confidence_breakdown`）。")


def test_warning_text_ignores_engine_and_language_but_accepts_them() -> None:
    plain = warning_text(WarningReason.LSP_UNAVAILABLE)
    assert warning_text("lsp_unavailable", confidence=S, engine="tree-sitter", language="python") == plain


def test_warning_text_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        warning_text("nonsense")
    with pytest.raises(ValueError):
        warning_text(WarningReason.LSP_TIMEOUT)
    with pytest.raises(ValueError):
        warning_text(WarningReason.MIXED, confidence=S)
    with pytest.raises(ValueError):
        warning_text(WarningReason.MIXED, file_count=3)
    with pytest.raises(ValueError):
        warning_text(WarningReason.MIXED, confidence=E, file_count=3)  # exact は降格ではない


def test_envelope_templates_do_not_drift_from_the_selector() -> None:
    """mcp は engine を import できない（設計書 §4.2）ので、envelope の複製と一致することをここで守る。"""
    from lucianke.mcp.envelope import ACCURACY_WARNING_TEMPLATES

    assert dict(ACCURACY_WARNING_TEMPLATES) == dict(LEVEL_TEMPLATES)


def test_module_exports_are_consistent() -> None:
    for name in selector.__all__:
        assert hasattr(selector, name), name
