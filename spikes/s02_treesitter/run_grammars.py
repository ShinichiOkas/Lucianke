"""S-2 項目 2・3: 全文法の読み込み・ABI・サンプル解析・タグ風クエリを検査し、Markdown 表で出す。

使い方: uv run python spikes/s02_treesitter/run_grammars.py
出力: 言語取得関数の実名表 / ABI 表 / 同梱 TAGS_QUERY の可否 / 自作タグ風クエリの結果。失敗は例外メッセージ付きで出る。
前提: .venv 同期済み（tree-sitter 0.26 と tree_sitter_* 文法パッケージ）。
"""

from __future__ import annotations

import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

sys.stdout.reconfigure(encoding="utf-8")

import tree_sitter as ts  # noqa: E402
from grammars import G, REQUIRED_7, dist_version, load_language, run_tag_query  # noqa: E402


def bundled_tags(g):
    """パッケージ同梱の TAGS_QUERY を取り出す。(text|None, error|None)"""
    mod = importlib.import_module(g.module)
    try:
        v = getattr(mod, "TAGS_QUERY")
        return (v, None) if v is not None else (None, "TAGS_QUERY が None（同梱 .scm を見つけられない）")
    except Exception as e:  # AttributeError / FileNotFoundError / ModuleNotFoundError
        return None, f"{type(e).__name__}: {e}"


def error_nodes(node):
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.is_error or n.is_missing:
            out.append(n)
        if n.has_error:
            stack.extend(n.children)
    return out


print(f"tree-sitter {dist_version('tree-sitter')}  LANGUAGE_VERSION={ts.LANGUAGE_VERSION}  "
      f"MIN_COMPATIBLE_LANGUAGE_VERSION={ts.MIN_COMPATIBLE_LANGUAGE_VERSION}\n")

print("## 言語取得関数・ABI・解析")
print("| lang_id | 配布名 | 版 | 関数の実名 | abi_version | semantic_version | name | サンプル解析 | ERROR/MISSING | ルート型 |")
print("|---|---|---|---|---|---|---|---|---|---|")
loaded = {}
for g in G:
    try:
        lang = load_language(g)
        tree = ts.Parser(lang).parse(g.sample.encode())
        errs = error_nodes(tree.root_node)
        loaded[g.lang_id] = lang
        print(f"| {g.lang_id} | {g.dist} | {dist_version(g.dist)} | `{g.module}.{g.func}()` | {lang.abi_version} | "
              f"{lang.semantic_version} | {lang.name} | OK | {len(errs)} | {tree.root_node.type} |")
    except Exception as e:
        print(f"| {g.lang_id} | {g.dist} | {dist_version(g.dist)} | `{g.module}.{g.func}()` | - | - | - | **NG** {type(e).__name__}: {e} | - | - |")

print("\n## 同梱 TAGS_QUERY（パッケージが持つ tags.scm）と、自作タグ風クエリ")
print("| lang_id | 同梱 TAGS_QUERY | 同梱: compile | 同梱: matches 数 / name 付き | 自作: compile | 自作: matches 数 | 自作: 拾った (kind, name) |")
print("|---|---|---|---|---|---|---|")
results = {}
for g in G:
    if g.lang_id not in loaded:
        print(f"| {g.lang_id} | (未ロード) | | | | | |")
        continue
    lang = loaded[g.lang_id]
    src = g.sample.encode()
    # 同梱
    text, err = bundled_tags(g)
    if text is None:
        b = ("なし" if not g.bundled else f"**取得不可** ({err})", "-", "-")
    else:
        try:
            r = run_tag_query(lang, text, src)
            named = sum(1 for x in r if x[2])
            b = ("あり", "OK", f"{len(r)} / {named}")
        except Exception as e:
            b = ("あり", f"**NG** {type(e).__name__}: {str(e)[:80]}", "-")
    # 自作
    try:
        r = run_tag_query(lang, g.own_tags, src)
        picked = sorted({(k, n) for _, k, n, *_ in r})
        o = ("OK", str(len(r)), ", ".join(f"{k.split('.')[-1]}:{n}" for k, n in picked))
        results[g.lang_id] = len(r) > 0 and all(x[2] for x in r)
    except Exception as e:
        o = (f"**NG** {type(e).__name__}: {str(e)[:80]}", "-", "-")
        results[g.lang_id] = False
    print(f"| {g.lang_id} | {b[0]} | {b[1]} | {b[2]} | {o[0]} | {o[1]} | {o[2]} |")

# --- 個別の確認 -------------------------------------------------------------
print("\n## 個別の確認")
# (a) c_sharp の同梱クエリはディレクトリ名がハイフン付き (tree_sitter_c-sharp/queries) で import 経由では取れない
import tree_sitter_c_sharp  # noqa: E402

sp = pathlib.Path(tree_sitter_c_sharp.__file__).parent.parent
alt = sp / "tree_sitter_c-sharp" / "queries" / "tags.scm"
print(f"- c_sharp: tags.scm は `{alt.relative_to(sp)}`（存在: {alt.exists()}）。`tree_sitter_c_sharp.TAGS_QUERY` -> ", end="")
text, err = bundled_tags(next(g for g in G if g.lang_id == "c_sharp"))
print(err or "取得できた")  # c_sharp は None が返る（パッケージ不備）
if alt.exists():
    lang = loaded["c_sharp"]
    r = run_tag_query(lang, alt.read_text(encoding="utf-8"), next(g for g in G if g.lang_id == "c_sharp").sample.encode())
    print(f"  パス直読みの tags.scm は compile OK、matches {len(r)} 件: {sorted({(k.split('.')[-1], n) for _, k, n, *_ in r})}")

# (b) typescript の同梱 TAGS_QUERY は TS 固有構文だけ。JS の tags と連結して使えるか
import tree_sitter_javascript  # noqa: E402
import tree_sitter_typescript  # noqa: E402

for lid in ("typescript", "tsx"):
    g = next(x for x in G if x.lang_id == lid)
    lang = loaded[lid]
    cat = tree_sitter_javascript.TAGS_QUERY + "\n" + tree_sitter_typescript.TAGS_QUERY
    try:
        r = run_tag_query(lang, cat, g.sample.encode())
        print(f"- {lid}: JS.TAGS + TS.TAGS 連結: compile OK、{len(r)} 件 {sorted({(k.split('.')[-1], n) for _, k, n, *_ in r})}")
    except Exception as e:
        print(f"- {lid}: JS.TAGS + TS.TAGS 連結: NG {type(e).__name__}: {e}")
    try:
        r = run_tag_query(lang, tree_sitter_typescript.TAGS_QUERY, g.sample.encode())
        print(f"- {lid}: TS.TAGS 単独: {len(r)} 件 {sorted({(k.split('.')[-1], n) for _, k, n, *_ in r})}")
    except Exception as e:
        print(f"- {lid}: TS.TAGS 単独: NG {type(e).__name__}: {e}")

# (c) 合格判定（設計書 12.1: 対象 7 文法で解析とタグクエリが通る）
print("\n## 合格判定（対象 7 文法）")
ok_all = True
for lid in REQUIRED_7:
    ok = lid in loaded and results.get(lid, False)
    ok_all &= ok
    print(f"- {lid}: {'合格' if ok else '不合格'}")
print(f"=> S-2 合格条件: {'合格' if ok_all else '不合格'}")
sys.exit(0 if ok_all else 1)
