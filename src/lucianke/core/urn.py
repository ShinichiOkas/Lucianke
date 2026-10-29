"""URN の構文解析・整形・緩和（仕様書 第 7 章）。

::

    urn = "lk://" workspace-id "/" path [ "#" selector ]

セレクタは 5 種: ``code-sel``（Tier 1〜2。``qualified[/arity][~sighash]``）、``data-sel``
（``xpath:`` ``jsonpath:`` ``jq:`` ``toml:`` ``yaml:``）、``nb-sel``（``cell:N[/qualified]``）、
``tag-sel``（``ctags:NAME@Ln``）、``lex-sel``（``lex:NAME@Ln``）。

文字列の表現（正準形）:

* ``path`` は ``/`` で区切った各セグメントをパーセントエンコードする。エンコードしない文字は
  RFC 3986 の unreserved（``A-Za-z0-9-._~``）と ``!$&'()*+,;=:@``、および ASCII 外の印字可能文字
  （日本語のファイル名などは読みやすさとトークン数のためそのまま置く）。それ以外（``#`` ``%`` 空白
  ``?`` ``[`` ``]`` バックスラッシュ、制御文字など）は UTF-8 の ``%XX``（16 進は大文字）にする。
* ``data-sel`` の ``expr`` も同じ集合でエンコードする。``/`` は ``arity`` の区切りと紛れないよう
  常にエンコードする（仕様書 §7.2 の例 ``xpath:%2F%2Ftestcase%5Bfailure%5D``）。
* ``qualified`` と ``ctags:`` / ``lex:`` の名前は、``A-Za-z0-9_$-`` と ASCII 外の印字可能文字だけをそのまま置く。
  ``qualified`` のスコープ区切り ``.`` は生のまま。``/`` ``~`` ``@`` ``#`` ``%`` はエンコードする
  （``/`` は arity、``~`` は sighash、``@`` は行番号の区切りに使うため。C++ のデストラクタ ``~Foo`` は ``%7EFoo``）。
* ``parse`` は生の空白・制御文字・``#`` の二重出現・不正なパーセント列・不正な UTF-8 を ``ValueError`` にする。
  それ以外の文字は ``ident`` として寛容に受け入れる（``/`` と ``~`` だけは区切りとして解釈する）。

不正な入力は ``ValueError``。ツール層が ``E_INVALID_ARGUMENT`` に包む。
"""

from __future__ import annotations

import hashlib
import re
import string
import unicodedata
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote

SCHEME = "lk://"
EXTERNAL_WORKSPACE_ID = (
    "_ext"  # 仕様書 §9.3: ワークスペース外のシンボルは lk://_ext/<pkg>[@<ver>]#<qualified>
)

DataKind = Literal["xpath", "jsonpath", "jq", "toml", "yaml"]
DATA_KINDS: tuple[DataKind, ...] = ("xpath", "jsonpath", "jq", "toml", "yaml")

_WORKSPACE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_SIGHASH_RE = re.compile(r"[0-9a-f]{8}")
_CODE_SEL_RE = re.compile(r"([^/~]+)(?:/([0-9]+))?(?:~([0-9A-Fa-f]{8}))?")
_CELL_SEL_RE = re.compile(r"cell:([0-9]+)(?:/(.+))?")
_TAG_SEL_RE = re.compile(r"(ctags|lex):(.+)@L([0-9]+)")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")

_UNRESERVED = string.ascii_letters + string.digits + "-._~"
_PCHAR_SAFE = frozenset(_UNRESERVED + "!$&'()*+,;=:@")
_IDENT_SAFE = frozenset(string.ascii_letters + string.digits + "_$-")
_PCHAR_NEEDS_QUOTE_RE = re.compile(r"[^A-Za-z0-9\-._~!$&'()*+,;=:@]")
_IDENT_NEEDS_QUOTE_RE = re.compile(r"[^A-Za-z0-9_$\-]")


# --- パーセントエンコード ---------------------------------------------------------------


def _quote(text: str, safe: frozenset[str], needs_quote: re.Pattern[str]) -> str:
    if text.isascii() and not needs_quote.search(text):
        return text
    out: list[str] = []
    for ch in text:
        if ch in safe:
            out.append(ch)
        elif ord(ch) >= 0x80 and unicodedata.category(ch)[0] not in "CZ":
            out.append(ch)
        else:
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
    return "".join(out)


