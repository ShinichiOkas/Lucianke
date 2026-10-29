"""Compact DSL の文法検査器（仕様書 §6.1 の EBNF 由来。設計書 §7.2.4・§10.2）。

``validate(text)`` は違反の説明の列を返す。空なら適合。コードフェンスの内側は検査しない。

行種は行ごとの正規表現で判定する。語彙（kind・modifier・attr-key・use・sev・level）は **この検査器に独立して**
書き写してある（ビルダ側の定数を import しない。ビルダのバグを検査器が共有しないため）。

EBNF と §5.4 の記述・例のあいだで食い違う点は、次のように決めている:

* ヘッダ末尾の ``~<used>/<max>tok`` は ``key=value`` ではないが、§5.4.2 のとおり許す（成功ヘッダでは必須）。
* ``tree-line`` の ``(cycle)``（§11 T-07 の例）は ``:<line>`` の直後に 1 つ許す。
* ``! `` 行（banner）は先頭だけでなく本文中にも許す（§5.4.4 の失敗項目、§5.4.2 の候補行）。
* ``kv-line`` はインデント 0、``attr-line`` はインデント 2 以上（キーは attr-key 語彙に限る）で区別する。
* トレーラは ``# summary:`` → ``# more:`` の順で末尾にだけ置ける。それ以外の ``# `` 行は違反。
* §11 の例のうち EBNF に合わないもの（``@L`` の無い ``[Node] ×23``、範囲の後ろの ``(25 lines)``、
  インデント付きの ``  resolved:``）は **適合としない**。EBNF に従う。
"""

from __future__ import annotations

import re

KINDS = frozenset(
    "Module Class Interface Struct Enum Trait Type Function Method Constructor Field Property Variable "
    "Constant Table Column Endpoint Message Service Node Edge Cell Tag Key Section File".split()
)
ATTR_KEYS = frozenset(
    "sig doc urn type body when conf scip decorators raises refs rank sha declared_in change".split()
)
USES = frozenset("r w rw call type import impl".split())
SEVERITIES = frozenset("EWIH")
LEVELS = frozenset({"exact", "structural", "heuristic", "lexical_fallback"})
LSP_STATUSES = frozenset({"ready", "indexing", "standby", "unavailable", "n/a"})
LINE_LIMIT = 200

_INDENT = r"(?P<indent>(?:  )*)"
_KEY = r"[A-Za-z_][A-Za-z0-9_.\-]*"

RE_HEADER = re.compile(
    r"^# (?P<tool>[A-Za-z_][A-Za-z0-9_]*) (?P<status>ok|error E_[A-Z0-9_]+)"
    r"(?P<kvs>(?: [A-Za-z_][A-Za-z0-9_]*=\S+)*)(?: ~(?P<used>\d+)/(?P<max>\d+)tok)?$"
)
RE_BANNER = re.compile(r"^! \S.*$")
RE_FILE = re.compile(
    r"^=== (?P<path>.+) \((?P<lines>\d+) lines(?:, (?P<cells>\d+) cells)?"
    r"(?:, skeleton: (?P<skel>\d+) lines)?(?:, sha=(?P<sha>[0-9A-Fa-f]{12}))?\) ===$"
)
RE_IMPORT = re.compile(r"^imports: \[.*\]$")
RE_SYMBOL = re.compile(
    rf"^{_INDENT}\[(?P<kind>[A-Za-z]+)\] (?P<rest>.+?) @L(?P<start>\d+)(?:-(?P<end>\d+))?"
    r"(?P<fold> \{ \.\.\.\d+ (?:lines|nodes) omitted \})?$"
)
RE_ATTR = re.compile(rf"^(?P<indent>(?:  )+)(?P<key>{_KEY}): (?P<text>.+)$")
RE_REF = re.compile(
    rf"^{_INDENT}(?P<path>\S.*?):(?P<line>\d+)(?::(?P<col>\d+))?  (?P<rest>.*)$"
)
RE_DIAG = re.compile(
    r"^(?P<sev>[EWIH]) (?P<path>\S.*?):(?P<line>\d+)(?::(?P<col>\d+))? "
    r"(?:\[(?P<source>[^:\]\s]+):(?P<code>[^\]\s]+)\] )?(?P<msg>\S.*?)(?:  fix=(?P<fix>\S+))?$"
)
RE_TREE = re.compile(
    rf"^{_INDENT}(?P<dir><-|->) (?P<name>\S.*?) @(?P<path>\S.*?):(?P<line>\d+)(?P<cycle> \(cycle\))?"
    r"(?:  when: (?P<when>\S.*?))?(?:  conf=(?P<conf>[a-z_]+))?$"
)
RE_PATH = re.compile(r"^path (?P<n>\d+) \((?P<hops>\d+) hops\)$")
RE_STEP = re.compile(
    r"^(?P<indent>(?:  )*)(?P<n>\d+)\. (?P<name>\S.*?) @(?P<path>\S.*?):(?P<line>\d+)"
    r"(?:  var=(?P<var>\S.*?))?(?:  when: (?P<when>\S.*?))?$"
)
RE_FENCE = re.compile(r"^(?P<fence>`{3,})(?P<lang>[^\s`]*)$")
RE_TABLE = re.compile(r"^\| .* \|$")
RE_NOTE = re.compile(r"^- \S.*$")
RE_KV = re.compile(rf"^(?P<key>{_KEY}):(?: .*)?$")
RE_MORE = re.compile(r"^# more: cursor=(?P<cursor>[A-Za-z0-9_\-=.]+)(?: remaining≈(?P<n>\d+))?$")
RE_SUMMARY = re.compile(r"^# summary: \S.*$")
_RE_ATTR_SHAPE = re.compile(rf"^ +(?P<key>{_KEY}): ")


