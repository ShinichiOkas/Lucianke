"""Compact DSL のレンダラと、行種ごとの組み立て関数（仕様書 §6.1、§5.4.2、§8.4、設計書 §7.2.4）。

ツールと骨格（``syntax``）は文法を直接書かず、ここの関数を呼んで ``Record.lines`` を作る（設計書 §7.2.4）。
文法への適合は ``tests/contract/compact_grammar.py`` の検査器で確かめる。

規約（全関数に共通）:

* 戻り値は **改行を含まない 1 行の文字列**、または行の列（``list[str]``）。``Record.lines`` の各要素にそのまま入れる。
  ``render`` が ``"\\n"`` で連結する。
* 行・列は **1 始まり** で外に出す（仕様書 §5.10）。呼び手が 1 始まりの値を渡す前提で、関数は変換しない。
  0 以下を渡すと ``ValueError``（内部の 0 始まりが漏れたことを早く見つけるため）。
* テキスト欄の改行は空白 1 つに畳む（1 行に収める）。
* 規約 6: ``snippet`` ・``doc`` ・``when`` は、行全体が 200 文字を超えるとき、その欄の末尾を切って ``…`` で終え、
  行全体を 200 文字以内に収める。``sig`` とコードブロックには適用しない。
* 語彙（``kind`` ・``modifier`` ・``attr-key`` ・``use`` ・``sev`` ・``level``）は仕様書 §6.1 の EBNF に限る。違えば ``ValueError``。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lucianke.core.model import Confidence, Kind
from lucianke.core.records import ResolutionMeta
from lucianke.mcp.budget import trailer_lines
from lucianke.mcp.envelope import collect_warnings
from lucianke.mcp.types import Rendered, RenderInput

__all__ = [
    "ATTR_KEYS",
    "KINDS",
    "LINE_LIMIT",
    "MODIFIERS",
    "SEVERITIES",
    "USES",
    "attr_line",
    "banner",
    "banner_lines",
    "candidate_line",
    "code_block",
    "diag_line",
    "error_header_line",
    "file_block_header",
    "header_line",
    "import_line",
    "kv_line",
    "note",
    "path_header",
    "ref_line",
    "render",
    "step_line",
    "symbol_line",
    "table",
    "trailer_more",
    "trailer_summary",
    "tree_line",
]

LINE_LIMIT = 200  # 規約 6
KINDS: frozenset[str] = frozenset(k.value for k in Kind)
MODIFIERS: tuple[str, ...] = ("pub", "priv", "prot", "static", "async", "abstract", "export")
ATTR_KEYS: frozenset[str] = frozenset(
    {
        "sig", "doc", "urn", "type", "body", "when", "conf", "scip", "decorators", "raises",
        "refs", "rank", "sha", "declared_in", "change",
    }
)  # fmt: skip
USES: tuple[str, ...] = ("r", "w", "rw", "call", "type", "import", "impl")
SEVERITIES: tuple[str, ...] = ("E", "W", "I", "H")
_LEVELS: frozenset[str] = frozenset(c.value for c in Confidence)
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_ERROR_CODE_RE = re.compile(r"E_[A-Z0-9_]+")
_SHA_RE = re.compile(r"[0-9a-fA-F]{12,}")
_CURSOR_RE = re.compile(r"[A-Za-z0-9_\-=.]+")


# --- 内部の道具 -------------------------------------------------------------------------------


def _one_line(text: object) -> str:
    """改行を空白 1 つに畳み、前後の空白を除く（内側の空白の並びは保つ）。"""
    parts = [p.strip() for p in str(text).splitlines()]
    return " ".join(p for p in parts if p)


def _ind(level: int) -> str:
    if level < 0:
        raise ValueError(f"indent must be >= 0: {level}")
    return "  " * level


def _pos(name: str, value: int) -> int:
    if value < 1:
        raise ValueError(f"{name} は 1 始まり（>= 1）: {value}")
    return value


def _word(label: str, value: object, vocabulary: Iterable[str]) -> str:
    text = str(value)
    if text not in vocabulary:
        raise ValueError(f"{label} は {sorted(vocabulary)} のいずれか: {text!r}")
    return text


def _fit(prefix: str, text: str, suffix: str = "") -> str:
    """``prefix + text + suffix`` が 200 文字を超えるとき、``text`` の末尾を ``…`` に置き換えて収める。"""
    if len(prefix) + len(text) + len(suffix) <= LINE_LIMIT:
        return text
    keep = max(LINE_LIMIT - len(prefix) - len(suffix) - 1, 0)
    return text[:keep].rstrip() + "…"


def _no_space(text: object) -> str:
    return re.sub(r"\s+", "", str(text)) or "-"


def _level(value: object) -> str:
    return _word("conf", str(value), _LEVELS)


# --- ヘッダ・警告 -----------------------------------------------------------------------------


def header_line(
    tool: str,
    *,
    conf: Confidence | str,
    lsp: str,
    rev: str,
    ws: str | None,
    used_tokens: int,
    max_tokens: int,
    lsp_progress: int | None = None,
) -> str:
    """成功時のヘッダ行（仕様書 §5.4.2）。``# <tool> ok conf=… lsp=… rev=… ws=… ~<used>/<max>tok``。

    ``lsp_progress`` があれば ``lsp=indexing(40%)`` の形で添える（§8.4 規約 2: 呼び直しの目安）。
    ``ws`` が無ければ ``-``。
    """
    lsp_text = f"{_no_space(lsp)}({lsp_progress}%)" if lsp_progress is not None else _no_space(lsp)
    return (
        f"# {tool} ok conf={_level(conf)} lsp={lsp_text} rev={_no_space(rev)} "
        f"ws={_no_space(ws) if ws else '-'} ~{used_tokens}/{max_tokens}tok"
    )


def error_header_line(tool: str, code: str) -> str:
    """失敗時のヘッダ行 ``# <tool> error <CODE>``（仕様書 §5.4.2）。"""
    code_text = str(code)
    if not _ERROR_CODE_RE.fullmatch(code_text):
        raise ValueError(f"error code の形が違う: {code_text!r}")
    return f"# {tool} error {code_text}"


