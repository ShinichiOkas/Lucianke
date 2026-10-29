"""``_resolution_meta`` の組み立て（設計書 §7.2、仕様書 §5.5、§8.3、§8.4、ADR-5）。

ツールは精度の集計コードを書かない。レコードが持つ ``confidence`` をパッケージ済みページから集計し、
ツール宣言の最高水準 ``ToolOutput.max_confidence`` と比べて降格かどうかを決める。警告文はエンジン選択の
``EngineChoice.warning`` があればそれ、無ければ仕様書 §8.4 の雛形を水準から選ぶ。
"""

from __future__ import annotations

from lucianke.core.model import Confidence, LspStatus, Tier
from lucianke.core.records import ResolutionMeta, ToolOutput, aggregate_confidence
from lucianke.mcp.types import Page, RenderInput

# 仕様書 §8.4「警告文の雛形」（日本語・``accuracy_warning``）。``exact`` は降格ではないので雛形を持たない。
ACCURACY_WARNING_TEMPLATES: dict[Confidence, str] = {
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


def build_meta(
    out: ToolOutput | None,
    page: Page | None,
    *,
    tier: Tier | None = None,
    detected_language: str | None = None,
) -> ResolutionMeta:
    """``_resolution_meta`` を組み立てる（仕様書 §5.5）。

    * ``out`` が ``None``（失敗応答）のときは ``exact`` ・``n/a`` で作る（失敗応答にも付ける: §5.4.1）。
    * 水準・内訳・``mixed`` ・``is_degraded`` は ``page.records``（予算内に詰めたレコード）から
      ``aggregate_confidence`` で集計する。``page`` が無いときは ``out.records`` 全体を使う。
      レコードが 0 件のときは ``out.engine.confidence`` を水準とする（0 件でも降格を表すため）。
    * ``tier`` ・``detected_language`` を省略すると ``out.tier`` ・``out.detected_language`` を使う
      （``out`` も無ければ ``Tier.T1`` ・``"n/a"``）。
    * ``engine_used`` ・``lsp_status`` ・``lsp_progress`` は ``out.engine`` から。``lsp_progress`` は
      ``indexing`` のときだけ 0〜100 に丸めて出す（他は ``None``）。
    * ``accuracy_warning`` は降格時だけ入れる（MUST）。``out.engine.warning`` があればそれ、無ければ
      仕様書 §8.4 の雛形を水準から選ぶ。``exact`` 応答（および最高水準どおりの応答）は ``None``。
    """
    if out is None:
        return ResolutionMeta(
            confidence_level=Confidence.EXACT,
            is_degraded=False,
            tier=tier if tier is not None else Tier.T1,
            detected_language=detected_language if detected_language is not None else "n/a",
            engine_used="n/a",
            lsp_status=LspStatus.NOT_APPLICABLE.value,
        )
    records = page.records if page is not None else out.records
    level, breakdown, mixed, degraded = aggregate_confidence(
        records, out.max_confidence, empty_level=out.engine.confidence
    )
    lsp_status = str(out.engine.lsp_status)
    progress = out.engine.lsp_progress
    if lsp_status != LspStatus.INDEXING.value or progress is None:
        progress = None
    else:
        progress = max(0, min(100, int(progress)))
    warning: str | None = None
    if degraded:
        warning = out.engine.warning or ACCURACY_WARNING_TEMPLATES.get(level)
    return ResolutionMeta(
        confidence_level=level,
        is_degraded=degraded,
        tier=tier if tier is not None else out.tier,
        detected_language=detected_language if detected_language is not None else out.detected_language,
        engine_used=out.engine.engine,
        lsp_status=lsp_status,
        lsp_progress=progress,
        mixed=mixed,
        confidence_breakdown=breakdown,
        accuracy_warning=warning,
    )


def collect_warnings(inp: RenderInput) -> list[str]:
    """``inp.warnings`` と ``inp.page.warnings`` を、重複を除いて順序を保ったまま 1 本にする。

    パイプラインがどちらに入れても（両方に入れても）compact と json で同じ警告列になるようにする。
    """
    merged: list[str] = list(inp.warnings)
    if inp.page is not None:
        merged.extend(inp.page.warnings)
    return list(dict.fromkeys(merged))