def _check_header(line: str, add) -> None:
    m = RE_HEADER.match(line)
    if not m:
        add("ヘッダ行が `# <tool> (ok|error <CODE>) [key=value ...] [~used/maxtok]` の形でない")
        return
    kvs = dict(p.split("=", 1) for p in m["kvs"].split())
    if m["status"] == "ok":
        for required in ("conf", "lsp", "rev", "ws"):
            if required not in kvs:
                add(f"成功ヘッダに {required}= が無い")
        if m["used"] is None:
            add("成功ヘッダに ~<used>/<max>tok が無い")
        if kvs.get("conf") not in LEVELS and "conf" in kvs:
            add(f"conf= の値が水準でない: {kvs['conf']!r}")
        lsp = kvs.get("lsp")
        if lsp is not None and re.sub(r"\(\d+%\)$", "", lsp) not in LSP_STATUSES:
            add(f"lsp= の値が語彙外: {lsp!r}")


def validate(text: str, *, require_header: bool = True) -> list[str]:
    """Compact DSL の ``text`` を検査し、違反の説明の列を返す（空なら適合）。

    ``require_header`` を偽にすると、先頭がヘッダ行でなくてよく、末尾の改行も要求しない
    （``Record.lines`` だけを連結した本文の検査用）。
    """
    problems: list[str] = []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    elif require_header:
        problems.append("末尾が改行で終わっていない")
    if require_header and not lines:
        return [*problems, "空の文書（ヘッダ行が無い）"]

    fence: int | None = None  # 開いているフェンスの長さ
    fence_start = 0
    table_cols: int | None = None  # 直前の行が表のとき、その列数
    trailer: str | None = None  # 最後に見たトレーラ行の種類（"summary" | "more"）

    for no, line in enumerate(lines, start=1):

        def add(reason: str, _no: int = no, _line: str = line) -> None:
            problems.append(f"line {_no}: {reason}: {_line!r}")

        if fence is not None:  # コードフェンスの内側は検査しない
            m = re.fullmatch(r"(`{3,})", line)
            if m and len(m[1]) >= fence:
                fence = None
            continue

        if not line.strip():
            add("空行は文法に無い（コードフェンスの外）")
            table_cols = None
            continue
        indent_len = len(line) - len(line.lstrip(" "))
        if line[indent_len : indent_len + 1] in ("\t", "\r"):
            add("タブ・CR は使えない")
            continue
        if indent_len % 2:
            add("インデントが 2 の倍数でない")
            continue

        # --- ヘッダ・トレーラ -----------------------------------------------------------
        if line.startswith("# ") and not line.startswith(("# more:", "# summary:")):
            if no == 1:
                _check_header(line, add)
            else:
                add("`# ` で始まる行はヘッダ（1 行目）とトレーラ（`# summary:` / `# more:`）だけ")
            continue
        if no == 1 and require_header:
            add("1 行目がヘッダ行でない")
        if line.startswith("# summary:"):
            if trailer == "more":
                add("`# summary:` は `# more:` より前に置く")
            elif not RE_SUMMARY.match(line):
                add("`# summary: <text>` の形でない")
            trailer = trailer or "summary"
            table_cols = None
            continue
        if line.startswith("# more:"):
            if not RE_MORE.match(line):
                add("`# more: cursor=<token>[ remaining≈N]` の形でない")
            trailer = "more"
            table_cols = None
            continue
        if trailer is not None:
            add("トレーラ（`# summary:` / `# more:`）の後に本文を置けない")
            continue

        # --- 表（連続する行は列数を揃える）----------------------------------------------
        if indent_len == 0 and line.startswith("| "):
            if not RE_TABLE.match(line):
                add("表の行が `| a | b |` の形でない")
            else:
                cols = len(line[2:-2].split(" | "))
                if table_cols is not None and cols != table_cols:
                    add(f"表の列数が前の行 ({table_cols}) と違う ({cols})")
                table_cols = cols
            continue
        table_cols = None

        if indent_len == 0:
            fence_len = _check_top_level(line, add)
            if fence_len:
                fence, fence_start = fence_len, no
            continue
        _check_indented(line, add)

    if fence is not None:
        problems.append(f"line {fence_start}: コードフェンスが閉じていない")
    return problems