def banner(text: object) -> list[str]:
    """``! <text>`` の警告行。``text`` が複数行なら 1 行ずつ ``! `` を付ける。"""
    parts = [p.strip() for p in str(text).splitlines()]
    return [f"! {p}" for p in parts if p]


def banner_lines(meta: ResolutionMeta) -> list[str]:
    """降格時の警告行（仕様書 §5.4.2、§8.4 規約 1）。降格でなければ空。

    ``! DEGRADED: confidence=<水準> engine=<engine_used> lang=<detected_language>`` と
    ``! Warning: <accuracy_warning>``。``lang=`` の値は空白を除く（``unknown (.cob)`` → ``unknown(.cob)``）。
    """
    if not meta.is_degraded:
        return []
    lines = [
        f"! DEGRADED: confidence={_level(meta.confidence_level)} "
        f"engine={_no_space(meta.engine_used)} lang={_no_space(meta.detected_language)}"
    ]
    if meta.accuracy_warning:
        lines.append(f"! Warning: {_one_line(meta.accuracy_warning)}")
    return lines


def candidate_line(candidate: Mapping[str, Any]) -> str:
    """``E_TARGET_AMBIGUOUS`` の候補 1 件: ``! candidate: <urn>  <signature>  @<path>:<line>``。"""
    parts = [f"! candidate: {_one_line(candidate.get('urn', ''))}"]
    signature = _one_line(candidate.get("signature") or "")
    if signature:
        parts.append(signature)
    path = _one_line(candidate.get("path") or "")
    if path:
        line = candidate.get("line")
        parts.append(f"@{path}:{line}" if line is not None else f"@{path}")
    return "  ".join(parts)


# --- 本文の行種 -------------------------------------------------------------------------------


def kv_line(key: str, value: object) -> str:
    """``kv-line``: ``<key>: <value>``。ツール固有の見出し行（``target:`` ・``rows:`` など）。"""
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"kv-line の key が不正: {key!r}")
    return f"{key}: {_one_line(value)}"


def note(text: object) -> str:
    """``note``: ``- <text>``。"""
    return f"- {_one_line(text)}"


