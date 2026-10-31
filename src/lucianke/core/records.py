"""出力の中間表現（設計書 ADR-4、ADR-5、§5）。

ツールハンドラは **優先順位つきのレコード列** (``ToolOutput``) を返すだけにする。
パッカーが予算内に詰め、カーソルと要約を作り、レンダラが形式に直す。ハンドラは出力形式を知らない。
精度（``confidence``）はレコードが持ち、メタデータ（``ResolutionMeta``）は ``aggregate_confidence`` で集計する。
"""

from __future__ import annotations

import dataclasses
import json
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
        shrink: 任意。残り予算（トークン）で収まる縮退版（T-02 の ``doc:`` → ``urn:`` → 非公開 → 入れ子の順に 1 件ずつの省略）を返す。
            収められなければ ``None``。パッカーは「入らないレコード」にこれがあれば一度だけ呼ぶ。
        resume: 縮退版が **1 レコードの途中で切れている** ときの、続きの位置（ツールが決める 1 以上の整数。T-05 は続きの
            先頭行番号）。縮退してもレコード全体が入る（T-02 の省略など）なら ``None``。これがあるレコードは、パッカーが
            ``next_cursor`` を「同じレコードの ``resume`` から」にして返す（``join_offset``）。続きを呼ぶと、ツールは
            ``split_offset(ctx.cursor_offset)`` で取り出した ``resume`` から同じレコードを作り直す（仕様書 §11 T-05 規約 7）。
        shrink_json: 任意。``shrink`` と同じ縮退の鎖で、json で描いたときの大きさ（``data`` を詰めた JSON と区切りの ``,``。
            ``mcp/render/json_out.record_text``）が ``ask`` トークン以下の最も浅い版。パッカーは json で数えるときこれを先に試し、
            実際に数えて収まればそのまま使う（``shrink`` の ``ask`` を二分探索して版ごとに JSON を作って数えなくて済む。BUG-0078）。
        section: 任意。答えの本体の外の **補足の欄** の名前（T-06 / T-07 の ``exact`` の答えに添える ``name_matched``。BUG-0107）。
            json はこのレコードの ``data`` を ``records_key`` の配列ではなく ``data[section]`` の配列に置く（``data_static`` と
            同じキーにしない）。``_resolution_meta`` の精度の集計（``envelope.build_meta``）には数えない（本体の答えの水準を補足が
            下げない。欄の各要素が自分の ``confidence_level`` を持つ）。compact の行・予算・ページネーションは本体のレコードと
            同じに扱う（並びの順に詰め、続きはカーソルで）。``None`` は本体。
        attached: 任意。このレコードに **付随する行**（T-07 の json の ``edges``: ノードごとの呼び出しの辺。BUG-0136）。
            ``{json のキー: 行の列}``。json は入ったレコードの付随行だけを、レコードの並びの順に ``data[キー]`` の配列へ連ねる
            （同じ行が 2 度出ないよう、同一の行は最初の 1 件だけ）。付随行はレコードの一部として予算に数え（``json_out.record_text``）、
            レコードが入らなければ付随行も入らない（木全体の辺を ``data_static`` に置くと、ノードを 1 件も詰める前に前置きとして
            予算を食う）。compact の行には影響しない。
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
    resume: int | None = None
    shrink_json: Callable[[int], Record | None] | None = None
    section: str | None = None
    attached: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None

    def with_tokens(self, tokens: int) -> Record:
        return dataclasses.replace(self, tokens=tokens)


def json_record_text(rec: Record) -> str:
    """json で描かれるときの 1 レコードの本文（``data`` の JSON と区切りの ``,``、付随行〈``Record.attached``〉の配列）。

    パッカーが json の応答を見積もる単位（``mcp/render/json_out.record_text`` が返す）。縮退版を作るツールも、同じ数え方で
    大きさを測る（BUG-0136）。付随行は ``data[キー]`` の配列に連なるので、配列の括弧まで数えた上限。
    """

    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    text = dumps(dict(rec.data)) + ","
    if rec.attached:
        text += "".join(dumps(list(rows)) for rows in rec.attached.values())
    return text


RESUME_SHIFT = 32  # カーソルのオフセットの上位ビット = レコード内の再開位置（``Record.resume``）。下位 32 ビット = レコードの添字


def join_offset(index: int, resume: int | None) -> int:
    """レコードの添字と、そのレコード内の再開位置を 1 つのカーソルのオフセットにする（``resume`` が無ければ添字のまま）。"""
    return index if resume is None else (resume << RESUME_SHIFT) | index


def split_offset(offset: int) -> tuple[int, int | None]:
    """``join_offset`` の逆。``(レコードの添字, 再開位置または None)``。再開位置の無いオフセット（既存の全ツール）は添字のまま。"""
    index = offset & ((1 << RESUME_SHIFT) - 1)
    resume = offset >> RESUME_SHIFT
    return index, (resume or None)


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
        workspace_id: ハンドラが確定した ``workspace_id``。ワークスペースを作る・変えるツール（T-00 ``set_roots``）が、
            ハンドラの前に解決した値（登録前なら ``None``）と違う値を返すとき、パイプラインが描画前に ``CallInfo.workspace_id`` を
            これに差し替える（ヘッダの ``ws=`` が登録後のアクティブを指す。BUG-0019）。通常のツールは ``None`` のまま。
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
    workspace_id: str | None = None


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
