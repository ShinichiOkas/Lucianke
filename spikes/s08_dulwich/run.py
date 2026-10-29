"""S-08: dulwich による Git の直読（参照解決・blob・STAGED・ツリー差分・log --follow・.gitignore・status・不変性）。
使い方: uv run python spikes/s08_dulwich/run.py            # このリポジトリだけ（ネットワーク不要）
        uv run python spikes/s08_dulwich/run.py --click   # pallets/click を %TEMP% に clone して F-PY 規模も測る（終了時に削除）
一時リポジトリは %TEMP%\\lucianke_s08 に作る。読むだけで、対象の作業ツリー・index・refs は変えない（項目 8 で検証）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import dulwich
from dulwich.diff_tree import (
    CHANGE_ADD, CHANGE_COPY, CHANGE_DELETE, CHANGE_MODIFY, CHANGE_RENAME,
    RenameDetector, tree_changes,
)
from dulwich.ignore import IgnoreFilterManager
from dulwich.index import ConflictedIndexEntry
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob
from dulwich.objectspec import AmbiguousShortId, parse_commit, parse_object
from dulwich.repo import Repo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
TMP = Path(tempfile.gettempdir()) / "lucianke_s08"
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
LETTER = {CHANGE_ADD: "A", CHANGE_DELETE: "D", CHANGE_MODIFY: "M", CHANGE_RENAME: "R", CHANGE_COPY: "C"}


# ---------------------------------------------------------------- 計時・git 補助
def bench(fn, n: int = 100):
    """初回（cold）と、その後 n 回の中央値・p95・最小（ms）。"""
    t0 = time.perf_counter(); fn(); first = (time.perf_counter() - t0) * 1000
    xs = []
    for _ in range(n):
        t0 = time.perf_counter(); fn(); xs.append((time.perf_counter() - t0) * 1000)
    xs.sort()
    return {"first": first, "med": statistics.median(xs), "p95": xs[int(len(xs) * 0.95) - 1], "min": xs[0]}


def fmt(b) -> str:
    return f"初回 {b['first']:.1f} / 中央値 {b['med']:.2f} / p95 {b['p95']:.2f} ms"


def git(cwd, *args, check=True) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, env=GIT_ENV)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {args}: {p.stderr.decode(errors='replace')}")
    return p.stdout.decode("utf-8", "replace")


def rmtree_force(path: Path) -> None:
    """読み取り専用の .git/objects も消す。"""
    if Path(path).exists():
        shutil.rmtree(path, onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))


def sha256_file(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def state_digest(root: Path) -> dict[str, str | None]:
    """index・HEAD・packed-refs・refs/**・objects の数・git status の出力。
    git status は index の stat キャッシュを書き換え得るので、先に 1 回流してから index を測る（呼び出し側の順序）。"""
    g = root / ".git"
    d: dict[str, str | None] = {k: sha256_file(g / k) for k in ("index", "HEAD", "packed-refs", "config")}
    h = hashlib.sha256()
    for p in sorted((g / "refs").rglob("*")):
        if p.is_file():
            h.update(p.relative_to(g).as_posix().encode() + p.read_bytes())
    d["refs"] = h.hexdigest()[:16]
    d["objects"] = str(sum(1 for _ in (g / "objects").rglob("*") if _.is_file()))
    return d


def settle_git_index(root: Path) -> str:
    """git 自身が index を書く分を先に済ませる。戻り値は porcelain 出力の sha（作業ツリーの状態の指紋）。"""
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "-unormal"], capture_output=True)
    return hashlib.sha256(out.stdout).hexdigest()[:16]


# ---------------------------------------------------------------- 使うコードの型（docs/spikes/S-08.md と同じ）
class RevisionNotFound(Exception):
    pass


_BAD = re.compile(r"[\s:?*\[\\]|\.\.|@\{|^$")
_SPLIT = re.compile(r"^(?P<base>.+?)(?P<suffix>(?:[~^]\d*)*)$")


def resolve_revision(repo: Repo, rev: str) -> str:
    """HEAD / ブランチ / タグ / リモート追跡 / 4 桁以上の短縮 SHA / 40 桁 SHA に、~N と ^N を付けたもの → コミット SHA（40 桁）。"""
    m = _SPLIT.match(rev)
    if not m or _BAD.search(m["base"]) or "^{" in rev:
        raise RevisionNotFound(rev)
    try:
        commit = parse_commit(repo, m["base"])          # 参照・短縮 SHA・注釈付きタグの剥がしを担う
        if m["suffix"]:
            obj = parse_object(repo, commit.id + m["suffix"].encode())   # ~ と ^ は完全 SHA を基点に
            commit = parse_commit(repo, obj)
    except (KeyError, ValueError, AmbiguousShortId) as e:
        raise RevisionNotFound(rev) from e
    return commit.id.decode("ascii")


def read_blob(repo: Repo, commit_sha: str, path: str) -> bytes | None:
    commit = repo[commit_sha.encode("ascii")]
    tree = repo[commit.tree]
    try:
        mode, sha = tree.lookup_path(repo.object_store.__getitem__, path.encode("utf-8"))
    except (KeyError, Exception) as e:                    # KeyError=無い / NotTreeError=途中がファイル / SubmoduleEncountered
        if type(e).__name__ in ("KeyError", "NotTreeError", "SubmoduleEncountered"):
            return None
        raise
    obj = repo[sha]
    return obj.data if isinstance(obj, Blob) else None   # ディレクトリ(Tree)は None


def read_staged_blob(repo: Repo, path: str) -> bytes | None:
    index = repo.open_index()                              # 読むだけ。ロックも書き込みもしない
    try:
        entry = index[path.encode("utf-8")]
    except KeyError:
        return None
    if isinstance(entry, ConflictedIndexEntry):            # 競合中は stage 2（this）を採る
        entry = entry.this or entry.ancestor or entry.other
        if entry is None:
            return None
    return repo[entry.sha].data


def changed_files(repo: Repo, a_sha: str, b_sha: str, *, threshold: int = 50):
    """(種別 A/M/D/R/C, 旧パス, 新パス) の列。改名は内容の類似度 threshold %以上。"""
    store = repo.object_store
    ta, tb = repo[a_sha.encode()].tree, repo[b_sha.encode()].tree
    rd = RenameDetector(store, rename_threshold=threshold)
    out = []
    for c in tree_changes(store, ta, tb, rename_detector=rd):
        out.append((LETTER[c.type], c.old.path.decode() if c.old and c.old.path else None,
                    c.new.path.decode() if c.new and c.new.path else None))
    return out


def file_log(repo: Repo, head_sha: str, path: str, *, max_entries: int | None = None, threshold: int = 50):
    """git log --follow 相当。(コミット SHA, 種別 A/M/D/R/C, その時点のパス, 改名前のパス) を新しい順に。"""
    rd = RenameDetector(repo.object_store, rename_threshold=threshold)
    w = repo.get_walker(include=[head_sha.encode()], paths=[path.encode()], follow=True,
                        rename_detector=rd, max_entries=max_entries)
    cur = path.encode()
    out = []
    for e in w:
        flat = []
        for c in e.changes():                 # マージコミットは親ごとの list の list
            flat.extend(x for x in (c if isinstance(c, list) else [c]) if x is not None)
        hit = next((c for c in flat if (c.new and c.new.path == cur) or (c.old and c.old.path == cur)), None)
        if hit is None:
            out.append((e.commit.id.decode(), "?", cur.decode(), None)); continue
        old = hit.old.path if hit.old else None
        out.append((e.commit.id.decode(), LETTER[hit.type], cur.decode(), old.decode() if hit.type in (CHANGE_RENAME, CHANGE_COPY) else None))
        if hit.type in (CHANGE_RENAME, CHANGE_COPY):
            cur = old
    return out


def file_log_fast(repo: Repo, head_sha: str, path: str, *, max_commits: int | None = None, threshold: int = 50):
    """git log --follow 相当（自前）。コミットごとに「そのパスの (mode, sha)」だけを親と比べる（全ツリー差分はしない）。
    マージで片方の親と同一ならその親だけ辿る（git の既定の履歴簡約）。パスが親に無いときだけ改名検出を走らせる。
    max_commits は「調べたコミット数」の上限。戻りは (コミット SHA, 種別 A/M/D/R/C, その時点のパス, 改名前のパス)。"""
    import heapq
    store = repo.object_store
    rd = RenameDetector(store, rename_threshold=threshold)
    memo: dict[tuple[bytes, bytes], tuple[int, bytes] | None] = {}

    def entry(tree_id: bytes, pth: bytes):
        k = (tree_id, pth)
        if k not in memo:
            try:
                memo[k] = store[tree_id].lookup_path(store.__getitem__, pth)
            except Exception:   # KeyError / NotTreeError / SubmoduleEncountered -> 無い扱い
                memo[k] = None
        return memo[k]

    seq = 0
    heap = []
    c0 = repo[head_sha.encode()]
    heapq.heappush(heap, (-c0.commit_time, seq, c0.id, path.encode()))
    seen: set[tuple[bytes, bytes]] = set()
    out, examined = [], 0
    while heap and (max_commits is None or examined < max_commits):
        _, _, cid, cur = heapq.heappop(heap)
        if (cid, cur) in seen:
            continue
        seen.add((cid, cur))
        examined += 1
        c = repo[cid]
        mine = entry(c.tree, cur)
        parents = c.parents
        same_parent = None
        kind, old = None, None
        if not parents:
            kind = "A" if mine else None
        else:
            theirs = [(pid, entry(repo[pid].tree, cur)) for pid in parents]
            same_parent = next((pid for pid, e in theirs if e == mine), None)
            if same_parent is None:
                if mine is None:
                    kind = "D"
                elif all(e is None for _, e in theirs):
                    kind = "A"
                    if len(parents) == 1:      # 追加に見えるときだけ改名検出（親->子の全差分は、ここでしか取らない）
                        for ch in rd.changes_with_renames(repo[parents[0]].tree, c.tree):
                            if ch.type in (CHANGE_RENAME, CHANGE_COPY) and ch.new.path == cur:
                                kind, old = ("R" if ch.type == CHANGE_RENAME else "C"), ch.old.path
                                break
                else:
                    kind = "M"
        if kind:
            out.append((cid.decode(), kind, cur.decode(), old.decode() if old else None))
        nxt = [same_parent] if same_parent is not None and len(parents) > 1 else list(parents)
        for pid in nxt:
            seq += 1
            heapq.heappush(heap, (-repo[pid].commit_time, seq, pid, old if (old and kind in ("R", "C")) else cur))
    return out


# ---------------------------------------------------------------- 項目 1: リビジョン解決
def item1(repo: Repo, root: Path, label: str, specs: list[str]):
    print(f"\n## 1 リビジョン解決 [{label}]")
    ok = True
    for spec in specs:
        want = git(root, "rev-parse", "--verify", "--quiet", spec + "^{commit}", check=False).strip() or None
        if any(u in spec for u in ("@{", "..", ":", "^{")):
            want = None   # 設計書 §7.9.1: 対応外の構文（git は解せる）は「見つからない」が正解
        try:
            got = resolve_revision(repo, spec)
            err = ""
        except RevisionNotFound as e:
            got, err = None, f"RevisionNotFound(cause={type(e.__cause__).__name__ if e.__cause__ else '構文'})"
        status = "OK" if got == want else "NG"
        ok &= got == want
        print(f"  {status} {spec!r:28} -> {(got or '-')[:12]:12} (git: {(want or '-')[:12]:12}) {err}")
    return ok


def raw_exceptions(repo: Repo, specs: list[str]):
    print("  素の例外型（parse_commit / parse_object）:")
    for spec in specs:
        r = []
        for fn in (parse_commit, parse_object):
            try:
                o = fn(repo, spec); r.append(f"{fn.__name__}=OK({type(o).__name__})")
            except BaseException as e:   # noqa: BLE001
                r.append(f"{fn.__name__}={type(e).__name__}")
        print(f"    {spec!r:26} " + "  ".join(r))


# ---------------------------------------------------------------- 一時リポジトリ
def run_git_env(cwd, i, *args):
    ts = 1_700_000_000 + i * 3600
    env = {**GIT_ENV, "GIT_AUTHOR_DATE": f"{ts} +0000", "GIT_COMMITTER_DATE": f"{ts} +0000",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


def make_rename_repo(dst: Path) -> Path:
    """改名・改名＋小修正・改名＋大書き換え・ディレクトリ移動を含む 10 コミットの小さな履歴。"""
    if dst.exists():
        rmtree_force(dst)
    dst.mkdir(parents=True)
    run_git_env(dst, 0, "init", "-q", "-b", "main")
    run_git_env(dst, 0, "config", "core.autocrlf", "false")
    body = "".join(f"line {i} of the module with some words to make it similar\n" for i in range(40))
    i = 0

    def commit(msg):
        nonlocal i
        i += 1
        run_git_env(dst, i, "add", "-A"); run_git_env(dst, i, "commit", "-q", "-m", msg)
    (dst / "a.py").write_text(body, newline="\n"); commit("add a.py")
    (dst / "a.py").write_text(body + "extra\n", newline="\n"); commit("modify a.py")
    run_git_env(dst, i, "mv", "a.py", "b.py"); (dst / "b.py").write_text(body.replace("line 3 ", "LINE 3 ") + "extra\n", newline="\n"); commit("rename a->b (+小修正)")
    (dst / "b.py").write_text(body + "extra\nmore\n", newline="\n"); commit("modify b.py")
    (dst / "pkg").mkdir(); run_git_env(dst, i, "mv", "b.py", "pkg/c.py"); commit("move b->pkg/c (純粋な改名)")
    (dst / "other.py").write_text("unrelated\n", newline="\n"); commit("other.py")
    (dst / "pkg/c.py").write_text(body + "extra\nmore\nagain\n", newline="\n"); commit("modify pkg/c.py")
    run_git_env(dst, i, "mv", "pkg/c.py", "pkg/d.py")
    (dst / "pkg/d.py").write_text("".join(f"completely different {i}\n" for i in range(40)) + body[:600], newline="\n"); commit("rename c->d (+大幅書き換え)")
    (dst / "pkg/d.py").write_text("".join(f"completely different {i}\n" for i in range(40)) + body[:600] + "tail\n", newline="\n"); commit("modify pkg/d.py")
    run_git_env(dst, i, "tag", "-a", "v1", "-m", "annotated tag"); run_git_env(dst, i, "tag", "light")
    return dst


def make_dirty_clone(dst: Path) -> Path:
    """このリポジトリの clone に、staged / unstaged / untracked / ignored / stat だけ変わったファイルを作る。"""
    if dst.exists():
        rmtree_force(dst)
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(dst)], check=True, capture_output=True)
    run_git_env(dst, 0, "config", "user.email", "t@example.com"); run_git_env(dst, 0, "config", "user.name", "t")
    (dst / "README.md").write_text("STAGED CONTENT\n", newline="\n"); run_git_env(dst, 0, "add", "README.md")
    (dst / "README.md").write_text("WORKTREE CONTENT\n", newline="\n")           # staged と worktree で内容が違う
    (dst / "pyproject.toml").open("a", newline="\n").write("# unstaged\n")
    (dst / "newfile.txt").write_text("new\n"); run_git_env(dst, 0, "add", "newfile.txt")   # staged add
    (dst / "untracked.txt").write_text("u\n")
    (dst / "node_modules").mkdir(); (dst / "node_modules/x.js").write_text("ignored\n")
    (dst / ".gitignore").open("a", newline="\n").write("\nnode_modules/\n")
    run_git_env(dst, 0, "tag", "light"); run_git_env(dst, 0, "tag", "-a", "ann", "-m", "ann")
    # stat だけ変えた（内容は同じ）ファイル: dulwich status が index を書き換えるか見る
    lic = dst / "LICENSE"; os.utime(lic, (time.time() - 5000, time.time() - 5000))
    return dst


# ---------------------------------------------------------------- 本体
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--click", action="store_true", help="pallets/click を clone して F-PY 規模も測る")
    ap.add_argument("--reps", type=int, default=100)
    args = ap.parse_args()
    TMP.mkdir(exist_ok=True)
    results: dict[str, bool] = {}
    from dulwich import _objects  # noqa: F401  C 拡張が入っているか
    print(f"dulwich {'.'.join(map(str, dulwich.__version__))}  python {sys.version.split()[0]}  C拡張(_objects) あり")

    # ---------- このリポジトリ（小規模・ルーズオブジェクト）
    repo = Repo(str(ROOT))
    head = resolve_revision(repo, "HEAD")
    h3 = resolve_revision(repo, "HEAD~3")
    results["1 self"] = item1(repo, ROOT, "Lucianke 自身", [
        "HEAD", "HEAD~3", "HEAD^", "HEAD~1~1", "main", "origin/main", "refs/heads/main", "heads/main", head[:7], head[:4],
        h3, "nonexistent", "HEAD~9999", "HEAD^2", "HEAD@{1}", "HEAD~1..HEAD", "HEAD:README.md", "a94", "", "HEAD^{commit}"])
    raw_exceptions(repo, ["HEAD~3", "a9477ab~1", "HEAD@{1}", "HEAD:README.md", "nonexistent", "HEAD~9999"])
    try:
        Repo(str(ROOT / "src"))
    except Exception as e:   # noqa: BLE001
        print(f"  Repo(サブディレクトリ) -> {type(e).__module__}.{type(e).__name__}（Repo.discover を使うなら {Repo.discover(str(ROOT / 'src')).path}）")
    empty = TMP / "empty"; shutil.rmtree(empty, ignore_errors=True); empty.mkdir(); run_git_env(empty, 0, "init", "-q")
    try:
        resolve_revision(Repo(str(empty)), "HEAD")
    except RevisionNotFound as e:
        print(f"  コミット無しのリポジトリで HEAD -> RevisionNotFound(cause={type(e.__cause__).__name__})")

    # ---------- 項目 2・3・4・5・6・7 の本体は関数にして Lucianke / click で同じ手順
    for label, root, files, ref_old, follow_path in [("Lucianke 自身", ROOT, ["README.md", "pyproject.toml"], "HEAD~5", "pyproject.toml")]:
        measure(label, root, files, ref_old, follow_path, args.reps, results)

    # ---------- タグ（一時リポジトリ）
    rr = make_rename_repo(TMP / "rename")
    with Repo(str(rr)) as r2:
        results["1 tags"] = item1(r2, rr, "改名リポジトリ（注釈付き v1・軽量 light）", ["v1", "light", "v1~2", "light^", "tags/v1", "refs/tags/v1", "v1^{}"])
    # 注釈付きタグが非コミットを指す場合
    run_git_env(rr, 99, "tag", "-a", "treetag", "-m", "t", "HEAD^{tree}")
    with Repo(str(rr)) as r2:
        try:
            resolve_revision(r2, "treetag")
        except RevisionNotFound as e:
            print(f"  タグがツリーを指す: RevisionNotFound(cause={type(e.__cause__).__name__}: {e.__cause__})")
    follow_rename_repo(rr, results)

    # ---------- click（F-PY 規模）
    click_root = None
    if args.click:
        click_root = TMP / "click"
        if not click_root.exists():
            subprocess.run(["git", "clone", "-q", "https://github.com/pallets/click.git", str(click_root)], check=True)
        with Repo(str(click_root)) as rc:
            clist = ["HEAD", "HEAD~3", "HEAD~500", "HEAD~3000", "main", "origin/main", "0.1", "8.1.7", "8.1.7~2", "8.1.7^{}", rc.head().decode()[:7],
                     "master", "nonexistent"]
            ambiguous = find_ambiguous_prefix(rc)
            if ambiguous:
                clist.append(ambiguous)
            results["1 click"] = item1(rc, click_root, "click（pack 済み 3379 コミット）", clist)
            if ambiguous:
                try:
                    parse_commit(rc, ambiguous)
                except AmbiguousShortId as e:
                    print(f"  曖昧な 4 桁 {ambiguous!r} -> AmbiguousShortId（Exception 直下。KeyError ではない）候補 {len(e.options)} 件")
        measure("click（F-PY 規模の代役）", click_root, ["pyproject.toml", "src/click/core.py"], "HEAD~100", "src/click/core.py", args.reps, results,
                click=True)
        pack_handle_test(click_root)

    # ---------- 項目 8 と status（一時 clone と実リポジトリ）
    item8(results)
    import_cost()
    if click_root:
        rmtree_force(click_root)
    for d in ("rename", "empty", "dirty", "click_copy"):
        rmtree_force(TMP / d)
    try:
        TMP.rmdir()
    except OSError:
        pass
    print("\n## 総括")
    for k, v in results.items():
        print(f"  {'合格' if v else '不合格'}  {k}")
    return 0


def find_ambiguous_prefix(repo: Repo) -> str | None:
    seen: dict[str, int] = {}
    for e in repo.get_walker():
        p = e.commit.id[:4].decode()
        seen[p] = seen.get(p, 0) + 1
        if seen[p] == 2:
            return p
    return None


def measure(label, root: Path, files, ref_old, follow_path, reps, results, click=False):
    print(f"\n## 2〜7 [{label}]")
    with Repo(str(root)) as repo:
        head = resolve_revision(repo, "HEAD")
        total = sum(1 for _ in repo.get_walker())
        print(f"  HEAD={head[:8]} コミット数={total} 追跡ファイル={len(list(repo.open_index().paths()))} 形式={'pack' if list((root/'.git/objects/pack').glob('*.pack')) else 'ルーズのみ'}")

        # 2: HEAD~3 の blob（resolve 込み・warm の同一 Repo）
        for f in files:
            want = git(root, "show", f"HEAD~3:{f}")  # テキストの照合
            b = read_blob(repo, resolve_revision(repo, "HEAD~3"), f)
            same = b is not None and b.decode("utf-8", "replace").replace("\r\n", "\n") == want.replace("\r\n", "\n")
            t = bench(lambda: read_blob(repo, resolve_revision(repo, "HEAD~3"), f), reps)
            # cold: Repo を開き直す（pack index の読み込み込み）
            def cold():
                with Repo(str(root)) as r:
                    read_blob(r, resolve_revision(r, "HEAD~3"), f)
            tc = bench(cold, max(10, reps // 5))
            ok = t["med"] <= 50 and same
            results[f"2 HEAD~3 blob {f} [{label}]"] = ok
            print(f"  2 {'合格' if ok else '不合格'} {f} ({len(b or b'')} B) 照合={'一致' if same else '不一致'} warm: {fmt(t)} | Repo 開き直し込み: {fmt(tc)}")
        for rev in ("HEAD~3", "HEAD", "main"):
            t = bench(lambda: resolve_revision(repo, rev), reps)
            print(f"    resolve_revision({rev!r}): {fmt(t)}")
        t = bench(lambda: resolve_revision(repo, head[:7]), reps)
        print(f"    resolve_revision(短縮 SHA 7 桁): {fmt(t)}")
        # 欠損・ディレクトリ・途中がファイル
        c = resolve_revision(repo, "HEAD~3")
        print(f"    存在しないパス -> {read_blob(repo, c, 'no/such.txt')!r}, ディレクトリ -> {read_blob(repo, c, files[-1].split('/')[0] if '/' in files[-1] else 'src')!r}, 途中がファイル -> {read_blob(repo, c, files[0] + '/x')!r}")
        # list_tree
        t = bench(lambda: list(iter_tree_contents(repo.object_store, repo[c.encode()].tree)), max(10, reps // 5))
        n = len(list(iter_tree_contents(repo.object_store, repo[c.encode()].tree)))
        print(f"    list_tree（再帰 {n} ファイル、HEAD~3）: {fmt(t)}")

        # 3: STAGED（実リポジトリはステージ内容が他の役次第なので、取得方法と速度のみ）
        t = bench(lambda: read_staged_blob(repo, files[0]), reps)
        idx_blob = read_staged_blob(repo, files[0]); head_blob = read_blob(repo, head, files[0])
        print(f"  3 STAGED 取得 {files[0]}: {fmt(t)}（HEAD と同一={idx_blob == head_blob}、未追跡は {read_staged_blob(repo, 'zzz-untracked')!r}）")

        # 4: ツリー差分
        old = resolve_revision(repo, ref_old)
        got = changed_files(repo, old, head)
        gt = git(root, "diff-tree", "-r", "-M50%", "--name-status", old, head).strip().splitlines()
        gts = {_norm_status(l) for l in gt if l}
        ours = {(k, o if k in "RC" else None, n or o) for k, o, n in got}
        ok = ours == gts
        t = bench(lambda: changed_files(repo, old, head), max(10, reps // 5))
        results[f"4 差分 {ref_old}..HEAD [{label}]"] = ok
        print(f"  4 {'合格' if ok else '不合格'} {ref_old}..HEAD: {len(got)} ファイル（"
              + ", ".join(f"{k}{sum(1 for g in got if g[0]==k)}" for k in "ADMRC" if any(g[0] == k for g in got)) + f"） git と{'一致' if ok else '不一致'}: {t['med']:.1f} ms（中央値。初回 {t['first']:.1f}）")
        if not ok:
            print("     dulwich のみ:", sorted(ours - gts)[:5], " git のみ:", sorted(gts - ours)[:5])
        if click:
            # 改名を含む既知のコミット（click.py -> click/core.py: git は R053、src 移動: R100）
            for sub in ("R053", "R100"):
                line = git(root, "log", "--follow", "--name-status", "--format=C %H", "--", "src/click/core.py")
                cs = None; last = None
                for ln in line.splitlines():
                    if ln.startswith("C "):
                        last = ln[2:]
                    elif ln.startswith(sub):
                        cs = last; break
                if not cs:
                    continue
                par = git(root, "rev-parse", cs + "^").strip()
                for thr in (50, 60):
                    got2 = changed_files(repo, par, cs, threshold=thr)
                    gt2 = {_norm_status(l) for l in git(root, "diff-tree", "-r", "-M50%", "--name-status", par, cs).strip().splitlines() if l}
                    ours2 = {(k, o if k in "RC" else None, n or o) for k, o, n in got2}
                    r_n = sum(1 for g in got2 if g[0] == "R")
                    t2 = bench(lambda: changed_files(repo, par, cs, threshold=thr), 10)
                    print(f"    改名コミット {sub} {cs[:8]} の差分 threshold={thr}: 変更 {len(got2)} 件(うち R {r_n}) git との一致={'一致' if ours2 == gt2 else '不一致'} {t2['med']:.1f} ms")

        # 5: log --follow
        gt_follow = git(root, "log", "--follow", "--format=%H", "--", follow_path).split()
        nrep = 1 if click else max(5, reps // 10)       # click の Walker は十数秒かかるので 1 回だけ
        for thr in ((50,) if click else (50, 60)):
            t0 = time.perf_counter(); fl = file_log(repo, head, follow_path, threshold=thr); tt = {"med": (time.perf_counter() - t0) * 1000}
            if not click:
                tt = bench(lambda: file_log(repo, head, follow_path, threshold=thr), nrep)
            shas = [x[0] for x in fl]
            same = shas == gt_follow
            ok = tt["med"] <= 500
            results[f"5 log --follow {follow_path} Walker [{label}] threshold={thr}"] = ok
            print(f"  5 {'合格' if ok else '不合格'} Walker {follow_path} threshold={thr}: dulwich {len(shas)} 件 / git {len(gt_follow)} 件 / 完全一致={same} / 無制限 {tt['med']:.0f} ms")
            if not same:
                print(f"     dulwich のみ {len(set(shas)-set(gt_follow))} 件、git のみ {len(set(gt_follow)-set(shas))} 件、順序差のみ={set(shas)==set(gt_follow)}")
        for mc in (None, 500):
            ff = file_log_fast(repo, head, follow_path, max_commits=mc)
            fs = [x[0] for x in ff]
            tf = bench(lambda: file_log_fast(repo, head, follow_path, max_commits=mc), nrep)
            if mc is None:
                same = fs == gt_follow
                cmp = f" / git {len(gt_follow)} 件と{'完全一致' if same else f'不一致（自前のみ {len(set(fs)-set(gt_follow))}、git のみ {len(set(gt_follow)-set(fs))}、集合は同じ={set(fs)==set(gt_follow)}）'}"
            else:
                same = fs == gt_follow[:len(fs)]
                cmp = f" / git の先頭 {len(fs)} 件と{'完全一致' if same else '不一致'}"
            ok = tf["med"] <= 500 and same
            results[f"5 log --follow {follow_path} 自前 max_commits={mc} [{label}]"] = ok
            print(f"  5 {'合格' if ok else '不合格'} 自前 {follow_path} max_commits={mc}: {len(ff)} 件{cmp} / 中央値 {tf['med']:.0f} ms（初回 {tf['first']:.0f}）")

        # 6: .gitignore
        ignore_test(repo, root, reps, results, label)

        # 7: status
        from dulwich import porcelain
        for mode in ("normal", "no"):
            t = bench(lambda: porcelain.status(repo, untracked_files=mode, optional_locks=False), 3)
            print(f"  7 porcelain.status(untracked_files={mode!r}, optional_locks=False) [{label}]: {fmt(t)}")
        st = porcelain.status(repo, untracked_files="normal", optional_locks=False)
        gt_st = git(root, "status", "--porcelain=v1", "-unormal").splitlines()
        print(f"    staged={ {k: len(v) for k, v in st.staged.items()} } unstaged={len(st.unstaged)} untracked={[p.decode() for p in st.untracked][:6]}"
              f" | git: {len(gt_st)} 行（他の役の作業中は一致しないことがある）")


def _norm_status(line: str):
    parts = line.split("\t")
    k = parts[0][0]
    if k in "RC":
        return (k, parts[1], parts[2])
    return (k, None, parts[1])


def ignore_test(repo, root: Path, reps, results, label):
    mgr = IgnoreFilterManager.from_repo(repo)
    cases = [(".venv/", True), (".venv/Lib/site-packages/x.py", True), ("src/lucianke/__pycache__/", True),
             ("src/lucianke/__pycache__/a.cpython-312.pyc", True), ("spikes/s05_ts/node_modules/", True),
             ("spikes/s05_ts/node_modules/a/b.js", True), ("src/lucianke/server.py", None), ("README.md", None), ("a.pyc", True)]
    if label.startswith("click"):
        cases = [(".venv/", None), ("src/click/__pycache__/a.pyc", True), ("docs/_build/", True), ("src/click/core.py", None)]
    print("  6 .gitignore 判定（IgnoreFilterManager.is_ignored: True=無視, False=明示的に除外解除, None=言及なし）")
    ok = True
    for p, want in cases:
        got = mgr.is_ignored(p)
        gt = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--no-index", p],
                            capture_output=True, env=GIT_ENV).returncode == 0
        agree = (got is True) == gt
        ok &= agree
        print(f"    {'OK' if agree else 'NG'} {p!r:50} dulwich={got!r:5} git check-ignore={gt}")
    # 1,000 パス（実在ファイル + 合成）
    paths = []
    for dp, dn, fn in os.walk(root):
        rel = Path(dp).relative_to(root).as_posix()
        if rel.startswith(".git"):
            dn[:] = []; continue
        for f in fn:
            paths.append(f if rel == "." else f"{rel}/{f}")
        if len(paths) > 3000:
            break
    paths = (paths * 3)[:1000] if len(paths) < 1000 else paths[:1000]
    t = bench(lambda: [mgr.is_ignored(p) for p in paths], 5)
    def fresh():   # マネージャを作り直して 1,000 件（.gitignore の読み直し込み）
        m = IgnoreFilterManager.from_repo(repo)
        return [m.is_ignored(p) for p in paths]
    tf = bench(fresh, 3)
    tm = bench(lambda: IgnoreFilterManager.from_repo(repo), 10)
    res_ok = t["med"] <= 100
    results[f"6 ignore 判定の一致 [{label}]"] = ok
    print(f"    1,000 パス（実在ファイル）: warm 中央値 {t['med']:.1f} ms / 作り直し込み {tf['med']:.1f} ms（初回 {tf['first']:.1f}）/ from_repo 単体 {tm['med']:.1f} ms")
    dirs = [p + "/" for p in {os.path.dirname(x) for x in paths} if p]
    t2 = bench(lambda: [mgr.is_ignored(d) for d in dirs[:1000]], 5)
    print(f"    ディレクトリ（末尾スラッシュ）{min(len(dirs),1000)} 件: {t2['med']:.1f} ms。末尾スラッシュ無しだと os.path.isdir() で FS を叩く: "
          f"{bench(lambda: [mgr.is_ignored(d.rstrip('/')) for d in dirs[:1000]], 5)['med']:.1f} ms")


def follow_rename_repo(rr: Path, results):
    print("\n## 5b 改名をまたぐ履歴の正しさ（合成リポジトリ。正解は git log --follow）")
    with Repo(str(rr)) as r:
        head = resolve_revision(r, "HEAD")
        for path in ("pkg/d.py", "pkg/c.py", "a.py"):
            gt = git(rr, "log", "--follow", "--format=%H", "--", path).split()
            for thr in (50, 60):
                fl = file_log(r, head, path, threshold=thr)
                shas = [x[0] for x in fl]
                fs = file_log_fast(r, head, path, threshold=thr)
                print(f"  {path:9} threshold={thr}: Walker {len(shas)} 件 一致={shas == gt} 種別={''.join(x[1] for x in fl)} / 自前 {len(fs)} 件 一致={[x[0] for x in fs] == gt} 種別={''.join(x[1] for x in fs)} / git {len(gt)} 件")
        # 通常の blob 差分（改名つき）
        c = [x for x in git(rr, "log", "--format=%H").split()]
        for i in (2, 4, 7):
            got = changed_files(r, c[i], c[i - 1] if False else c[i - 1])  # c[i-1] は 1 つ新しい
        print("  tree diff（改名の判別）:")
        for lbl, (a, b) in {"a->b(+小修正)": (7, 6), "b->pkg/c": (5, 4), "c->d(+大書き換え)": (2, 1)}.items():
            # log は新しい順: c[7] が 3 番目のコミット...
            got = changed_files(r, c[a], c[b])
            gt = git(rr, "diff-tree", "-r", "-M50%", "--name-status", c[a], c[b]).strip().splitlines()
            print(f"    {lbl}: dulwich={got} / git={[l.replace(chr(9), ' ') for l in gt]}")


def pack_handle_test(click_root: Path):
    print("\n## Windows の保持ハンドル（Repo を開いたまま pack を触れるか）")
    cp = TMP / "click_copy"
    rmtree_force(cp)
    shutil.copytree(click_root, cp)
    pack = next((cp / ".git/objects/pack").glob("*.pack"))
    r = Repo(str(cp))
    resolve_revision(r, "HEAD~3"); read_blob(r, resolve_revision(r, "HEAD"), "pyproject.toml")
    try:
        os.replace(pack, pack.with_suffix(".bak"))
        print("  Repo 開いたまま pack を改名: 成功（ハンドルを保持しない）")
        os.replace(pack.with_suffix(".bak"), pack)
    except PermissionError as e:
        print(f"  Repo 開いたまま pack を改名: PermissionError winerror={getattr(e, 'winerror', None)}（git gc / repack と衝突し得る）")
    r.close()
    try:
        os.replace(pack, pack.with_suffix(".bak"))
        print("  repo.close() 後に pack を改名: 成功")
    except PermissionError as e:
        print(f"  repo.close() 後も PermissionError winerror={getattr(e, 'winerror', None)}")


def item8(results):
    print("\n## 8 不変性（dulwich の読み取りが index / refs / 作業ツリーを変えないこと）")
    from dulwich import porcelain
    dirty = make_dirty_clone(TMP / "dirty")
    with Repo(str(dirty)) as r:   # 項目 2〜7 と同じ操作を一通り
        # staged の取得（README は staged と worktree で内容が違う）
        s = read_staged_blob(r, "README.md"); hb = read_blob(r, resolve_revision(r, "HEAD"), "README.md")
        wt = (dirty / "README.md").read_bytes()
        print(f"  3 STAGED 取得: staged={s!r} / worktree={wt!r} / HEAD の先頭={hb[:20]!r}（staged が worktree・HEAD と別物として取れた={s not in (wt, hb)}）")
        print(f"    新規 staged（newfile.txt）={read_staged_blob(r, 'newfile.txt')!r}、未追跡={read_staged_blob(r, 'untracked.txt')!r}")
        for spec in ("light", "ann", "ann^{}"):
            try:
                print(f"    タグ {spec!r} -> {resolve_revision(r, spec)[:12]}（git: {git(dirty, 'rev-parse', spec + '^{commit}').strip()[:12]}）")
            except RevisionNotFound as e:
                print(f"    タグ {spec!r} -> RevisionNotFound")
    # git の書き換え分を先に済ませる
    settle_git_index(dirty); settle_git_index(dirty)
    before = state_digest(dirty); wt_before = settle_git_index(dirty)
    wt_files_before = {p.relative_to(dirty).as_posix(): sha256_file(p) for p in dirty.rglob("*") if p.is_file() and ".git" not in p.relative_to(dirty).parts}
    with Repo(str(dirty)) as r:
        head = resolve_revision(r, "HEAD")
        for rev in ("HEAD~3", "main", "origin/main", "light", "ann", head[:7]):
            resolve_revision(r, rev)
        read_blob(r, head, "README.md"); read_staged_blob(r, "README.md"); changed_files(r, resolve_revision(r, "HEAD~5"), head)
        file_log(r, head, "pyproject.toml"); IgnoreFilterManager.from_repo(r).is_ignored("node_modules/x.js")
        st = porcelain.status(r, optional_locks=False)
        print(f"  7 status（optional_locks=False）: staged={ {k: [p.decode() for p in v] for k, v in st.staged.items() if v} } unstaged={[p.decode() for p in st.unstaged]} untracked={[p.decode() for p in st.untracked]}")
    after = state_digest(dirty)
    wt_files_after = {p.relative_to(dirty).as_posix(): sha256_file(p) for p in dirty.rglob("*") if p.is_file() and ".git" not in p.relative_to(dirty).parts}
    same = before == after and wt_files_before == wt_files_after
    results["8 一時 clone（解決・blob・staged・差分・follow・ignore・status[optional_locks=False]）"] = same
    print(f"  8a {'合格' if same else '不合格'} 一時 clone（dirty）: index/HEAD/refs/objects/作業ツリー全ファイル sha が前後で{'同一' if same else '変化'}"
          + ("" if same else f" 差分={ {k: (before[k], after[k]) for k in before if before[k] != after[k]} }"))
    # 既定の status（optional_locks=True）は?
    lic = dirty / "LICENSE"; os.utime(lic, (time.time() - 9000, time.time() - 9000))   # git の整え直しの後に stat だけずらす
    before = state_digest(dirty)
    with Repo(str(dirty)) as r:
        porcelain.status(r, optional_locks=False)
    mid = state_digest(dirty)
    with Repo(str(dirty)) as r:
        porcelain.status(r)
    after2 = state_digest(dirty)
    print(f"  8b' optional_locks=False: 変化={sorted(k for k in before if before[k] != mid[k]) or 'なし'}")
    changed = {k for k in mid if mid[k] != after2[k]}
    print(f"  8b porcelain.status() 既定（optional_locks=True）: 変化した項目={sorted(changed) or 'なし'}（LICENSE の mtime だけ変えてあった）")
    results["8 status 既定が index を書く（不採用の根拠）"] = True

    # 実リポジトリ: git status で整えてから前後の指紋
    ok = False
    for attempt in range(3):
        settle_git_index(ROOT)
        b = state_digest(ROOT); wb = settle_git_index(ROOT); b = state_digest(ROOT)
        with Repo(str(ROOT)) as r:
            head = resolve_revision(r, "HEAD")
            for rev in ("HEAD~3", "main", "origin/main", head[:7]):
                resolve_revision(r, rev)
            read_blob(r, head, "README.md"); read_staged_blob(r, "README.md"); changed_files(r, resolve_revision(r, "HEAD~5"), head)
            file_log(r, head, "pyproject.toml"); IgnoreFilterManager.from_repo(r).is_ignored(".venv/")
            porcelain.status(r, untracked_files="normal", optional_locks=False)
        a = state_digest(ROOT)
        if b["index"] == a["index"] and b["refs"] == a["refs"] and b["HEAD"] == a["HEAD"] and b["packed-refs"] == a["packed-refs"]:
            ok = True; break
        print(f"  8c 試行 {attempt + 1}: 他の役のステージ/コミットと重なった可能性 {[k for k in b if b[k] != a[k]]}（再試行）")
    results["8 実リポジトリ（index・refs・HEAD 不変）"] = ok
    print(f"  8c {'合格' if ok else '不合格'} 実リポジトリ: index sha256={a['index'][:12] if a['index'] else None} 前後で{'同一' if ok else '変化'}（git status を先に流して index を整えた後）")


def import_cost():
    print("\n## import コスト（別プロセス）")
    for mods in ("dulwich.repo, dulwich.objectspec, dulwich.diff_tree, dulwich.walk, dulwich.ignore",
                 "dulwich.repo, dulwich.objectspec, dulwich.diff_tree, dulwich.walk, dulwich.ignore, dulwich.porcelain"):
        code = ("import time,sys;t=time.perf_counter();import " + mods +
                ";dt=(time.perf_counter()-t)*1000;print(f'{dt:.0f} ms', 'urllib3' in sys.modules, 'ssl' in sys.modules, 'http.client' in sys.modules, len(sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
        print(f"  import {mods.split(', ')[-1]} まで: {out}  (時間, urllib3, ssl, http.client, 全モジュール数)")


if __name__ == "__main__":
    sys.exit(main())
