"""トークン推定器の回帰テストの標本を凍結する（S-12 の較正コーパスの写し、BUG-0068）。
使い方: uv run --no-project --with tiktoken --with numpy --with scipy python bench/tokens/freeze_corpus.py
出力: tests/fixtures/tokens/manifest.json と tests/fixtures/tokens/<種類>/<番号>.txt（前回の写しは消して書き直す）

``calibrate.py`` の ``load_corpus``（``src/`` ・``tests/`` ・``docs/`` などリポジトリ内のファイル）から、種類ごとに文字数の上限まで
標本を選んで本文をそのまま写し、``o200k_base`` のトークン数（基準値）と本文の SHA-256 を manifest に記録する。
回帰テスト ``tests/unit/test_core_tokens_calibration.py`` はこの写しだけを読む（ネットワークも tiktoken も使わない）。
リポジトリのファイルは開発中に書き換わるので、生のファイルを照合すると標本がすぐ外れて検証が空洞化する（BUG-0068）。
推定式（``core/tokens.py``）を変えても基準値は変わらない。凍結し直すのは標本そのものを替えたいとき（種類を足す、など）だけ。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrate  # noqa: E402  （tiktoken・numpy・scipy を読み込む。load_corpus と ref_tokens を共用する）

ROOT = calibrate.ROOT
DEST = ROOT / "tests" / "fixtures" / "tokens"
#: 種類ごとの標本の文字数の上限。ここに無い種類（poly・compact・spec_json_sql・合成）は小さいので全部写す
BUDGET_CHARS: dict[str, int] = {"py_src": 100_000, "py_tests": 100_000, "md_ja": 80_000, "md_spikes": 40_000}
#: 上限のある種類で、これより長い本文は行の境目でこの文字数以下の断片に分けてから選ぶ（巨大な 1 ファイルが上限を占めないように）
SEG_MAX_CHARS = 8_000


def segments(name: str, text: str) -> list[tuple[str, str]]:
    """(名前, 本文) の列。``SEG_MAX_CHARS`` 以下ならそのまま、超えればほぼ等しい長さの断片に行の境目で分け
    （端に極端に短い断片を作らない）、名前に行の範囲を付ける。"""
    if len(text) <= SEG_MAX_CHARS:
        return [(name, text)]
    k = -(-len(text) // SEG_MAX_CHARS)
    target = len(text) / k
    out: list[tuple[str, str]] = []
    cur: list[str] = []
    n = 0
    start = 1
    lineno = 0
    for line in text.splitlines(keepends=True):
        lineno += 1
        cur.append(line)
        n += len(line)
        if n >= target and len(out) < k - 1:
            out.append((f"{name}#L{start}-{lineno}", "".join(cur)))
            cur, n, start = [], 0, lineno + 1
    if cur:
        out.append((f"{name}#L{start}-{lineno}", "".join(cur)))
    return out


def select(kind: str, pieces: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """名前のハッシュ順（内容に依らない擬似乱数の順）に、上限の文字数まで取る。上限の無い種類は全部。"""
    budget = BUDGET_CHARS.get(kind)
    if budget is None:
        return pieces
    order = sorted(pieces, key=lambda p: hashlib.sha1(f"{kind}|{p[0]}".encode()).hexdigest())
    chosen: list[tuple[str, str]] = []
    used = 0
    for name, text in order:
        if used + len(text) <= budget:
            chosen.append((name, text))
            used += len(text)
    return sorted(chosen)


def stats(rows: list[tuple[int, int]]) -> tuple[int, int, float, float]:
    """rows: (基準, 推定)。(基準合計, 推定合計, 合計誤差, 基準 MIN_REF 以上の標本の平均絶対誤差)。"""
    ref = sum(r for r, _ in rows)
    est = sum(e for _, e in rows)
    errs = [abs(e / r - 1) for r, e in rows if r >= calibrate.MIN_REF]
    return ref, est, est / ref - 1, (sum(errs) / len(errs) if errs else 0.0)


def git_head() -> str:
    """写した本文の出どころ。コーパスの範囲に未コミットの変更があれば ``+dirty`` を付ける。"""

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout

    try:
        head = git("rev-parse", "HEAD").strip()
        dirty = git("status", "--porcelain", "--", "src", "tests", "docs", ":!tests/fixtures/tokens").strip()
    except (OSError, subprocess.CalledProcessError):
        return "?"
    return head + ("+dirty" if dirty else "")


def main() -> None:
    est = calibrate.TokenEstimator()
    corpus = calibrate.load_corpus()
    by_kind: dict[str, list[tuple[str, str]]] = defaultdict(list)
    full: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for kind, name, text in corpus:
        r = calibrate.ref_tokens(text)
        if r == 0:
            continue
        full[kind].append((r, est.count(text)))
        # 上限のある種類だけ断片に分ける（ほかは全部写すので、S-12 と同じくファイル・ブロック単位のまま）
        by_kind[kind].extend(segments(name, text) if kind in BUDGET_CHARS else [(name, text)])

    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    samples: list[dict] = []
    frozen: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for kind in sorted(by_kind):
        (DEST / kind).mkdir()
        for i, (name, text) in enumerate(select(kind, by_kind[kind])):
            r = calibrate.ref_tokens(text)
            if r == 0:
                continue
            data = text.encode("utf-8")
            rel = f"{kind}/{i:03d}.txt"
            (DEST / rel).write_bytes(data)  # バイト列のまま（tests/fixtures は -text で改行を変えない）
            samples.append(
                {
                    "kind": kind,
                    "name": name,
                    "file": rel,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "chars": len(text),
                    "ref": r,
                }
            )
            frozen[kind].append((r, est.count(text)))

    manifest = {
        "about": "tests/unit/test_core_tokens_calibration.py の標本（凍結）。ref は o200k_base のトークン数。"
        "作り直しは bench/tokens/freeze_corpus.py（BUG-0068）",
        "encoding": "o200k_base",
        "tiktoken": getattr(calibrate.tiktoken, "__version__", "?"),
        "date": time.strftime("%Y-%m-%d"),
        "source_commit": git_head(),
        "budget_chars": BUDGET_CHARS,
        "segment_max_chars": SEG_MAX_CHARS,
        "samples": samples,
    }
    (DEST / "manifest.json").write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    )

    print(
        f"{'kind':14s} {'full n':>6s} {'agg':>7s} {'mean|e|':>7s} | {'frozen n':>8s} {'ref':>7s} {'agg':>7s} {'mean|e|':>7s}"
    )
    for kind in sorted(full):
        _fr, _fe, fagg, fmean = stats(full[kind])
        ref, _e, agg, mean = stats(frozen[kind])
        print(
            f"{kind:14s} {len(full[kind]):6d} {fagg:+7.1%} {fmean:7.1%} | {len(frozen[kind]):8d} {ref:7d} {agg:+7.1%} "
            f"{mean:7.1%}"
        )
    total = sum(s["ref"] for s in samples)
    size = sum(len((DEST / s["file"]).read_bytes()) for s in samples)
    print(
        f"wrote {len(samples)} samples ({total} ref tokens, {size} bytes) to {DEST.relative_to(ROOT).as_posix()}"
    )


if __name__ == "__main__":
    main()