def _unquote(text: str, what: str) -> str:
    if _BAD_PERCENT_RE.search(text):
        raise ValueError(f"invalid percent-encoding in {what}: {text!r}")
    return unquote(text, encoding="utf-8", errors="strict")


def _quote_path(path: str) -> str:
    return "/".join(_quote(seg, _PCHAR_SAFE, _PCHAR_NEEDS_QUOTE_RE) for seg in path.split("/"))


def _quote_expr(expr: str) -> str:
    return _quote(expr, _PCHAR_SAFE, _PCHAR_NEEDS_QUOTE_RE)


def _quote_qualified(qualified: str) -> str:
    return ".".join(_quote(seg, _IDENT_SAFE, _IDENT_NEEDS_QUOTE_RE) for seg in qualified.split("."))


def _quote_name(name: str) -> str:
    return _quote(name, _IDENT_SAFE, _IDENT_NEEDS_QUOTE_RE)


# --- セレクタ ---------------------------------------------------------------------------


def _check_qualified(qualified: str) -> None:
    if not qualified or any(not seg for seg in qualified.split(".")):
        raise ValueError(f"invalid qualified name: {qualified!r}")


@dataclass(frozen=True, slots=True)
class CodeSel:
    """``qualified[/arity][~sighash]``（Tier 1〜2）。

    ``arity`` と ``sighash`` はオーバーロードや同名の再定義があるときだけ付ける（仕様書 §7.2。常時付与は MUST NOT）。
    ``sighash`` は小文字 16 進 8 桁（``sighash()`` の出力）。
    """

    qualified: str
    arity: int | None = None
    sighash: str | None = None

    def __post_init__(self) -> None:
        _check_qualified(self.qualified)
        if self.arity is not None and self.arity < 0:
            raise ValueError(f"arity must be >= 0: {self.arity}")
        if self.sighash is not None and not _SIGHASH_RE.fullmatch(self.sighash):
            raise ValueError(f"sighash must be 8 lowercase hex digits: {self.sighash!r}")


@dataclass(frozen=True, slots=True)
class DataSel:
    """``xpath:`` ``jsonpath:`` ``jq:`` ``toml:`` ``yaml:`` + 式（Tier 3）。``expr`` はデコード済みの式。"""

    kind: DataKind
    expr: str

    def __post_init__(self) -> None:
        if self.kind not in DATA_KINDS:
            raise ValueError(f"unknown data selector kind: {self.kind!r}")
        if not self.expr:
            raise ValueError("data selector expression is empty")


@dataclass(frozen=True, slots=True)
class CellSel:
    """``cell:N[/qualified]``（``.ipynb``）。``n`` はセル番号（0 始まりか 1 始まりかは notebook アダプタが決める）。"""

    n: int
    qualified: str | None = None

    def __post_init__(self) -> None:
        if self.n < 0:
            raise ValueError(f"cell index must be >= 0: {self.n}")
        if self.qualified is not None:
            _check_qualified(self.qualified)


@dataclass(frozen=True, slots=True)
class TagSel:
    """``ctags:NAME@Ln``（Tier 4 heuristic）。``line`` は 1 始まり。編集で変わりうる（仕様書 §7.3）。"""

    name: str
    line: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tag name is empty")
        if self.line < 1:
            raise ValueError(f"line must be >= 1: {self.line}")


@dataclass(frozen=True, slots=True)
class LexSel:
    """``lex:NAME@Ln``（Tier 4 lexical）。``line`` は 1 始まり。編集で変わりうる（仕様書 §7.3）。"""

    name: str
    line: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("lex name is empty")
        if self.line < 1:
            raise ValueError(f"line must be >= 1: {self.line}")


Selector = CodeSel | DataSel | CellSel | TagSel | LexSel