def file_block_header(
    path: str,
    *,
    lines: int,
    cells: int | None = None,
    skeleton_lines: int | None = None,
    sha12: str | None = None,
) -> str:
    """``file-block`` の見出し: ``=== <path> (<N> lines[, <M> cells][, skeleton: <K> lines][, sha=<12hex>]) ===``。

    ``sha12`` は 16 進 12 桁。それより長い 16 進（SHA-256 全体など）は先頭 12 桁に切って小文字にする。
    """
    inner = [f"{lines} lines"]
    if cells is not None:
        inner.append(f"{cells} cells")
    if skeleton_lines is not None:
        inner.append(f"skeleton: {skeleton_lines} lines")
    if sha12 is not None:
        if not _SHA_RE.fullmatch(sha12):
            raise ValueError(f"sha12 は 16 進 12 桁以上: {sha12!r}")
        inner.append(f"sha={sha12[:12].lower()}")
    return f"=== {_one_line(path)} ({', '.join(inner)}) ==="


def import_line(items: Iterable[str]) -> str:
    """``import-line``: ``imports: [a, b, c]``。"""
    return f"imports: [{', '.join(_one_line(i) for i in items)}]"


def symbol_line(
    kind: Kind | str,
    name: str,
    *,
    indent: int = 0,
    modifiers: Iterable[str] = (),
    sig: str | None = None,
    ret: str | None = None,
    start_line: int,
    end_line: int | None = None,
    fold_lines: int | None = None,
) -> str:
    """``symbol-block`` の見出し行: ``[Kind] [mods ]name[(sig)[ -> ret]] @L<a>[-<b>][ { ...N lines omitted }]``。

    * ``indent`` は入れ子の段数（1 段 = 空白 2 つ）。
    * ``sig`` は ``SymbolRecord.signature`` と同じ形（``(params)`` または ``(params) -> ret``）を渡す。
      ``(`` で始まらなければ丸括弧で包む。``ret`` は ``sig`` に ``" -> "`` が無いときだけ ``" -> ret"`` で添える。
    * ``end_line`` が ``None`` または ``start_line`` と同じなら ``@L<n>``（仕様書 §5.10）。
    * ``fold_lines`` は畳んだ本体の行数（``{ ...N lines omitted }``）。
    """
    kind_text = _word("kind", kind, KINDS)
    mods = [_word("modifier", m, MODIFIERS) for m in modifiers]
    _pos("start_line", start_line)
    if end_line is not None and end_line < start_line:
        raise ValueError(f"end_line < start_line: {end_line} < {start_line}")
    head = f"{_ind(indent)}[{kind_text}] " + "".join(f"{m} " for m in mods) + _one_line(name)
    if sig is not None:
        inline = _one_line(sig)
        if not inline.startswith("("):
            inline = f"({inline})"
        if ret and " -> " not in inline:
            inline += f" -> {_one_line(ret)}"
        head += inline
    elif ret:
        raise ValueError("ret には sig が必要（sig-inline = '(' text ')' [' -> ' text]）")
    rng = f"{start_line}" if end_line is None or end_line == start_line else f"{start_line}-{end_line}"
    line = f"{head} @L{rng}"
    if fold_lines is not None:
        line += f" {{ ...{fold_lines} lines omitted }}"
    return line


def attr_line(key: str, value: object, *, indent: int) -> str:
    """``attr-line``: ``<key>: <value>``。キーは仕様書 §6.1 の attr-key 語彙に限る（違えば ``ValueError``）。

    ``indent`` は **属性を持つ ``symbol_line`` の ``indent``**（段数）。属性行はその 1 段内側（空白 2 つ分深く）に置く。
    ``doc`` と ``when`` には規約 6 の 200 文字省略を適用する。
    """
    if key not in ATTR_KEYS:
        raise ValueError(f"attr-key は {sorted(ATTR_KEYS)} のいずれか: {key!r}")
    prefix = f"{_ind(indent + 1)}{key}: "
    text = _one_line(value)
    if key in ("doc", "when"):
        text = _fit(prefix, text)
    return prefix + text


def ref_line(
    path: str,
    line: int,
    col: int | None = None,
    *,
    use: str | None = None,
    enclosing: str | None = None,
    snippet: str,
    indent: int = 0,
) -> str:
    """``ref-line``: ``<path>:<line>[:<col>]  [[use] ][in <enclosing>  ]<snippet>``。

    ``line`` ・``col`` は 1 始まり（列は Unicode コードポイント単位）。``use`` は 7 値のいずれか。
    ``snippet`` には規約 6 の 200 文字省略を適用する。
    """
    prefix = f"{_ind(indent)}{_one_line(path)}:{_pos('line', line)}"
    if col is not None:
        prefix += f":{_pos('col', col)}"
    prefix += "  "
    if use is not None:
        prefix += f"[{_word('use', use, USES)}] "
    if enclosing:
        prefix += f"in {_one_line(enclosing)}  "
    text = _one_line(snippet)
    return prefix + _fit(prefix, text)


