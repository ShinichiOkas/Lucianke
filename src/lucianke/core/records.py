"""出力の中間表現（設計書 ADR-4、ADR-5、§5）。

ツールハンドラは **優先順位つきのレコード列** (``ToolOutput``) を返すだけにする。
パッカーが予算内に詰め、カーソルと要約を作り、レンダラが形式に直す。ハンドラは出力形式を知らない。
精度（``confidence``）はレコードが持ち、メタデータ（``ResolutionMeta``）は ``aggregate_confidence`` で集計する。
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from lucianke.core.model import Confidence, EngineChoice, Tier


@dataclass(frozen=True, slots=True, kw_only=True)
class Record:
    """パッカーが詰める最小単位。レコードは途中で切らない（仕様書 §5.6）。

    Attributes:
        kind: ``"file-block" | "symbol" | "ref" | "diag" | "tree" | "path" | "code" | "kv" | "note" | "row"``。
        key: 安定キー（ページ跨ぎの重複排除・並び替えに使う）。
        priority: 小さいほど先に詰める。ツールが付ける。
        lines: compact 表現（仕様書 §6.1 の 1 ブロック）。レンダラは連結するだけ。
        data: json 表現（``data`` 配列の 1 要素）。
        confidence: このレコードの精度水準。
        group: 要約の単位（例: ファイルパス）。
        tokens: パッカーが推定して埋める。
        shrink: 任意。残り予算（トークン）で収まる縮退版（T-02 の ``doc:`` → ``urn:`` → 非公開 → 入れ子の順の省略）を返す。
            収められなければ ``None``。パッカーは「入らないレコード」にこれがあれば一度だけ呼ぶ。
    """

    kind: str
    key: str
    priority: int
    lines: tuple[str, ...]
    data: Mapping[str, Any]
    confidence: Confidence
    group: str | None = None
    tokens: int | None = None
    shrink: Callable[[int], Record | None] | None = None

    def with_tokens(self, tokens: int) -> Record:
        return dataclasses.replace(self, tokens=tokens)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolOutput:
    """ツールハンドラの戻り値。

    Attributes:
        records: 優先順位順に並べて渡す。
        head_kv: compact のヘッダ直後に出す kv 行（``target:``, ``rows:`` など）。常に含める。
        data_static: json の ``data`` のうち、レコード配列以外（totals, target, summary など）。
        max_confidence: このツールが到達しうる最高水準（ADR-5、仕様書 §8.3）。
        engine: 代表のエンジン選択（``lsp_status``、``warning`` の元）。
        page_unit: 1 レコードの意味（事後条件の文言と一致させる）。
        summarize: 残りの要約文（``# summary:`` 行）を作る関数。
        warnings: ハンドラが応答に添える警告（仕様書 §5.4.4: バッチの一部失敗の件数など）。パイプラインが
            ``RenderInput.warnings`` へ流し、compact は ``! `` 行、json は ``warnings[]`` になる。
    """

    records: Sequence[Record]
    max_confidence: Confidence
    engine: EngineChoice
    page_unit: str
    head_kv: Sequence[tuple[str, str]] = ()
    data_static: Mapping[str, Any] = field(default_factory=dict)
    summarize: Callable[[Sequence[Record]], str] | None = None
    tier: Tier = Tier.T1  # _resolution_meta.tier（混在時は最も大きい数）
    detected_language: str = "n/a"  # _resolution_meta.detected_language
    # 並び全体の件数（ツールが知っているとき）。``len(records)`` より大きければ、``records`` は先頭の接頭辞で、差は具体化して
    # いない続き（遅延列。パッカーは全部入ってもカーソルを出す。設計書 §7.2.2）。``None`` または以下なら ``records`` が全体
    total_estimate: int | None = None
    warnings: Sequence[str] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolutionMeta:
    """``_resolution_meta``（仕様書 §5.5）。"""

    confidence_level: Confidence
    is_degraded: bool
    tier: Tier
    detected_language: str
    engine_used: str
    lsp_status: str
    lsp_progress: int | None = None
    mixed: bool = False
    confidence_breakdown: Mapping[Confidence, int] = field(default_factory=dict)
    accuracy_warning: str | None = None

    def to_json(self) -> dict[str, Any]:
        """§5.5 の JSON。``confidence_breakdown`` は高い水準から、件数 0 の水準は出さない。"""
        breakdown = {
            level.value: n
            for level in sorted(self.confidence_breakdown, reverse=True)
            if (n := self.confidence_breakdown[level]) > 0
        }
        return {
            "confidence_level": self.confidence_level.value,
            "is_degraded": self.is_degraded,
            "tier": int(self.tier),
            "detected_language": self.detected_language,
            "engine_used": self.engine_used,
            "lsp_status": str(self.lsp_status),
            "lsp_progress": self.lsp_progress,
            "mixed": self.mixed,
            "confidence_breakdown": breakdown,
            "accuracy_warning": self.accuracy_warning,
        }


class HasConfidence(Protocol):
    """``confidence`` を持つもの（``Record``、``SymbolRecord`` など）。"""

    @property
    def confidence(self) -> Confidence: ...


def aggregate_confidence(
    records: Iterable[HasConfidence],
    max_confidence: Confidence,
    *,
    empty_level: Confidence | None = None,
) -> tuple[Confidence, dict[Confidence, int], bool, bool]:
    """レコード列から ``(confidence_level, confidence_breakdown, mixed, is_degraded)`` を集計する（ADR-5）。

    * ``confidence_level``: 最も低い水準（仕様書 §5.5）。
    * ``confidence_breakdown``: 水準ごとの件数。高い水準から、件数 0 の水準は含めない。
    * ``mixed``: 2 水準以上が混在するとき ``True``。
    * ``is_degraded``: ``confidence_level`` が **ツールの最高水準 ``max_confidence`` より低い** とき ``True``
      （仕様書 §8.3。最高水準が ``structural`` のツールでは ``structural`` は降格ではない）。

    レコードが 0 件のときは ``(max_confidence, {}, False, False)``。0 件でも降格を表したいとき
    （単語境界 grep の結果が 0 件、など）は ``empty_level``（通常は ``ToolOutput.engine.confidence``）を渡す。
    その水準を ``confidence_level`` とし、``max_confidence`` より低ければ ``is_degraded`` を ``True`` にする。
    """
    counts = Counter(r.confidence for r in records)
    if not counts:
        level = max_confidence if empty_level is None else empty_level
        return (level, {}, False, level < max_confidence)
    level = min(counts)
    breakdown = {c: counts[c] for c in sorted(counts, reverse=True)}
    return (level, breakdown, len(breakdown) > 1, level < max_confidence)
