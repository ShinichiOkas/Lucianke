"""言語判定・ティア・秘密ファイル・テストパスの判定（設計書 §7.3.3、§9.4、§9.6、仕様書 §8.1、§13.5）。

判定の順序（``detect``）:

1. 名前が秘密らしい（仕様書 §13.5）→ ``language_id="secret"``、Tier 4、``is_secret=True``。
2. 先頭 8 KB に NUL がある、または UTF-8 として読めない → ``"binary"``、Tier 4、``is_binary=True``。
3. 拡張子と特別なファイル名（``Dockerfile`` など）。YAML / JSON は内容も見て OpenAPI を拾う。
4. シバン（``#!/usr/bin/env python3`` など）。
5. モードライン（``-*- mode: python -*-``、``vim: ft=python``）。
6. どれでも決まらなければ ``"unknown"``、Tier 4。

``.gitattributes`` の ``linguist-language``（仕様書 §8.1 の MAY）は、ワークスペースを知らない純粋関数であるここでは読まない。
読む場合は呼び出し側が ``detect`` の結果を上書きする。

外に出さない型（``Workspace`` など）には依存しない。``RelPath`` は封じ込め済みの文字列として受け取るだけである。
"""

from __future__ import annotations

import codecs
import fnmatch
import re
from collections.abc import Iterable
from dataclasses import dataclass

from lucianke.core.model import RelPath, Tier

__all__ = [
    "HEAD_BYTES",
    "LANGUAGE_TIERS",
    "SECRET_PATTERNS",
    "Detected",
    "detect",
    "is_secret_name",
    "is_test",
    "tier_of",
]

HEAD_BYTES = 8 * 1024
"""``detect`` に渡す先頭バイト列の長さ（設計書 §7.3.3）。これより長い入力は先頭だけを見る。"""

# --- 言語表 -----------------------------------------------------------------------------

_T1, _T2, _T3, _T4 = Tier.T1, Tier.T2, Tier.T3, Tier.T4

# 言語 ID → (ティア, 拡張子)。ティアは仕様書 §8.1。拡張子は小文字・ドット付き。
_LANGUAGES: dict[str, tuple[Tier, tuple[str, ...]]] = {
    # Tier 1: 完全決定論解析
    "python": (_T1, (".py", ".pyi", ".pyw")),
    "typescript": (_T1, (".ts", ".mts", ".cts")),
    "tsx": (_T1, (".tsx",)),
    "javascript": (_T1, (".js", ".mjs", ".cjs", ".jsx")),
    "rust": (_T1, (".rs",)),
    "go": (_T1, (".go",)),
    "c": (_T1, (".c", ".h")),
    "cpp": (_T1, (".cc", ".cpp", ".cxx", ".c++", ".hh", ".hpp", ".hxx", ".h++", ".ipp", ".tpp")),
    "c_sharp": (_T1, (".cs",)),
    "kotlin": (_T1, (".kt", ".kts")),
    "swift": (_T1, (".swift",)),
    # Tier 2: 言語境界・構造化仕様・クエリ（openapi は拡張子ではなく名前・内容で決まる）
    "cython": (_T2, (".pyx", ".pxd", ".pxi")),
    "ipynb": (_T2, (".ipynb",)),
    "sql": (_T2, (".sql",)),
    "dot": (_T2, (".dot", ".gv")),
    "mermaid": (_T2, (".mmd", ".mermaid")),
    "openapi": (_T2, ()),
    "proto": (_T2, (".proto",)),
    "graphql": (_T2, (".graphql", ".gql", ".graphqls")),
    # Tier 3: 階層データ・マークアップ・テンプレート
    "xml": (_T3, (".xml", ".xsd", ".xsl", ".xslt", ".wsdl", ".svg")),
    "json": (_T3, (".json", ".jsonc", ".json5")),
    "jsonl": (_T3, (".jsonl", ".ndjson")),
    "yaml": (_T3, (".yaml", ".yml")),
    "toml": (_T3, (".toml",)),
    "jinja2": (_T3, (".j2", ".jinja", ".jinja2")),
    "markdown": (_T3, (".md", ".markdown", ".mkd", ".mdx")),
    "rst": (_T3, (".rst", ".rest")),
    # Tier 4: 既知の名前を付けるが文法エンジンは無い（仕様書 §8.1 の「レガシー言語」ほか）
    "cobol": (_T4, (".cob", ".cbl", ".cobol", ".cpy")),
    "fortran": (_T4, (".f", ".for", ".ftn", ".f77", ".f90", ".f95", ".f03", ".f08")),
    "pascal": (_T4, (".pas", ".pp", ".dpr")),
    "ada": (_T4, (".adb", ".ads", ".ada")),
    "java": (_T4, (".java",)),
    "ruby": (_T4, (".rb",)),
    "php": (_T4, (".php",)),
    "perl": (_T4, (".pl", ".pm")),
    "lua": (_T4, (".lua",)),
    "shell": (_T4, (".sh", ".bash", ".zsh", ".ksh", ".fish")),
    "batch": (_T4, (".bat", ".cmd")),
    "powershell": (_T4, (".ps1", ".psm1")),
    "makefile": (_T4, (".mk",)),
    "dockerfile": (_T4, ()),
}