def _format_selector(sel: Selector) -> str:
    match sel:
        case CodeSel(qualified, arity, sighash):
            out = _quote_qualified(qualified)
            if arity is not None:
                out += f"/{arity}"
            if sighash is not None:
                out += f"~{sighash}"
            return out
        case DataSel(kind, expr):
            return f"{kind}:{_quote_expr(expr)}"
        case CellSel(n, qualified):
            return f"cell:{n}" if qualified is None else f"cell:{n}/{_quote_qualified(qualified)}"
        case TagSel(name, line):
            return f"ctags:{_quote_name(name)}@L{line}"
        case LexSel(name, line):
            return f"lex:{_quote_name(name)}@L{line}"


def _parse_qualified(text: str, what: str) -> str:
    qualified = _unquote(text, what)
    _check_qualified(qualified)
    return qualified


def _parse_selector(sel: str) -> Selector:
    if not sel:
        raise ValueError("empty selector")
    if "#" in sel:
        raise ValueError("'#' in a selector must be percent-encoded (%23)")
    for kind in DATA_KINDS:
        if sel.startswith(kind + ":"):
            return DataSel(kind, _unquote(sel[len(kind) + 1 :], f"{kind} expression"))
    if sel.startswith("cell:"):
        m = _CELL_SEL_RE.fullmatch(sel)
        if m is None:
            raise ValueError(f"invalid cell selector: {sel!r}")
        qualified = _parse_qualified(m[2], "cell selector") if m[2] else None
        return CellSel(int(m[1]), qualified)
    if sel.startswith(("ctags:", "lex:")):
        m = _TAG_SEL_RE.fullmatch(sel)
        if m is None:
            raise ValueError(f"invalid ctags/lex selector (expected NAME@L<line>): {sel!r}")
        name = _unquote(m[2], f"{m[1]} name")
        line = int(m[3])
        return TagSel(name, line) if m[1] == "ctags" else LexSel(name, line)
    m = _CODE_SEL_RE.fullmatch(sel)
    if m is None:
        raise ValueError(f"invalid code selector (expected qualified[/arity][~sighash]): {sel!r}")
    return CodeSel(
        _parse_qualified(m[1], "qualified name"),
        int(m[2]) if m[2] is not None else None,
        m[3].lower() if m[3] is not None else None,
    )


# --- Urn --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Urn:
    """シンボルの安定識別子。``path`` は **デコード済み** のワークスペース相対パス（``/`` 区切り）。

    ``selector`` が ``None`` のときはファイルそのものを指す。ワークスペース外のシンボルは
    ``workspace_id == "_ext"``、``path == "<pkg>[@<ver>]"``（``Urn.external()``）。
    """

    workspace_id: str
    path: str
    selector: Selector | None = None

    def __post_init__(self) -> None:
        if not _WORKSPACE_ID_RE.fullmatch(self.workspace_id):
            raise ValueError(f"invalid workspace id: {self.workspace_id!r}")
        if not self.path:
            raise ValueError("path is empty")
        for seg in self.path.split("/"):
            if seg in ("", ".", ".."):
                raise ValueError(f"invalid path segment {seg!r} in {self.path!r}")
            if any(unicodedata.category(c) == "Cc" for c in seg):
                raise ValueError(f"control character in path: {self.path!r}")

    @classmethod
    def external(cls, package: str, version: str | None = None, qualified: str | None = None) -> Urn:
        """外部シンボル ``lk://_ext/<pkg>[@<ver>]#<qualified>``（仕様書 §9.3）。"""
        path = package if version is None else f"{package}@{version}"
        return cls(EXTERNAL_WORKSPACE_ID, path, CodeSel(qualified) if qualified is not None else None)

    @property
    def is_external(self) -> bool:
        return self.workspace_id == EXTERNAL_WORKSPACE_ID

    def external_parts(self) -> tuple[str, str | None]:
        """外部 URN の ``(package, version)``。``@types/node@1.0`` のような先頭の ``@`` はパッケージ名の一部。"""
        if not self.is_external:
            raise ValueError("not an external URN")
        head, sep, tail = self.path.rpartition("@")
        if sep and head:
            return head, tail
        return self.path, None

    def with_selector(self, selector: Selector | None) -> Urn:
        return Urn(self.workspace_id, self.path, selector)

    def relax(self) -> Urn:
        return relax(self)

    def format(self) -> str:
        return format(self)

    def __str__(self) -> str:
        return format(self)