def diag_line(
    sev: str,
    path: str,
    line: int,
    col: int | None = None,
    *,
    source: str | None = None,
    code: str | None = None,
    message: str,
    fix: str | None = None,
) -> str:
    """``diag-line``: ``<sev> <path>:<line>[:<col>] [[<source>:<code>] ]<message>[  fix=<action-id>]``。

    ``source`` と ``code`` は両方指定するか、どちらも省く。``sev`` は ``E`` ・``W`` ・``I`` ・``H``。
    """
    out = f"{_word('sev', sev, SEVERITIES)} {_one_line(path)}:{_pos('line', line)}"
    if col is not None:
        out += f":{_pos('col', col)}"
    out += " "
    if (source is None) != (code is None):
        raise ValueError("source と code は両方指定するか、どちらも省く")
    if source is not None and code is not None:
        if not re.fullmatch(r"[^\s:\]]+", source):
            raise ValueError(f"diag の source に空白・':'・']' は使えない: {source!r}")
        if not re.fullmatch(r"[^\s\]]+", code):
            raise ValueError(f"diag の code に空白・']' は使えない: {code!r}")
        out += f"[{source}:{code}] "
    out += _one_line(message)
    if fix is not None:
        if not fix or re.search(r"\s", fix):
            raise ValueError(f"fix の action-id に空白は使えない: {fix!r}")
        out += f"  fix={fix}"
    return out


def tree_line(
    direction: str,
    name: str,
    path: str,
    line: int,
    *,
    indent: int,
    when: str | None = None,
    conf: Confidence | str | None = None,
    cycle: bool = False,
) -> str:
    """``tree-line``: ``<indent>(<-|->) <name> @<path>:<line>[ (cycle)][  when: <text>][  conf=<level>]``。

    ``(cycle)`` は仕様書 §11 の T-07 の例（``<- retry_order @src/api/routes.py:140 (cycle)``）に合わせ、
    ``:<line>`` の直後に置く。``when`` には規約 6 の 200 文字省略を適用する。
    """
    direction = _word("direction", direction, ("<-", "->"))
    prefix = f"{_ind(indent)}{direction} {_one_line(name)} @{_one_line(path)}:{_pos('line', line)}"
    if cycle:
        prefix += " (cycle)"
    suffix = f"  conf={_level(conf)}" if conf is not None else ""
    if when is None:
        return prefix + suffix
    lead = prefix + "  when: "
    return lead + _fit(lead, _one_line(when), suffix) + suffix


def path_header(n: int, hops: int) -> str:
    """``path-block`` の見出し: ``path <n> (<hops> hops)``。"""
    return f"path {n} ({hops} hops)"


def step_line(
    order: int,
    name: str,
    path: str,
    line: int,
    *,
    var: str | None = None,
    when: str | None = None,
    indent: int = 1,
) -> str:
    """``step-line``: ``<indent><n>. <name> @<path>:<line>[  var=<text>][  when: <text>]``。

    ``indent`` の既定は 1 段（仕様書 §11 T-14 の例）。``when`` には規約 6 の 200 文字省略を適用する。
    """
    out = f"{_ind(indent)}{order}. {_one_line(name)} @{_one_line(path)}:{_pos('line', line)}"
    if var is not None:
        out += f"  var={_one_line(var)}"
    if when is not None:
        lead = out + "  when: "
        out = lead + _fit(lead, _one_line(when))
    return out