LANGUAGE_TIERS: dict[str, Tier] = {lang: tier for lang, (tier, _) in _LANGUAGES.items()}
"""言語 ID → ティア。``binary`` / ``secret`` / ``unknown`` も Tier 4 として含む。"""
LANGUAGE_TIERS.update({"binary": _T4, "secret": _T4, "unknown": _T4})

_EXT_TO_LANGUAGE: dict[str, str] = {}
for _lang, (_, _exts) in _LANGUAGES.items():
    for _ext in _exts:
        if _ext in _EXT_TO_LANGUAGE:
            raise RuntimeError(f"duplicate extension {_ext!r}: {_EXT_TO_LANGUAGE[_ext]} / {_lang}")
        _EXT_TO_LANGUAGE[_ext] = _lang

# 拡張子を持たない（または拡張子では決まらない）既知のファイル名（小文字）。
_NAME_TO_LANGUAGE: dict[str, str] = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "makefile": "makefile",
    "gnumakefile": "makefile",
    "cargo.lock": "toml",
    "poetry.lock": "toml",
    "uv.lock": "toml",
    "pipfile": "toml",
}


def tier_of(language_id: str) -> Tier:
    """言語 ID のティア。未知の ID は Tier 4。"""
    return LANGUAGE_TIERS.get(language_id, _T4)


@dataclass(frozen=True, slots=True)
class Detected:
    """``detect`` の結果。``files.language`` / ``files.tier`` に記録する値と、本文を読んでよいか。"""

    language_id: str
    tier: Tier
    is_secret: bool = False
    is_binary: bool = False

    @property
    def index_content(self) -> bool:
        """本文を索引・読み取りしてよいか（設計書 §7.3.3 の ``index_content``）。秘密・バイナリは False。"""
        return not (self.is_secret or self.is_binary)


# --- 秘密ファイル -----------------------------------------------------------------------

SECRET_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "*.p12",
    "*credentials*.json",
)
"""名前が秘密らしいファイルのパターン（仕様書 §13.5）。ファイル名（ディレクトリを除く）に大文字小文字を無視して当てる。"""


def is_secret_name(path: str) -> bool:
    """``path``（相対パスでも名前だけでもよい）の末尾の名前が秘密らしいか。"""
    name = _basename(path).casefold()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_PATTERNS)


# --- テストパス -------------------------------------------------------------------------

_TEST_DIRS = frozenset({"test", "tests"})
_TEST_NAME_GLOBS = ("*_test.*", "*.spec.*", "*.test.*")


