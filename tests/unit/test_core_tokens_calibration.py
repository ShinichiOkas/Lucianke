"""トークン推定器の較正の回帰テスト（S-12、仕様書 §5.7: ``o200k_base`` 比 ±10%）。

基準値（``o200k_base`` のトークン数）は ``bench/tokens/result.json`` に記録されたもので、ネットワークも tiktoken も使わない。
標本の本文は result.json に無いので、``bench/tokens/calibrate.py`` の ``load_corpus`` と同じ方法でリポジトリ内のファイルを読む。
``src/`` ・``tests/`` ・``docs/`` は開発中に書き換わるため、記録した文字数と一致しないファイルは標本から外す（基準値が
別の本文のものになるため）。推定式を変えたら ``calibrate.py`` を再実行して result.json を更新する。
"""

from __future__ import annotations

import base64
import json
import random
import re
from collections import defaultdict
from pathlib import Path

import pytest

from lucianke.core.tokens import TokenEstimator

ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "bench" / "tokens" / "result.json"
LIMIT = 0.10  # 仕様書 §5.7
MIN_REF = 50  # ファイル単位の平均誤差から外す小さい標本（切り上げで相対誤差が暴れる）。calibrate.py と同じ
MIN_KIND_COVERAGE = 0.5  # 種類ごとの合否に使う、記録した基準トークンのうち照合できた割合の下限。これ未満（多くが書き換わった種類）は判定しない
MIN_TOTAL_TOKENS = 50_000  # 照合できた標本全体の最小の基準トークン数。これ未満は標本が空洞化している

EST = TokenEstimator()

type Sample = tuple[str, str, str, int]  # (種類, 名前, 本文, 基準トークン数)


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", errors="replace")


def _poly_kind(p: Path) -> str:
    n, s = p.name, p.suffix
    if s in {".py", ".ts", ".cob", ".sql", ".proto"}:
        return "poly_code"
    if s == ".ipynb":
        return "poly_ipynb"
    if s in {".json", ".yaml", ".toml", ".xml", ".custom"} or n == ".env":
        return "poly_data"
    return "poly_doc"


def _load_corpus() -> list[tuple[str, str, str]]:
    """``calibrate.py`` の ``load_corpus`` と同じ標本（種類, 名前, 本文）。"""
    out: list[tuple[str, str, str]] = []

    def rel(p: Path) -> str:
        return p.relative_to(ROOT).as_posix()

    for p in sorted((ROOT / "src").rglob("*.py")):
        out.append(("py_src", rel(p), _read(p)))
    for p in sorted((ROOT / "tests").rglob("*.py")):
        if "fixtures/poly" not in rel(p):
            out.append(("py_tests", rel(p), _read(p)))
    for p in sorted((ROOT / "docs").glob("*.md")):
        out.append(("md_ja", rel(p), _read(p)))
    for p in sorted((ROOT / "docs" / "spikes").glob("*.md")):
        if p.name != "S-12.md":
            out.append(("md_spikes", rel(p), _read(p)))
    for p in sorted((ROOT / "tests" / "fixtures" / "poly").rglob("*")):
        if p.is_file() and p.stat().st_size > 0:
            out.append((_poly_kind(p), rel(p), _read(p)))
    spec = _read(ROOT / "docs" / "Lucianke仕様書.md")
    for i, m in enumerate(re.finditer(r"^```([^\n]*)\n(.*?)^```", spec, re.S | re.M)):
        lang, body = m.group(1), m.group(2)
        if lang == "" and re.match(r"# \w+ (ok|partial|error)", body):
            out.append(("compact", f"spec#block{i}:{body.splitlines()[0][:40]}", body))
        elif lang in {"json", "sql"}:
            out.append(("spec_json_sql", f"spec#block{i}:{lang}", body))
    rnd = random.Random(12345)
    b64 = base64.b64encode(rnd.randbytes(6000)).decode()
    out.append(
        (
            "syn_base64",
            "synthetic:base64 8000 chars",
            "\n".join(b64[i : i + 76] for i in range(0, len(b64), 76)),
        )
    )
    out.append(("syn_hex", "synthetic:sha256 hex x60", "\n".join(rnd.randbytes(32).hex() for _ in range(60))))
    return out


RECORDED_FILES: list[dict] = json.loads(RESULT.read_text(encoding="utf-8"))["current"]["files"]
RECORDED_TOKENS: dict[str, int] = defaultdict(int)
for _f in RECORDED_FILES:
    RECORDED_TOKENS[_f["kind"]] += _f["ref"]


def _load_samples() -> list[Sample]:
    """記録された基準値と、本文の文字数が一致する標本だけ（種類, 名前, 本文, 基準）。"""
    recorded = {(f["kind"], f["name"]): f for f in RECORDED_FILES}
    samples: list[Sample] = []
    for kind, name, text in _load_corpus():
        rec = recorded.get((kind, name))
        if rec is not None and rec["chars"] == len(text):
            samples.append((kind, name, text, rec["ref"]))
    return samples