def _check_top_level(line: str, add) -> int:
    """インデント 0 の 1 行を検査する。コードフェンスを開いたときはそのフェンスの長さ、それ以外は 0。"""
    if line.startswith("!"):
        if not RE_BANNER.match(line):
            add("警告行は `! <text>`")
    elif line.startswith("==="):
        if not RE_FILE.match(line):
            add("file-block 見出しが `=== path (N lines[, M cells][, skeleton: K lines][, sha=<12hex>]) ===` の形でない")
    elif line.startswith("```"):
        m = RE_FENCE.match(line)
        if m is None:
            add("コードフェンスの言語指定が不正")
        else:
            return len(m["fence"])
    elif line.startswith("imports:"):
        if not RE_IMPORT.match(line):
            add("import-line は `imports: [..]`")
    elif line.startswith("- "):
        if not RE_NOTE.match(line):
            add("note は `- <text>`")
    elif line.startswith("path "):
        if not RE_PATH.match(line):
            add("path 見出しが `path <n> (<hops> hops)` の形でない")
    elif line.startswith("["):
        _check_symbol(line, add)
    elif re.match(r"^[EWIH] ", line):
        m = RE_DIAG.match(line)
        if m is None:
            add("diag-line は `<sev> <path>:<line>[:<col>] [[source:code] ]<message>[  fix=<id>]`")
        elif int(m["line"]) < 1 or (m["col"] is not None and int(m["col"]) < 1):
            add("行・列は 1 始まり")
    elif RE_TREE.match(line):
        _check_tree(line, add)
    elif RE_KV.match(line):
        pass  # kv-line
    elif (m := RE_REF.match(line)) is not None:
        _check_ref(m, line, add)
    else:
        add("どの行種にも当てはまらない")
    return 0


def _check_indented(line: str, add) -> None:
    """インデント 2 以上の 1 行を検査する（symbol-block・attr-line・ref-line・tree-line・step-line）。"""
    if line.lstrip().startswith("["):
        _check_symbol(line, add)
    elif RE_TREE.match(line):
        _check_tree(line, add)
    elif (step := RE_STEP.match(line)) is not None:
        _check_step(step, line, add)
    elif (shape := _RE_ATTR_SHAPE.match(line)) is not None:
        # 規約: インデント付きの `key: ` は attr-line だけ（kv-line や note に落とさない）
        if shape["key"] not in ATTR_KEYS:
            add(f"未知の attr-key {shape['key']!r}")
        elif not RE_ATTR.match(line):
            add("attr-line の本文が空")
        elif shape["key"] in ("doc", "when") and len(line) > LINE_LIMIT:
            add(f"doc/when の行が {LINE_LIMIT} 文字を超えている（規約 6）")
    elif (ref := RE_REF.match(line)) is not None:
        _check_ref(ref, line, add)
    else:
        add("どの行種にも当てはまらない")


def _check_symbol(line: str, add) -> None:
    m = RE_SYMBOL.match(line)
    if not m:
        add("symbol-block 見出しが `[Kind] [mods ]name[(sig)] @L<a>[-<b>][ { ...N lines|nodes omitted }]` の形でない")
        return
    if m["kind"] not in KINDS:
        add(f"未知の kind {m['kind']!r}")
    start = int(m["start"])
    end = int(m["end"]) if m["end"] else start
    if start < 1:
        add("行番号は 1 始まり")
    if end < start:
        add("範囲の終端が始端より小さい")


def _check_tree(line: str, add) -> None:
    m = RE_TREE.match(line)
    if m is None:
        return
    if m["conf"] is not None and m["conf"] not in LEVELS:
        add(f"conf= が水準でない: {m['conf']!r}")
    if int(m["line"]) < 1:
        add("行番号は 1 始まり")
    if m["when"] is not None and len(line) > LINE_LIMIT:
        add(f"when を持つ行が {LINE_LIMIT} 文字を超えている（規約 6）")


def _check_step(m: re.Match[str], line: str, add) -> None:
    if int(m["line"]) < 1:
        add("行番号は 1 始まり")
    if m["when"] is not None and len(line) > LINE_LIMIT:
        add(f"when を持つ行が {LINE_LIMIT} 文字を超えている（規約 6）")


def _check_ref(m: re.Match[str], line: str, add) -> None:
    if int(m["line"]) < 1 or (m["col"] is not None and int(m["col"]) < 1):
        add("行・列は 1 始まり")
    rest = m["rest"]
    use = re.match(r"\[([a-z]+)\] ", rest)
    if use and use[1] not in USES:
        add(f"use が語彙外: {use[1]!r}")
    if len(line) > LINE_LIMIT:
        add(f"ref-line が {LINE_LIMIT} 文字を超えている（規約 6）")
