"""S-12 トークン推定器の較正（TokenEstimator と o200k_base の比較、係数の改善案、速度）。
使い方: uv run --no-project --with tiktoken --with numpy --with scipy python bench/tokens/calibrate.py
出力: bench/tokens/result.json と coefficients_proposal.json（budget.py と pyproject は変えない。tiktoken はベンチ専用）
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import time
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

import numpy as np
import tiktoken
from scipy.optimize import nnls

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
try:  # 推定器は core/tokens.py に移された（budget.py は再公開）。どちらでも動くようにする
    from lucianke.core.tokens import TOKENS_PER_CHAR, TokenEstimator  # noqa: E402
except ImportError:
    from lucianke.mcp.budget import TOKENS_PER_CHAR, TokenEstimator  # noqa: E402

ENC = tiktoken.get_encoding("o200k_base")
MIN_REF = 50  # これ未満のファイルは平均・最大の誤差から外す（切り上げと端数で相対誤差が暴れる）
CHUNK_CHARS = 600  # 当てはめの標本の大きさ（パッカーが数えるレコード程度）
LIMIT = 0.10
#: 1 ファイルしかない種類は分割できないので、常に当てはめに入れる（交差検証の外。標本内の誤差として読むこと）
SINGLE_FILE_KINDS = {"poly_ipynb", "syn_base64", "syn_hex"}
SINGLE_FILE_FOLD = -1


def ref_tokens(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


# --- コーパス ----------------------------------------------------------------------------------


def read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", errors="replace")


def poly_kind(p: Path) -> str:
    n, s = p.name, p.suffix
    if s in {".py", ".ts", ".cob", ".sql", ".proto"}:
        return "poly_code"
    if s == ".ipynb":
        return "poly_ipynb"
    if s in {".json", ".yaml", ".toml", ".xml", ".custom"} or n == ".env":
        return "poly_data"
    return "poly_doc"  # .md .rst .mmd .dot .j2


def load_corpus() -> list[tuple[str, str, str]]:
    """(種類, 名前, 本文) の列。"""
    out: list[tuple[str, str, str]] = []
    rel = lambda p: p.relative_to(ROOT).as_posix()  # noqa: E731
    for p in sorted((ROOT / "src").rglob("*.py")):
        out.append(("py_src", rel(p), read(p)))
    for p in sorted((ROOT / "tests").rglob("*.py")):
        if "fixtures/poly" not in rel(p):
            out.append(("py_tests", rel(p), read(p)))
    for p in sorted((ROOT / "docs").glob("*.md")):
        out.append(("md_ja", rel(p), read(p)))
    for p in sorted((ROOT / "docs" / "spikes").glob("*.md")):
        if p.name != "S-12.md":  # 自分の成果物は入れない（再現のたびに結果が変わるため）
            out.append(("md_spikes", rel(p), read(p)))
    for p in sorted((ROOT / "tests" / "fixtures" / "poly").rglob("*")):
        if p.is_file() and p.stat().st_size > 0:
            out.append((poly_kind(p), rel(p), read(p)))
    spec = read(ROOT / "docs" / "Lucianke仕様書.md")
    for i, m in enumerate(re.finditer(r"^```([^\n]*)\n(.*?)^```", spec, re.S | re.M)):
        lang, body = m.group(1), m.group(2)
        if lang == "" and re.match(r"# \w+ (ok|partial|error)", body):
            out.append(("compact", f"spec#block{i}:{body.splitlines()[0][:40]}", body))
        elif lang in {"json", "sql"}:
            out.append(("spec_json_sql", f"spec#block{i}:{lang}", body))
    import base64
    import random

    rnd = random.Random(12345)  # 固定乱数（再現のため）
    b64 = base64.b64encode(rnd.randbytes(6000)).decode()
    out.append(("syn_base64", "synthetic:base64 8000 chars", "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))))
    hexes = "\n".join(rnd.randbytes(32).hex() for _ in range(60))  # SHA-256 風の 64 桁 hex を 60 行
    out.append(("syn_hex", "synthetic:sha256 hex x60", hexes))
    return out


# --- 素性（文字種と連の数） --------------------------------------------------------------------

_P = {
    "letters": re.compile(r"[A-Za-z]+"),
    "digits": re.compile(r"[0-9]+"),
    "space": re.compile(r"[ \t]+"),
    "nl": re.compile(r"[\r\n]+"),
    "symbol": re.compile(r"[!-/:-@\[-`{-~]+"),
    "kana": re.compile("[\u3040-\u30ff\u31f0-\u31ff]+"),  # ひらがな・カタカナ・カタカナ拡張
    "kanji": re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]+"),  # CJK 統合漢字・互換漢字
    "cjkpunct": re.compile("[\u3000-\u303f\uff00-\uffef]+"),  # CJK の記号・句読点、半角・全角形
}
_BLOB = re.compile(r"[A-Za-z0-9+/=]{40,}")  # base64 などの長い塊（画像の埋め込みなど）。他の文字種から切り離して数える
FEATURES = [
    "letters_chars", "letters_runs", "digits_chars", "digits_runs", "space_chars", "space_runs", "space_runs_multi",
    "nl_chars", "nl_runs", "symbol_chars", "symbol_runs", "kana", "kanji", "cjkpunct", "other", "blob_chars",
]


def features(text: str) -> dict[str, int]:
    f: dict[str, int] = {}
    total_known = 0
    blob = sum(map(len, _BLOB.findall(text)))
    f["blob_chars"] = blob
    if blob:
        text = _BLOB.sub("", text)
    for name, rx in _P.items():
        runs = rx.findall(text)
        chars = sum(map(len, runs))
        total_known += chars
        if name in {"kana", "kanji", "cjkpunct"}:
            f[name] = chars
        else:
            f[f"{name}_chars"] = chars
            f[f"{name}_runs"] = len(runs)
            if name == "space":
                f["space_runs_multi"] = sum(1 for r in runs if len(r) >= 2)
    f["other"] = len(text) - total_known
    return f


def vec(f: dict[str, int], cols: list[str]) -> np.ndarray:
    return np.array([f[c] for c in cols], dtype=float)


# --- 候補の推定式 ------------------------------------------------------------------------------

#: 係数を当てはめる素性の組。budget.py と同じ 4 分割（再当てはめ）から、文字種を細かく分ける順に並べる
CANDIDATES: dict[str, list[str]] = {  # 単純な順。最初に交差検証を通ったものを提案にする
    "base4_refit": ["@word", "symbol_chars", "@wide", "other"],
    "lean_wide": ["letters_runs", "digits_chars", "space_runs_multi", "nl_chars", "symbol_runs", "@wide", "other",
                  "blob_chars"],  # 仮名・漢字・全角記号を 1 つの係数にまとめる（全角記号の係数が不自然に大きくなるのを避ける）
    "lean_min": ["letters_runs", "digits_chars", "space_runs_multi", "nl_chars", "symbol_runs", "kana", "kanji",
                 "cjkpunct", "other", "blob_chars"],
    "split_chars": ["letters_chars", "digits_chars", "space_chars", "nl_chars", "symbol_chars", "kana", "kanji",
                    "cjkpunct", "other", "blob_chars"],
    "runs_lean": ["letters_runs", "letters_chars", "digits_chars", "space_runs_multi", "nl_chars", "symbol_runs",
                  "symbol_chars", "kana", "kanji", "cjkpunct", "other", "blob_chars"],
    "runs": ["letters_runs", "letters_chars", "digits_runs", "digits_chars", "space_runs_multi", "nl_chars",
             "symbol_runs", "symbol_chars", "kana", "kanji", "cjkpunct", "other", "blob_chars"],
}


def expand(f: dict[str, int]) -> dict[str, float]:
    g: dict[str, float] = dict(f)
    g["@word"] = f["letters_chars"] + f["digits_chars"] + f["space_chars"] + f["nl_chars"] + f["blob_chars"]
    g["@wide"] = f["kana"] + f["kanji"] + f["cjkpunct"]
    return g


def est_from(f: dict[str, float], cols: list[str], coef: list[Fraction]) -> int:
    units = sum(coef_i * f[c] for c, coef_i in zip(cols, coef, strict=True))
    return -((-units.numerator) // units.denominator) if isinstance(units, Fraction) else int(-(-units // 1))


def chunks(text: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    n = 0
    for line in text.splitlines(keepends=True):
        cur.append(line)
        n += len(line)
        if n >= CHUNK_CHARS:
            out.append("".join(cur))
            cur, n = [], 0
    if cur:
        if out and n < CHUNK_CHARS // 3:
            out[-1] += "".join(cur)
        else:
            out.append("".join(cur))
    return out or [text]


# --- 集計 -----------------------------------------------------------------------------------


def summarize(rows: list[dict]) -> dict:
    """rows: {name, ref, est}。種類の集計（合計の誤差、ファイルごとの平均・最大の絶対誤差）。"""
    ref = sum(r["ref"] for r in rows)
    est = sum(r["est"] for r in rows)
    big = [r for r in rows if r["ref"] >= MIN_REF]
    errs = [abs(r["est"] / r["ref"] - 1) for r in big]
    worst = max(big, key=lambda r: abs(r["est"] / r["ref"] - 1)) if big else None
    return {
        "files": len(rows),
        "files_ref_ge_min": len(big),
        "ref_tokens": ref,
        "est_tokens": est,
        "agg_err": est / ref - 1 if ref else None,
        "mean_abs_err": sum(errs) / len(errs) if errs else None,
        "max_abs_err": max(errs) if errs else None,
        "worst": {"name": worst["name"], "ref": worst["ref"], "est": worst["est"]} if worst else None,
    }


def by_kind(rows: list[dict]) -> dict[str, dict]:
    g: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        g[r["kind"]].append(r)
    res = {k: summarize(v) for k, v in g.items()}
    res["ALL"] = summarize(rows)
    return res


def passes(s: dict) -> bool:
    """合格: すべての種類で、合計の誤差と、ファイルごとの平均絶対誤差の両方が ±10% 以内。
    最大誤差（小さなファイルが暴れる）は参考値で、合否には使わない。"""
    return all(
        v["agg_err"] is not None and abs(v["agg_err"]) <= LIMIT and (v["mean_abs_err"] or 0) <= LIMIT
        for k, v in s.items()
        if k != "ALL"
    )


def fmt_pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:+.1f}%"


def print_table(title: str, s: dict) -> None:
    print(f"\n## {title}")
    print(f"{'kind':14s} {'n':>4s} {'ref':>8s} {'est':>8s} {'agg':>8s} {'mean|e|':>8s} {'max|e|':>8s}  worst")
    for k, v in s.items():
        w = v["worst"]["name"][-34:] if v["worst"] else ""
        print(f"{k:14s} {v['files']:4d} {v['ref_tokens']:8d} {v['est_tokens']:8d} {fmt_pct(v['agg_err']):>8s} "
              f"{fmt_pct(v['mean_abs_err']):>8s} {fmt_pct(v['max_abs_err']):>8s}  {w}")


# --- 当てはめ ---------------------------------------------------------------------------------


def fit(samples: list[dict], cols: list[str]) -> np.ndarray:
    """相対誤差の二乗和を最小化する非負の係数。種類ごとの重みの合計をそろえる。"""
    cnt: dict[str, int] = defaultdict(int)
    for s in samples:
        cnt[s["kind"]] += 1
    A = np.array([[s["f"][c] for c in cols] for s in samples], dtype=float)
    y = np.array([s["ref"] for s in samples], dtype=float)
    w = np.array([1.0 / np.sqrt(cnt[s["kind"]]) for s in samples])
    A = A / y[:, None] * w[:, None]
    coef, _ = nnls(A, w)
    return coef


def rationalize(coef: np.ndarray, den: int = 100) -> list[Fraction]:
    return [Fraction(int(round(c * den)), den) for c in coef]


# --- 速度 -------------------------------------------------------------------------------------


def make_blob(texts: list[str], size: int) -> str:
    parts: list[str] = []
    n = 0
    while n < size:
        for t in texts:
            parts.append(t)
            n += len(t)
            if n >= size:
                break
    return "".join(parts)[:size]


def bench_speed(fn, text: str, repeat: int = 7) -> float:
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn(text)
        best = min(best, time.perf_counter() - t0)
    return best * 1000


def proposal_estimator(cols: list[str], coef: list[Fraction]):
    """提案の式をそのまま実装した推定器（budget.py 担当が移す形。速度の測定にも使う）。"""
    den = 1
    for c in coef:
        den = max(den, c.denominator)
    w = {c: int(k * den) for c, k in zip(cols, coef, strict=True)}
    rx = {k: v for k, v in _P.items()}
    need = set(cols)

    def count(text: str) -> int:
        if not text:
            return 0
        units = 0
        known = 0
        blob = sum(map(len, _BLOB.findall(text)))
        if blob:
            units += w.get("blob_chars", 0) * blob
            text = _BLOB.sub("", text)
        for name, pat in rx.items():
            runs = pat.findall(text)
            chars = sum(map(len, runs))
            known += chars
            if name in {"kana", "kanji", "cjkpunct"}:
                units += w.get(name, w.get("@wide", 0)) * chars
            else:
                units += w.get(f"{name}_chars", 0) * chars + w.get(f"{name}_runs", 0) * len(runs)
                if name == "space" and "space_runs_multi" in need:
                    units += w["space_runs_multi"] * sum(1 for r in runs if len(r) >= 2)
        units += w.get("other", 0) * (len(text) - known)
        return -(-units // den)

    return count


_OTHER = re.compile("[^\t\r\n -~\u3000-\u303f\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef\U00020000-\U0002fa1f]")
_NONASCII_CLASSES = {"kana": _P["kana"], "kanji": _P["kanji"], "cjkpunct": _P["cjkpunct"]}


def fast_estimator(cols: list[str], coef: list[Fraction]):
    """提案の式を速く動かす実装（budget.py 担当が移す形）。必要な素性だけを数え、ASCII だけの文字列は非 ASCII の走査を省く。
    ``other`` は「ASCII 印字可能文字・タブ・改行・上記の CJK 類のどれでもない文字」の個数（残りの文字数ではなく直接数える）。"""
    den = 1
    for c in coef:
        den = max(den, c.denominator)
    w = {c: int(k * den) for c, k in zip(cols, coef, strict=True)}
    wl, wd, wsm, wnl, wsy = (w.get(k, 0) for k in ("letters_runs", "digits_chars", "space_runs_multi", "nl_chars",
                                                   "symbol_runs"))
    wo, wb = w.get("other", 0), w.get("blob_chars", 0)
    wnon = {k: w.get(k, w.get("@wide", 0)) for k in _NONASCII_CLASSES}
    rl, rd, rsm, rsy = _P["letters"], _P["digits"], re.compile("[ \t]{2,}"), _P["symbol"]
    rb, ro = _BLOB, _OTHER

    def count(text: str) -> int:
        if not text:
            return 0
        units = 0
        if wb and rb.search(text):
            units += wb * sum(map(len, rb.findall(text)))
            text = rb.sub("", text)
        units += wl * len(rl.findall(text)) + wd * sum(map(len, rd.findall(text))) + wsm * len(rsm.findall(text))
        units += wnl * (text.count("\n") + text.count("\r")) + wsy * len(rsy.findall(text))
        if not text.isascii():
            for k, rx in _NONASCII_CLASSES.items():
                units += wnon[k] * sum(map(len, rx.findall(text)))
        units += wo * len(ro.findall(text))
        return -(-units // den)

    return count


TERM_DEFS: dict[str, dict[str, str]] = {  # 提案の各項の定義（計数の単位と正規表現）
    "letters_runs": {"count": "matches", "regex": _P["letters"].pattern},
    "letters_chars": {"count": "chars", "regex": _P["letters"].pattern},
    "digits_chars": {"count": "chars", "regex": _P["digits"].pattern},
    "digits_runs": {"count": "matches", "regex": _P["digits"].pattern},
    "space_chars": {"count": "chars", "regex": _P["space"].pattern},
    "space_runs_multi": {"count": "matches", "regex": "[ 	]{2,}"},
    "nl_chars": {"count": "chars", "regex": _P["nl"].pattern},
    "symbol_runs": {"count": "matches", "regex": _P["symbol"].pattern},
    "symbol_chars": {"count": "chars", "regex": _P["symbol"].pattern},
    "kana": {"count": "chars", "regex": _P["kana"].pattern},
    "kanji": {"count": "chars", "regex": _P["kanji"].pattern},
    "cjkpunct": {"count": "chars", "regex": _P["cjkpunct"].pattern},
    "@wide": {"count": "chars", "regex": "kana + kanji + cjkpunct の 3 つの正規表現のどれか"},
    "other": {"count": "chars", "regex": _OTHER.pattern},
    "blob_chars": {"count": "chars", "regex": _BLOB.pattern},
}


def main() -> None:
    est = TokenEstimator()
    corpus = load_corpus()
    corpus_fp = hashlib.sha1("".join(f"{k}|{n}|{len(t)}" for k, n, t in corpus).encode()).hexdigest()[:12]
    print(f"corpus: {len(corpus)} files, {sum(len(t) for _, _, t in corpus)} chars, fp={corpus_fp}")

    # 1. 現行の TokenEstimator と o200k_base の比（ファイル・ブロックごと）
    files = []
    for kind, name, text in corpus:
        r = ref_tokens(text)
        if r == 0:
            continue
        f = expand(features(text))
        files.append({"kind": kind, "name": name, "ref": r, "est": est.count(text), "f": f, "chars": len(text)})
    base_stats = by_kind(files)
    print_table("現行 TokenEstimator（ファイル単位）", base_stats)

    # 1b. コンパクト出力はレコード（行）ごとに数えて足す（パッカーの数え方）。切り上げの偏りを見る
    perline = []
    for kind, name, text in corpus:
        if kind == "compact":
            perline.append({"kind": kind, "name": name, "ref": ref_tokens(text),
                            "est": sum(est.count(ln) for ln in text.splitlines())})
    perline_stats = summarize(perline)
    print(f"\n## compact を 1 行ずつ数えて足した場合: agg={fmt_pct(perline_stats['agg_err'])} "
          f"mean|e|={fmt_pct(perline_stats['mean_abs_err'])} max|e|={fmt_pct(perline_stats['max_abs_err'])}")

    # 2. 当てはめ（チャンク標本）。ファイル単位の交差検証（偶数番／奇数番のファイル）
    samples = []
    idx_in_kind: dict[str, int] = defaultdict(int)
    for kind, _name, text in corpus:
        fold = SINGLE_FILE_FOLD if kind in SINGLE_FILE_KINDS else idx_in_kind[kind] % 2
        idx_in_kind[kind] += 1
        pieces = [text] if kind in {"compact", "spec_json_sql"} else chunks(text)
        for piece in pieces:
            r = ref_tokens(piece)
            if r >= 10:
                samples.append({"kind": kind, "fold": fold, "ref": r, "f": expand(features(piece))})
    print(f"\nfit samples: {len(samples)}")

    candidates_out: dict[str, dict] = {}
    for cname, cols in CANDIDATES.items():
        # 交差検証
        cv_rows = []
        for hold in (0, 1):
            tr = [s for s in samples if s["fold"] != hold]
            coef = rationalize(fit(tr, cols))
            idx = defaultdict(int)
            for kind, name, _text in corpus:
                fold = SINGLE_FILE_FOLD if kind in SINGLE_FILE_KINDS else idx[kind] % 2
                idx[kind] += 1
                if fold != hold and not (fold == SINGLE_FILE_FOLD and hold == 0):
                    continue
                fr = next((x for x in files if x["name"] == name), None)
                if fr:
                    cv_rows.append({"kind": kind, "name": name, "ref": fr["ref"], "est": est_from(fr["f"], cols, coef)})
        cv = by_kind(cv_rows)
        # 全体で当てはめ
        coef = rationalize(fit(samples, cols))
        rows = [{"kind": x["kind"], "name": x["name"], "ref": x["ref"], "est": est_from(x["f"], cols, coef)}
                for x in files]
        st = by_kind(rows)
        candidates_out[cname] = {
            "cols": cols, "coef": {c: str(k) for c, k in zip(cols, coef, strict=True)}, "stats": st, "cv": cv,
            "pass_fit": passes(st), "pass_cv": passes(cv),
        }
        print_table(f"候補 {cname}（全体で当てはめ）pass={passes(st)}", st)
        print_table(f"候補 {cname}（交差検証の保持側）pass={passes(cv)}", cv)
        print("  coef:", {c: str(k) for c, k in zip(cols, coef, strict=True)})

    # 3. 速度（1 MB）
    ascii_blob = make_blob([t for k, _, t in corpus if k == "py_src"], 1_000_000)
    ja_blob = make_blob([t for k, _, t in corpus if k == "md_ja"], 1_000_000)
    compact_blob = make_blob([t for k, _, t in corpus if k == "compact"], 1_000_000)
    speed = {}
    for label, blob in (("py_src_1MB", ascii_blob), ("md_ja_1MB", ja_blob), ("compact_1MB", compact_blob)):
        speed[label] = {"current_ms": round(bench_speed(est.count, blob), 2), "chars": len(blob)}
    # 提案式の速度は、合格した最小の候補で測る
    chosen = next((n for n in CANDIDATES if candidates_out[n]["pass_cv"]), None)
    if chosen is None:  # どれも通らなければ、種類の合計誤差の最大が最小のもの
        chosen = min(candidates_out, key=lambda n: max(abs(v["agg_err"]) for k, v in candidates_out[n]["cv"].items()
                                                     if k != "ALL"))
    cols = CANDIDATES[chosen]
    coef = [Fraction(candidates_out[chosen]["coef"][c]) for c in cols]
    pe = proposal_estimator(cols, coef)
    fast = fast_estimator(cols, coef) if chosen in {"lean_min", "lean_wide"} else pe
    for label, blob in (("py_src_1MB", ascii_blob), ("md_ja_1MB", ja_blob), ("compact_1MB", compact_blob)):
        speed[label]["proposal_generic_ms"] = round(bench_speed(pe, blob), 2)
        speed[label]["proposal_ms"] = round(bench_speed(fast, blob), 2)
        speed[label]["tiktoken_ms"] = round(bench_speed(ref_tokens, blob, repeat=2), 1)
    # 提案式の実装が素性の式と一致するか
    mism = sum(1 for kind, name, text in corpus if text and pe(text) != est_from(expand(features(text)), cols, coef))
    mism_fast = sum(1 for kind, name, text in corpus if text and fast(text) != est_from(expand(features(text)), cols, coef))
    print("fast 実装と式の不一致:", mism_fast)
    print("\n## 速度(ms/1MB)", json.dumps(speed, ensure_ascii=False), "| 実装と式の不一致:", mism)

    result = {
        "spike": "S-12",
        "date": time.strftime("%Y-%m-%d"),
        "env": {"python": platform.python_version(), "platform": platform.platform(),
                "tiktoken": tiktoken.__version__ if hasattr(tiktoken, "__version__") else "?",
                "numpy": np.__version__},
        "encoding": "o200k_base",
        "corpus": {"files": len(corpus), "chars": sum(len(t) for _, _, t in corpus), "fingerprint": corpus_fp,
                   "note": "src/ と tests/ は他の役が並行して書き換えるため、実行のたびに少し変わる"},
        "limit": LIMIT, "min_ref_tokens_for_file_stats": MIN_REF, "chunk_chars": CHUNK_CHARS,
        "current_coefficients_tokens_per_char": {k: str(v) for k, v in TOKENS_PER_CHAR.items()},
        "current": {"by_kind": base_stats, "compact_per_line_sum": perline_stats,
                    "files": [{k: x[k] for k in ("kind", "name", "ref", "est", "chars")} for x in files]},
        "candidates": candidates_out,
        "chosen_for_proposal": chosen,
        "speed_1MB": speed,
        "impl_mismatch": mism, "fast_impl_mismatch": mism_fast,
    }
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    proposal = {
        "spike": "S-12",
        "status": "proposal (budget.py は未変更)",
        "basis": "o200k_base",
        "model": chosen,
        "rounding": "ceil（整数演算: units = sum(係数 × 数) / DEN の切り上げ）",
        "den": 100,
        "terms": [
            {"name": c, "tokens_per_unit": str(k), **TERM_DEFS[c]}
            for c, k in zip(cols, coef, strict=True)
        ],
        "order": "blob_chars を先に数えて本文から取り除き、残りの本文に他の項を当てる。ASCII だけの文字列は kana/kanji/cjkpunct を省いてよい",
        "expected_error_cv": candidates_out[chosen]["cv"],
        "expected_error_fit": candidates_out[chosen]["stats"],
    }
    (OUT / "coefficients_proposal.json").write_text(json.dumps(proposal, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
    print("\nwrote result.json, coefficients_proposal.json; chosen =", chosen)


if __name__ == "__main__":
    main()