SAMPLES = _load_samples()
BY_KIND: dict[str, list[Sample]] = defaultdict(list)
for _s in SAMPLES:
    BY_KIND[_s[0]].append(_s)


def _covered(kind: str) -> bool:
    """この種類は、記録した基準トークンの半分以上を照合できている（書き換わりで空洞化していない）。"""
    return sum(s[3] for s in BY_KIND.get(kind, [])) >= MIN_KIND_COVERAGE * RECORDED_TOKENS[kind] > 0


def _stats(samples: list[Sample], count) -> tuple[int, int, float, float]:
    """(基準合計, 推定合計, 合計誤差, ファイル単位の平均絶対誤差)。誤差は ``推定 / 基準 − 1``。"""
    ref = sum(s[3] for s in samples)
    ests = [count(s[2]) for s in samples]
    est = sum(ests)
    errs = [abs(e / s[3] - 1) for s, e in zip(samples, ests, strict=True) if s[3] >= MIN_REF]
    return ref, est, est / ref - 1, (sum(errs) / len(errs) if errs else 0.0)


def test_calibration_corpus_is_not_hollow() -> None:
    """記録と照合できた標本が十分にある（書き換わりで全部外れて、何も検証しないテストにならない）。"""
    total = sum(s[3] for s in SAMPLES)
    assert total >= MIN_TOTAL_TOKENS, (
        f"照合できた標本が少ない（基準 {total} トークン）。calibrate.py を再実行して result.json を更新する"
    )
    assert {
        "syn_base64",
        "syn_hex",
    } <= BY_KIND.keys()  # 合成の標本は本文が固定（乱数の種）。base64・hex の塊の検査


@pytest.mark.parametrize("kind", sorted({k for k, _n, _t, _r in SAMPLES} | {"md_ja", "compact"}))
def test_total_error_within_ten_percent_per_kind(kind: str) -> None:
    samples = BY_KIND.get(kind, [])
    if not _covered(kind):
        pytest.skip(
            f"{kind}: 記録と一致する標本が少ない（書き換わった）。calibrate.py で result.json を更新する"
        )
    ref, est, agg, mean_abs = _stats(samples, EST.count)
    assert abs(agg) <= LIMIT, f"{kind}: 推定 {est} / 基準 {ref} = {agg:+.1%}"
    assert mean_abs <= LIMIT, f"{kind}: ファイル単位の平均絶対誤差 {mean_abs:.1%}"


def test_never_far_below_reference_on_big_samples() -> None:
    """過小は実際の応答が予算を超える向きで危ない。標本全体の合計で、基準を 5% 以上は下回らない。"""
    ref, est, agg, _ = _stats(SAMPLES, EST.count)
    assert agg >= -0.05, f"全体 {est} / {ref} = {agg:+.1%}"


def test_compact_summed_per_line_like_the_packer() -> None:
    """パッカーはレコード（行）ごとに数えて足す。切り上げが 1 行ごとに乗っても ±10% に収まる。"""
    compact = BY_KIND.get("compact", [])
    if not compact:
        pytest.skip("compact の標本が記録と一致しない")
    ref = sum(s[3] for s in compact)
    est = sum(EST.count(line) for s in compact for line in s[2].splitlines())
    assert abs(est / ref - 1) <= LIMIT, f"{est} / {ref} = {est / ref - 1:+.1%}"


# --- 陽性対照: 旧式（文字種 4 分類の係数）はこの検査で落ちる -------------------------------------------------

_OLD_WORD = re.compile(r"[0-9A-Za-z \t\n\r\f\v]+")
_OLD_SYMBOL = re.compile(r"[!-/:-@\[-`{-~]+")
_OLD_WIDE = re.compile(
    "[\u3000-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef\U00020000-\U0002fa1f]+"
)


def _old_count(text: str) -> int:
    """S-12 以前の式: ASCII 英数字と空白 1/4、ASCII 記号 1/2、全角 2/3、その他 1/2 の切り上げ。"""
    word = sum(map(len, _OLD_WORD.findall(text)))
    symbol = sum(map(len, _OLD_SYMBOL.findall(text)))
    wide = 0 if text.isascii() else sum(map(len, _OLD_WIDE.findall(text)))
    other = len(text) - word - symbol - wide
    return -(-(word * 6 + symbol * 12 + wide * 16 + other * 12) // 24)


def test_positive_control_old_coefficients_fail_this_check() -> None:
    failing = []
    for kind, samples in BY_KIND.items():
        if not _covered(kind):
            continue
        _ref, _est, agg, mean_abs = _stats(samples, _old_count)
        if abs(agg) > LIMIT or mean_abs > LIMIT:
            failing.append(kind)
    assert failing, "旧式が通ってしまう。この回帰テストに検出力が無い"