def format(urn: Urn) -> str:  # noqa: A001 - 仕様の名前（parse / format）に合わせる
    """``Urn`` を正準形の文字列にする。``parse(format(u)) == u``。"""
    out = f"{SCHEME}{urn.workspace_id}/{_quote_path(urn.path)}"
    if urn.selector is not None:
        out += "#" + _format_selector(urn.selector)
    return out


def parse(text: str) -> Urn:
    """URN 文字列を ``Urn`` にする。不正な入力は ``ValueError``。

    正準形でない入力（エンコードされていない非 ASCII、sighash の大文字など）も受け入れ、
    ``format(parse(x))`` で正準形になる。
    """
    if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"URN must be a string: {text!r}")
    if not text.startswith(SCHEME):
        raise ValueError(f"URN must start with {SCHEME!r}: {text!r}")
    if not text.isprintable() or " " in text:
        raise ValueError(
            f"raw whitespace or control characters are not allowed (percent-encode them): {text!r}"
        )
    workspace_id, slash, tail = text[len(SCHEME) :].partition("/")
    if not slash:
        raise ValueError(f"URN has no path: {text!r}")
    path_part, hash_sep, selector_part = tail.partition("#")
    if not path_part:
        raise ValueError(f"URN has an empty path: {text!r}")
    segments = [_unquote(seg, "path") for seg in path_part.split("/")]
    for seg in segments:
        if "/" in seg:
            raise ValueError(f"'/' inside a path segment (%2F) is not allowed: {text!r}")
    path = "/".join(segments)
    selector = _parse_selector(selector_part) if hash_sep else None
    return Urn(workspace_id, path, selector)


def relax(urn: Urn) -> Urn:
    """``~sighash`` と ``/arity`` を外した形（仕様書 §7.4）。code-sel 以外はそのまま返す。"""
    sel = urn.selector
    if isinstance(sel, CodeSel) and (sel.arity is not None or sel.sighash is not None):
        return urn.with_selector(CodeSel(sel.qualified))
    return urn


# --- シグネチャのハッシュとワークスペース ID ---------------------------------------------

_WS_RE = re.compile(r"\s+")
_AROUND_PUNCT_RE = re.compile(r"\s*([,()\[\]{}])\s*")
_TRAILING_COMMA_RE = re.compile(r",(?=[)\]}])")


def normalize_signature(signature: str) -> str:
    """シグネチャの正規化（``sighash`` の入力）。

    1. 連続する空白（改行・タブを含む）を 1 つのスペースにし、前後を除く。
    2. カンマと括弧（``( ) [ ] { }``）の前後の空白を除く。
    3. 閉じ括弧の直前の末尾カンマを除く（``(a, b,)`` → ``(a,b)``）。

    ``->`` や ``:`` の前後の空白は **正規化しない**（仕様書 §7.2 の規則どおり）。
    """
    s = _WS_RE.sub(" ", signature).strip()
    s = _AROUND_PUNCT_RE.sub(r"\1", s)
    return _TRAILING_COMMA_RE.sub("", s)


def sighash(signature: str, *, start_line: int | None = None) -> str:
    """正規化したシグネチャの SHA-256 先頭 8 桁（小文字 16 進）。

    ``start_line`` を与えると、シグネチャまで同一の再定義を区別するために
    ``正規化シグネチャ + "\\n" + 開始行`` をハッシュする（仕様書 §7.2 の 4 つ目の規則）。
    """
    text = normalize_signature(signature)
    if start_line is not None:
        text += f"\n{start_line}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


_WORKSPACE_ID_BAD_CHARS_RE = re.compile(r"[^A-Za-z0-9_.\-]")


def workspace_id_from_dirname(name: str) -> str:
    """ルートのディレクトリ名から ``workspace-id`` を作る（仕様書 §7.2）。

    英数・``-``・``_``・``.`` 以外の 1 文字を ``_`` 1 文字に置き換える。空文字列は ``"_"`` にする。
    衝突時の ``-2``, ``-3`` の付与は workspace 層の仕事。
    """
    return _WORKSPACE_ID_BAD_CHARS_RE.sub("_", name) or "_"
