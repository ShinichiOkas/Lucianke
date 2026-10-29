"""S-3 の「使うコードの型」: ast-grep-py で構文パターンを検索し、置換テキストを自前展開して書き換える最小部品。

使い方: 直接実行しない。`uv run python spikes/s03_astgrep/run_astgrep.py` が import して検証する。
要点: `SgNode.replace(text)` はメタ変数を展開しない（文字列をそのまま Edit にするだけ）。展開は `expand_template` で行い、
      適用は `root.commit_edits([...])`。位置（Pos.line/column/index・Edit.start_pos/end_pos）はコードポイント単位の 0 始まり。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ast_grep_py import SgNode, SgRoot

# Lucianke の lang_id -> ast-grep の言語名。None は ast-grep が持たない言語（SgRoot が PanicException を投げる）
AST_GREP_LANG: dict[str, str | None] = {
    "python": "python", "typescript": "typescript", "tsx": "tsx", "javascript": "javascript",
    "json": "json", "yaml": "yaml", "markdown": "markdown", "rust": "rust", "go": "go",
    "c": "c", "cpp": "cpp", "c_sharp": "csharp",
    "toml": None, "xml": None, "dtd": None, "rst": None, "sql": None, "markdown_inline": None,
}

# メタ変数: $NAME（単一ノード）、$$$NAME（0 個以上のノード）。$_ / $_X / $$$ は捕捉しない。
META_RE = re.compile(r"\$\$\$([A-Z_][A-Z0-9_]*)?|\$([A-Z_][A-Z0-9_]*)")


class UnsupportedLanguage(ValueError):
    pass


class PatternError(ValueError):  # -> E_SYNTAX_INVALID（details.param = "pattern"）
    pass


class TemplateError(ValueError):  # -> E_SYNTAX_INVALID（details.param = "rewrite"）
    pass


def make_root(src: str, lang_id: str) -> SgNode:
    """SgRoot を作って root ノードを返す。未対応言語は事前に弾く（SgRoot は PanicException = BaseException の子を投げ、stderr にも出す）。"""
    name = AST_GREP_LANG.get(lang_id)
    if name is None:
        raise UnsupportedLanguage(lang_id)
    return SgRoot(src, name).root()


def pattern_vars(pattern: str) -> tuple[set[str], set[str]]:
    """(単一メタ変数名の集合, 複数メタ変数名の集合)。捕捉しない名前（_ 始まり・名前なし）は除く。"""
    single, multi = set(), set()
    for m in META_RE.finditer(pattern):
        name = m.group(1) if m.group(0).startswith("$$$") else m.group(2)
        if name and not name.startswith("_"):
            (multi if m.group(0).startswith("$$$") else single).add(name)
    return single, multi


CONTEXT_LANGS = {"go", "c"}  # 実測: 式だけのパターン（fmt.Println($M)）が一致しない言語。{"context": ..., "selector": ...} 形式で包む


def pattern_hint(lang_id: str, pattern: str | dict, n_matches: int) -> str | None:
    """一致 0 件のときだけ、パターン自体の問題を疑う手がかりを返す（確定はできないので警告として使う）。

    ast-grep は不正なパターンでもエラーにせず 0 件を返す（"print(" など）。一方 tree-sitter でパターン文字列を単独で
    解析して ERROR が出ても、Rust / Java / C# の `foo($M)` のように正しく一致する場合がある。だから一致 0 件のときだけ見る。
    """
    if n_matches:
        return None
    text = pattern if isinstance(pattern, str) else pattern["context"]
    reasons = []
    if make_root(text.replace("$", "µ"), lang_id).find(kind="ERROR") is not None:
        reasons.append("パターンが単独の構文として解析できない（ERROR）")
    if lang_id in CONTEXT_LANGS and isinstance(pattern, str):
        reasons.append(f"{lang_id} は式だけのパターンが一致しにくい（context / selector 形式で包む）")
    return "; ".join(reasons) or None


@dataclass(frozen=True)
class MetaVar:
    start: int  # コードポイント単位の絶対位置（Python の str インデックスと同じ）
    end: int
    text: str  # 複数メタ変数は「先頭ノードの開始〜末尾ノードの終了」のソース断片（区切りの , や空白を保つ）


@dataclass(frozen=True)
class Match:
    start: tuple[int, int]  # (行, コードポイント列) 0 始まり
    end: tuple[int, int]
    start_index: int  # コードポイント単位の絶対位置
    end_index: int
    text: str
    kind: str
    single: dict[str, MetaVar]
    multi: dict[str, MetaVar | None]  # 0 個一致は None
    node: SgNode = field(repr=False, compare=False)


def search(src: str, lang_id: str, pattern: str | dict, constraints: dict | None = None) -> list[Match]:
    """pattern は文字列、または {"context": ..., "selector": ...}（ast_grep_py.Pattern）。

    ast-grep が単独の AST ルートを作れないパターンは RuntimeError("cannot get matcher ...") になる -> PatternError にする。
    """
    root = make_root(src, lang_id)
    singles, multis = pattern_vars(pattern if isinstance(pattern, str) else pattern["context"])
    try:
        found = root.find_all({"rule": {"pattern": pattern}, "constraints": constraints}) if constraints else root.find_all(pattern=pattern)
    except RuntimeError as e:
        raise PatternError(str(e).splitlines()[0]) from e
    out: list[Match] = []
    for n in found:
        r = n.range()
        single = {}
        for name in singles:
            v = n.get_match(name)
            if v is not None:
                vr = v.range()
                single[name] = MetaVar(vr.start.index, vr.end.index, v.text())
        multi: dict[str, MetaVar | None] = {}
        for name in multis:
            nodes = n.get_multiple_matches(name)
            if nodes:
                s, e = nodes[0].range().start.index, nodes[-1].range().end.index
                multi[name] = MetaVar(s, e, src[s:e])
            else:
                multi[name] = None
        out.append(Match((r.start.line, r.start.column), (r.end.line, r.end.column), r.start.index, r.end.index, n.text(), n.kind(), single, multi, n))
    return out


def outermost(matches: list[Match]) -> list[Match]:
    """入れ子の一致（foo(foo(1)) の内側）を落として最も外側だけ残す。重なる Edit を commit_edits に渡さないため。"""
    out: list[Match] = []
    for m in sorted(matches, key=lambda m: (m.start_index, -m.end_index)):
        if not out or m.start_index >= out[-1].end_index:
            out.append(m)
    return out


def _line_indent(src: str, index: int) -> int:
    ls = src.rfind("\n", 0, index) + 1
    k = ls
    while k < len(src) and src[k] in " \t" and k < index:
        k += 1
    return k - ls


def expand_template(template: str, m: Match, src: str, pattern: str | dict) -> str:
    """`logger.info($MSG)` のようなテンプレートにメタ変数を展開する。

    - テンプレートが参照できるのはパターンで捕捉した変数だけ（それ以外は TemplateError）。
    - 複数行の `$$$X` は、2 行目以降のインデントを「挿入先の列」に合わせてずらす。
    - テンプレートが複数行のとき、2 行目以降には一致位置の行のインデントを足す（ast-grep CLI と同じ流儀）。
    """
    singles, multis = pattern_vars(pattern if isinstance(pattern, str) else pattern["context"])
    base_indent = _line_indent(src, m.start_index)
    lines_out: list[str] = []
    for ti, tline in enumerate(template.split("\n")):
        prefix = " " * base_indent if ti > 0 else ""
        cur = prefix
        pos = 0
        for mm in META_RE.finditer(tline):
            cur += tline[pos : mm.start()]
            pos = mm.end()
            is_multi = mm.group(0).startswith("$$$")
            name = mm.group(1) if is_multi else mm.group(2)
            if not name:
                raise TemplateError(f"{mm.group(0)}: 名前のないメタ変数は参照できない")
            if name not in (multis if is_multi else singles):
                raise TemplateError(f"{mm.group(0)}: パターンで捕捉していない")
            var = m.multi.get(name) if is_multi else m.single.get(name)
            if var is None:
                continue  # $$$X が 0 個一致 -> 空
            # 挿入先の列（出力側）と、捕捉ノードの元の列。複数行の捕捉は 2 行目以降をその差だけずらす
            target_col = len(cur) if ti > 0 else _col_in_line(src, m.start_index) + len(cur)
            shift = target_col - _col_in_line(src, var.start)
            text = _shift_continuation(var.text, shift)
            cur += text
        cur += tline[pos:]
        lines_out.append(cur)
    return "\n".join(lines_out)


def _col_in_line(src: str, index: int) -> int:
    return index - (src.rfind("\n", 0, index) + 1)


def _shift_continuation(text: str, shift: int) -> str:
    """複数行テキストの 2 行目以降の先頭の空白を shift 桁ぶん増減する（空行はそのまま）。"""
    if "\n" not in text or shift == 0:
        return text
    head, *rest = text.split("\n")
    out = []
    for r in rest:
        if not r.strip():
            out.append(r)
        elif shift > 0:
            out.append(" " * shift + r)
        else:
            out.append(r[min(-shift, len(r) - len(r.lstrip(" "))) :])
    return "\n".join([head, *out])


def rewrite(src: str, lang_id: str, pattern: str | dict, template: str, constraints: dict | None = None) -> tuple[str, int]:
    """パターンに一致する箇所（最も外側のみ）をテンプレートで置換した新しいソースと件数を返す。"""
    ms = outermost(search(src, lang_id, pattern, constraints))
    if not ms:
        return src, 0
    edits = [m.node.replace(expand_template(template, m, src, pattern)) for m in ms]  # replace は展開しない。展開済みの文字列を渡す
    return ms[0].node.get_root().root().commit_edits(edits), len(ms)