def code_block(
    lines: str | Sequence[str],
    *,
    lang: str | None = None,
    start_line: int | None = None,
    with_line_numbers: bool = True,
) -> list[str]:
    """``code-block``: フェンス行 + 本文 + フェンス行 の行の列。

    * 本文はエスケープせずそのまま出す。中に ``` があれば、最長の連続より 1 本長いフェンスを使う（規約 5）。
    * ``with_line_numbers`` が真で ``start_line`` があるとき、各行に ``@L<n>: `` を付ける（``n`` は 1 始まり）。
      ``start_line`` が無ければ付けない。``lines`` の各要素に改行があれば分割する。
    """
    body: list[str] = []
    for item in [lines] if isinstance(lines, str) else lines:
        body.extend(str(item).replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    if lang is not None and not re.fullmatch(r"[^\s`]*", lang):
        raise ValueError(f"lang に空白・バッククォートは使えない: {lang!r}")
    longest = max((len(m) for text in body for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    numbered = with_line_numbers and start_line is not None
    if numbered:
        first = _pos("start_line", start_line or 1)
        body = [f"@L{first + i}: {text}" for i, text in enumerate(body)]
    return [f"{fence}{lang or ''}", *body, fence]


def table(rows: Sequence[Sequence[object]]) -> list[str]:
    """``table``: 1 行 1 組の ``| a | b |``。先頭の行を見出しとして渡す（見出しの区切り行は出さない）。

    セルの ``|`` は ``\\|`` にし、改行は空白に畳む。``None`` は ``NULL``（仕様書 T-20 の例）。
    """
    out: list[str] = []
    for row in rows:
        cells = ["NULL" if c is None else _one_line(c).replace("|", "\\|") for c in row]
        out.append("| " + " | ".join(cells) + " |")
    return out


def trailer_more(cursor: str, remaining: int | None = None) -> str:
    """``# more: cursor=<token>[ remaining≈<N>]``。"""
    if not _CURSOR_RE.fullmatch(cursor):
        raise ValueError(f"cursor は base64url の文字だけ: {cursor!r}")
    return f"# more: cursor={cursor}" + (f" remaining≈{remaining}" if remaining is not None else "")


def trailer_summary(text: object) -> str:
    """``# summary: <text>``。"""
    return f"# summary: {_one_line(text)}"


# --- レンダラ ---------------------------------------------------------------------------------


def _trailer_lines(inp: RenderInput) -> list[str]:
    """``# summary:`` と ``# more:``。続きがあるときは ``budget.trailer_lines`` の出力そのまま。

    パッカーはその関数の出力でトレーラを数えて ``Page.used_tokens_est`` を決めるので、文言を変えない
    （変えると予算の保証が崩れる）。続きが無いのに ``summary`` があるときだけ、要約行を単独で出す。
    """
    page = inp.page
    if page is None:
        return []
    if page.next_cursor:
        return list(trailer_lines(page.summary, page.next_cursor, page.remaining))
    return [trailer_summary(page.summary)] if page.summary else []


def _record_lines(inp: RenderInput) -> list[str]:
    if inp.page is None:
        return []
    return [line for rec in inp.page.records for line in rec.lines]


def render(inp: RenderInput) -> Rendered:
    """compact 形式に描画する（仕様書 §5.4.2。設計書 §7.2.4）。

    成功: ヘッダ → 降格の警告行 → ``warnings`` の ``! `` 行 → ``head_kv`` の kv 行 → レコードの ``lines`` →
    ``# summary:`` → ``# more:``。警告は ``inp.warnings`` と ``inp.page.warnings`` を重複なしで併合する。
    失敗: ``# <tool> error <CODE>`` → ``! <message>`` → ``E_TARGET_AMBIGUOUS`` なら候補（1 行 1 件）→ 警告。
    ``out`` と ``page`` が一緒に渡されたとき（``E_TIMEOUT`` の部分結果）は、続けてレコードとトレーラも出す。
    """
    call = inp.call
    warnings = [line for w in collect_warnings(inp) for line in banner(w)]
    if inp.error is not None:
        lines = [error_header_line(call.tool, inp.error.code), *banner(inp.error.message)]
        lines += [candidate_line(c) for c in inp.error.candidates]
        lines += warnings
        if inp.out is not None and inp.page is not None:
            lines += _record_lines(inp) + _trailer_lines(inp)
        return Rendered(text="\n".join(lines) + "\n", is_error=True)
    used = inp.page.used_tokens_est if inp.page is not None else 0
    lines = [
        header_line(
            call.tool,
            conf=inp.meta.confidence_level,
            lsp=inp.meta.lsp_status,
            rev=call.revision,
            ws=call.workspace_id,
            used_tokens=used,
            max_tokens=call.max_tokens,
            lsp_progress=inp.meta.lsp_progress,
        ),
        *banner_lines(inp.meta),
        *warnings,
    ]
    if inp.out is not None:
        lines += [kv_line(k, v) for k, v in inp.out.head_kv]
    lines += _record_lines(inp) + _trailer_lines(inp)
    return Rendered(text="\n".join(lines) + "\n")
