"""トークン推定器の較正の回帰テスト（S-12、仕様書 §5.7: ``o200k_base`` 比 ±10%）。

標本は ``tests/fixtures/tokens/`` に凍結した本文で、``bench/tokens/freeze_corpus.py`` が ``calibrate.py`` の ``load_corpus``
（``src/`` ・``tests/`` ・``docs/`` ・poly フィクスチャ・仕様書の例・合成の base64 と hex）から種類ごとに切り出して写したもの。
基準値（``o200k_base`` のトークン数）は同じ所の ``manifest.json`` に本文の SHA-256 と並べて記録してあり、ネットワークも
tiktoken も使わない。リポジトリの生のファイルは読まないので、開発中の書き換えで標本が外れて検証が空洞化しない（BUG-0068:
以前は生のファイルを記録の文字数と照合し、外れた種類を skip していた）。凍結した本文が記録と食い違うときや、種類が欠けた
ときは skip せずに失敗する。推定式（``core/tokens.py``）を変えても基準値は変わらないので凍結し直さない。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

from lucianke.core.tokens import TokenEstimator

ROOT = Path(__file__).resolve().parents[2]
FROZEN = ROOT / "tests" / "fixtures" / "tokens"
MANIFEST = FROZEN / "manifest.json"
REFREEZE = (
    "標本を作り直すなら `uv run --no-project --with tiktoken --with numpy --with scipy python "
    "bench/tokens/freeze_corpus.py` を実行して tests/fixtures/tokens/ ごとコミットする"
)
LIMIT = 0.10  # 仕様書 §5.7
MIN_REF = 50  # 標本単位の平均誤差から外す小さい標本（切り上げで相対誤差が暴れる）。calibrate.py と同じ
MIN_TOTAL_TOKENS = 50_000  # 標本全体の最小の基準トークン数。これ未満は標本が空洞化している
#: 必ず検証する種類（S-12 のコーパスの全種類）。manifest から欠けたら失敗にする（黙って検証が減らないように）
REQUIRED_KINDS = frozenset(
    {
        "compact",
        "md_ja",
        "md_spikes",
        "poly_code",
        "poly_data",
        "poly_doc",
        "poly_ipynb",
        "py_src",
        "py_tests",
        "spec_json_sql",
        "syn_base64",
        "syn_hex",
    }
)

EST = TokenEstimator()

type Sample = tuple[str, str, str, int]  # (種類, 名前, 本文, 基準トークン数)


def _load() -> tuple[list[Sample], dict[str, list[str]]]:
    """凍結した標本と、種類ごとの食い違い（ファイルが無い・SHA-256 や文字数が記録と違う）の一覧。"""
    entries = json.loads(MANIFEST.read_bytes().decode("utf-8"))["samples"]
    samples: list[Sample] = []
    problems: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        path = FROZEN / e["file"]
        if not path.is_file():
            problems[e["kind"]].append(f"{e['file']}（{e['name']}）が無い")
            continue
        data = path.read_bytes()
        text = data.decode("utf-8")
        if hashlib.sha256(data).hexdigest() != e["sha256"] or len(text) != e["chars"]:
            problems[e["kind"]].append(f"{e['file']}（{e['name']}）の本文が manifest の記録と違う")
            continue
        samples.append((e["kind"], e["name"], text, e["ref"]))
    listed = {e["file"] for e in entries}
    for p in sorted(FROZEN.rglob("*.txt")):
        rel = p.relative_to(FROZEN).as_posix()
        if rel not in listed:
            problems[rel.split("/", 1)[0]].append(f"{rel} が manifest に無い（作り直しの途中で止まった？）")
    return samples, problems


SAMPLES, PROBLEMS = _load()
BY_KIND: dict[str, list[Sample]] = defaultdict(list)
for _s in SAMPLES:
    BY_KIND[_s[0]].append(_s)


def _assert_intact(kind: str) -> list[Sample]:
    """この種類の標本が記録どおり揃っている（食い違い・欠けは skip ではなく失敗）。標本を返す。"""
    assert not PROBLEMS.get(kind), f"{kind}: 凍結した標本が manifest と食い違う: {PROBLEMS[kind]}。{REFREEZE}"
    samples = BY_KIND.get(kind, [])
    assert samples, f"{kind}: 凍結した標本が無い。{REFREEZE}"
    return samples


def _stats(samples: list[Sample], count) -> tuple[int, int, float, float]:
    """(基準合計, 推定合計, 合計誤差, 標本単位の平均絶対誤差)。誤差は ``推定 / 基準 − 1``。"""
    ref = sum(s[3] for s in samples)
    ests = [count(s[2]) for s in samples]
    est = sum(ests)
    errs = [abs(e / s[3] - 1) for s, e in zip(samples, ests, strict=True) if s[3] >= MIN_REF]
    return ref, est, est / ref - 1, (sum(errs) / len(errs) if errs else 0.0)


def test_frozen_corpus_is_intact_and_not_hollow() -> None:
    """凍結した標本が manifest の記録（SHA-256・文字数）と一致し、全種類が揃い、量も足りる。"""
    assert not PROBLEMS, f"凍結した標本が manifest と食い違う: {dict(PROBLEMS)}。{REFREEZE}"
    missing = REQUIRED_KINDS - BY_KIND.keys()
    assert not missing, f"凍結した標本に種類が欠けている: {sorted(missing)}。{REFREEZE}"
    total = sum(s[3] for s in SAMPLES)
    assert total >= MIN_TOTAL_TOKENS, f"凍結した標本が少ない（基準 {total} トークン）。{REFREEZE}"


@pytest.mark.parametrize("kind", sorted(REQUIRED_KINDS | BY_KIND.keys()))
def test_total_error_within_ten_percent_per_kind(kind: str) -> None:
    samples = _assert_intact(kind)
    ref, est, agg, mean_abs = _stats(samples, EST.count)
    assert abs(agg) <= LIMIT, f"{kind}: 推定 {est} / 基準 {ref} = {agg:+.1%}"
    assert mean_abs <= LIMIT, f"{kind}: 標本単位の平均絶対誤差 {mean_abs:.1%}"


def test_never_far_below_reference_on_big_samples() -> None:
    """過小は実際の応答が予算を超える向きで危ない。標本全体の合計で、基準を 5% 以上は下回らない。"""
    ref, est, agg, _ = _stats(SAMPLES, EST.count)
    assert agg >= -0.05, f"全体 {est} / {ref} = {agg:+.1%}"


def test_compact_summed_per_line_like_the_packer() -> None:
    """パッカーはレコード（行）ごとに数えて足す。切り上げが 1 行ごとに乗っても ±10% に収まる。"""
    compact = _assert_intact("compact")
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
        _ref, _est, agg, mean_abs = _stats(samples, _old_count)
        if abs(agg) > LIMIT or mean_abs > LIMIT:
            failing.append(kind)
    # 旧式は日本語 Markdown で −16%、base64・hex の塊で −40% 超の過小だった（S-12）。どちらも落ちなければ検出力が無い
    assert {"md_ja", "syn_base64"} <= set(failing), (
        f"旧式が通ってしまう（落ちた種類: {failing}）。検出力が無い"
    )