def is_test(rel: RelPath | str, extra_globs: Iterable[str] = ()) -> bool:
    """既定のテストパス規則（仕様書 §9.2 の ``files.is_test``）。

    ``test/`` / ``tests/`` ディレクトリの配下、または名前が ``*_test.*``・``*.spec.*``・``*.test.*``・
    ``test_*.py``。大文字小文字は区別しない。``extra_globs`` は言語サポートが足す追加パターン
    （``LanguageSupport.test_path_globs``、設計書 §7.3.3）で、``/`` を含めばパス全体、含まなければ名前に当てる（``fnmatch`` なので ``*`` は ``/`` を越える）。
    """
    parts = [p.casefold() for p in str(rel).split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    if any(p in _TEST_DIRS for p in parts[:-1]):
        return True
    if any(fnmatch.fnmatchcase(name, g) for g in _TEST_NAME_GLOBS):
        return True
    if fnmatch.fnmatchcase(name, "test_*.py"):
        return True
    full = "/".join(parts)
    for glob in extra_globs:
        g = glob.casefold()
        if fnmatch.fnmatchcase(full if "/" in g else name, g):
            return True
    return False


# --- 判定本体 ---------------------------------------------------------------------------


def detect(rel: RelPath, head: bytes) -> Detected:
    """``rel`` と先頭バイト列（最大 ``HEAD_BYTES``）から言語・ティアを判定する。例外は送出しない。"""
    head = head[:HEAD_BYTES]
    is_binary = _looks_binary(head)
    if is_secret_name(rel):
        # 名前が決め手。バイナリの秘密（.p12 など）も含めて本文は読まない。
        return Detected("secret", _T4, is_secret=True, is_binary=is_binary)
    if is_binary:
        return Detected("binary", _T4, is_binary=True)
    language = _detect_language(rel, head)
    return Detected(language, tier_of(language))


def _looks_binary(head: bytes) -> bool:
    """NUL を含む、または UTF-8 として読めない（設計書 §7.3.3 手順 1、§9.6）。"""
    if b"\x00" in head:
        return True
    # 先頭 8 KB で切れた末尾のマルチバイト文字は誤判定しない（全体が 8 KB に満たなければ末尾も検査する）。
    final = len(head) < HEAD_BYTES
    try:
        codecs.getincrementaldecoder("utf-8")(errors="strict").decode(head, final=final)
    except UnicodeDecodeError:
        return True
    return False


def _detect_language(rel: str, head: bytes) -> str:
    name = _basename(rel)
    lowered = name.casefold()
    text = head.decode("utf-8", errors="replace")
    if text.startswith("﻿"):
        text = text[1:]

    ext = _extension(lowered)
    language = _NAME_TO_LANGUAGE.get(lowered) or _EXT_TO_LANGUAGE.get(ext)
    if language in ("yaml", "json"):
        language = _openapi_or(language, lowered, ext, text)
    if language:
        return language

    return _from_shebang(text) or _from_modeline(text, complete=len(head) < HEAD_BYTES) or "unknown"


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _extension(lowered_name: str) -> str:
    """最後のドット以降（``.d.ts`` → ``.ts``、``.env`` のような先頭ドットだけの名前は拡張子なし）。"""
    idx = lowered_name.rfind(".")
    return lowered_name[idx:] if idx > 0 else ""


# OpenAPI: 名前（openapi.yaml / swagger.json / foo.openapi.yaml）または最上位キーで判定する。迷ったら元の yaml / json。
_OPENAPI_STEMS = frozenset({"openapi", "swagger"})
_OPENAPI_YAML = re.compile(r"""^(?:openapi\s*:\s*["']?3\.\d|swagger\s*:\s*["']?2\.0)""", re.MULTILINE)
_OPENAPI_JSON = re.compile(r"""(?:^|[{,])\s*"(?:openapi"\s*:\s*"3\.\d|swagger"\s*:\s*"2\.0")""", re.MULTILINE)


def _openapi_or(language: str, lowered: str, ext: str, text: str) -> str:
    stem = lowered[: -len(ext)] if ext else lowered
    if stem in _OPENAPI_STEMS or stem.endswith((".openapi", ".swagger")):
        return "openapi"
    # JSON の最上位キーは先頭付近にある。深い入れ子の同名キーを拾わないよう、見る範囲を絞る。
    found = _OPENAPI_YAML.search(text) if language == "yaml" else _OPENAPI_JSON.search(text[:2048])
    return "openapi" if found else language


# --- シバン ------------------------------------------------------------------------------

_PYTHON_INTERPRETER = re.compile(r"^(?:python|pypy)\d*(?:\.\d+)*$")
_SHEBANG_INTERPRETERS: dict[str, str] = {
    "node": "javascript",
    "nodejs": "javascript",
    "bun": "javascript",
    "ts-node": "typescript",
    "tsx": "typescript",
    "deno": "typescript",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "dash": "shell",
    "ash": "shell",
    "ksh": "shell",
    "fish": "shell",
    "perl": "perl",
    "ruby": "ruby",
    "php": "php",
    "lua": "lua",
    "pwsh": "powershell",
}


def _from_shebang(text: str) -> str | None:
    if not text.startswith("#!"):
        return None
    tokens = text[2:].split("\n", 1)[0].strip().split()
    if not tokens:
        return None
    program = _basename(tokens[0].replace("\\", "/")).casefold()
    if program == "env":
        # env のオプション（-S、-i、NAME=value）を飛ばして最初のコマンドを取る。
        rest = [t for t in tokens[1:] if not t.startswith("-") and "=" not in t]
        if not rest:
            return None
        program = _basename(rest[0]).casefold()
    if _PYTHON_INTERPRETER.match(program):
        return "python"
    return _SHEBANG_INTERPRETERS.get(program)


# --- モードライン ------------------------------------------------------------------------

_EMACS_MODELINE = re.compile(r"-\*-(.*?)-\*-")
_EMACS_MODE_KEY = re.compile(r"(?:^|;)\s*mode\s*:\s*([\w+#.-]+)", re.IGNORECASE)
_EMACS_BARE_MODE = re.compile(r"^\s*([\w+#.-]+)\s*$")
_VIM_MODELINE = re.compile(r"(?:^|\s)(?:vim?|ex)\s*:.*?\b(?:ft|filetype)\s*=\s*([\w+#.-]+)", re.IGNORECASE)

# エディタ側の呼び名 → 言語 ID。表に無い名前は判定に使わない（unknown に落とす）。
_MODE_ALIASES: dict[str, str] = {
    "python": "python",
    "python3": "python",
    "pyrex": "cython",
    "cython": "cython",
    "typescript": "typescript",
    "ts": "typescript",
    "typescriptreact": "tsx",
    "tsx": "tsx",
    "javascript": "javascript",
    "js": "javascript",
    "js2": "javascript",
    "javascriptreact": "javascript",
    "jsx": "javascript",
    "rust": "rust",
    "go": "go",
    "c": "c",
    "c++": "cpp",
    "cpp": "cpp",
    "c#": "c_sharp",
    "csharp": "c_sharp",
    "cs": "c_sharp",
    "kotlin": "kotlin",
    "swift": "swift",
    "sql": "sql",
    "dot": "dot",
    "graphviz-dot": "dot",
    "mermaid": "mermaid",
    "proto": "proto",
    "protobuf": "proto",
    "graphql": "graphql",
    "xml": "xml",
    "nxml": "xml",
    "json": "json",
    "yaml": "yaml",
    "toml": "toml",
    "jinja": "jinja2",
    "jinja2": "jinja2",
    "markdown": "markdown",
    "md": "markdown",
    "rst": "rst",
    "cobol": "cobol",
    "fortran": "fortran",
    "pascal": "pascal",
    "ada": "ada",
    "java": "java",
    "ruby": "ruby",
    "php": "php",
    "perl": "perl",
    "lua": "lua",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "shell-script": "shell",
    "makefile": "makefile",
    "dockerfile": "dockerfile",
}


def _from_modeline(text: str, *, complete: bool) -> str | None:
    """先頭 5 行（全体が読めているときは末尾 5 行も）からモードラインを探す。Emacs 形式を先に見る。"""
    lines = text.splitlines()
    candidates = lines[:5]
    if complete and len(lines) > 5:
        candidates = candidates + lines[-5:]
    for line in candidates:
        found = _emacs_mode(line) or _vim_filetype(line)
        if found:
            language = _MODE_ALIASES.get(found.casefold().removesuffix("-mode"))
            if language:
                return language
    return None


def _emacs_mode(line: str) -> str | None:
    m = _EMACS_MODELINE.search(line)
    if not m:
        return None
    body = m.group(1)
    keyed = _EMACS_MODE_KEY.search(body)
    if keyed:
        return keyed.group(1)
    bare = _EMACS_BARE_MODE.match(body)  # `-*- python -*-`
    return bare.group(1) if bare else None


def _vim_filetype(line: str) -> str | None:
    m = _VIM_MODELINE.search(line)
    return m.group(1) if m else None
