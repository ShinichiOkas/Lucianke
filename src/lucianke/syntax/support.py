"""言語の登録表（設計書 §7.4.1、ADR-9）。

``LanguageSupport`` / ``FormatAdapter`` の Protocol と、具体の登録用データ ``LanguageSpec``、登録表 ``SUPPORT`` を置く。
ツールはこの表を通してだけ言語を知る。新しい言語は ``_SPECS`` に 1 行足し、``.scm`` クエリを ``queries`` に載せるだけで済む。

登録は import 時に一度だけ行う。各言語の ``grammar_loader()`` を試し、読み込めない言語（extras が未導入、ABI が本体の範囲外、
その他の例外）は ``SUPPORT`` に入れず、理由を ``unavailable_languages()`` で返す。呼び手（``status`` ツール、起動ログ）が
警告に使う。M1 の必須言語（python / typescript / tsx / javascript / json / yaml / toml / xml / markdown）が欠けるのは
導入の不備なので、呼び手が ``unavailable_languages()`` を見て報告する。

拡張子の割り当て（``language_for_extension``）は 1 拡張子 1 言語。``.h`` は ``c`` に、``.js`` と ``.jsx`` は ``javascript`` に、
``.ts`` 系は ``typescript``（JSX を含む ``.tsx`` だけ ``tsx``）に割り当てる。内容による言語の再判定は呼び手の仕事。
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import tree_sitter

from lucianke.core.model import Kind, Range, Tier
from lucianke.core.textpos import IndentSpec
from lucianke.syntax.types import LanguageQueries, Match, ParsedFile, SkeletonOptions

if TYPE_CHECKING:
    from lucianke.core.records import Record
    from lucianke.core.urn import Selector

__all__ = [
    "ADAPTERS",
    "SUPPORT",
    "FormatAdapter",
    "LanguageSpec",
    "LanguageSupport",
    "build_registry",
    "get_adapter",
    "get_support",
    "language_for_extension",
    "register_adapter",
    "unavailable_languages",
]


# --- Protocol（設計書 §7.4.1） ------------------------------------------------------------


class LanguageSupport(Protocol):
    """Tier 1〜2 のコード言語。``LanguageSpec`` が文法・クエリ・インデント既定などのデータを持ち、
    ``symbol_kind`` / ``definition_range`` / ``is_private`` は symbols・boundaries の役が実装する。
    """

    language_id: str
    tier: Tier
    extensions: tuple[str, ...]
    indent_default: IndentSpec
    test_path_globs: tuple[str, ...]
    lsp_servers: tuple[Any, ...]  # LspServerDef（§7.5.1）。型は lsp の役が定める
    queries: LanguageQueries  # 設計書は ``queries()`` メソッドだが、登録用データクラスの属性にした（遅延コンパイルは使う側で行う）

    def grammar(self) -> tree_sitter.Language: ...

    def symbol_kind(self, node: Any) -> Kind: ...

    def definition_range(self, node: Any, parsed: ParsedFile) -> Range: ...  # §7.4.5

    def is_private(self, name: str, node: Any) -> bool: ...


class FormatAdapter(Protocol):
    """Tier 3〜4、および .ipynb / DOT / Mermaid。骨格・部分取得・置換（M2）。"""

    language_id: str
    tier: Tier
    extensions: tuple[str, ...]

    def skeleton(self, source: bytes, opts: SkeletonOptions) -> list[Record]: ...

    def select(
        self, source: bytes, selector: Selector
    ) -> list[Match]: ...  # xpath: / jsonpath: / jq: / toml: / yaml: / cell:

    def replace(self, source: bytes, selector: Selector, new: bytes) -> bytes: ...  # M2


# --- 具体の登録用データ ------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class LanguageSpec:
    """言語 1 つの登録データ。

    ``queries`` は今は空（``LanguageQueries()``。全項目 None）。クエリは symbols・skeleton の役が足す。
    ``symbol_kind`` / ``definition_range`` / ``is_private`` は ``LanguageSupport`` の約束だが、ここでは実装しない
    （別の役が ``LanguageSpec`` を拡張するか、実装つきのクラスで ``SUPPORT`` を置き換える）。
    """

    language_id: str
    tier: Tier
    extensions: tuple[str, ...]
    grammar_loader: Callable[[], tree_sitter.Language]
    indent_default: IndentSpec
    test_path_globs: tuple[str, ...] = ()
    queries: LanguageQueries = field(default_factory=LanguageQueries)
    lsp_servers: tuple[Any, ...] = ()
    _language: tree_sitter.Language | None = field(default=None, init=False, repr=False)

    def grammar(self) -> tree_sitter.Language:
        """文法。初回に ``grammar_loader()`` を呼び、以後は同じ ``Language`` を返す（共有してよい。S-02）。"""
        lang = self._language
        if lang is None:
            lang = self.grammar_loader()
            object.__setattr__(self, "_language", lang)
        return lang


# --- 言語の読み込み表（S-02: lang_id -> (import 名, 関数の実名)） -----------------------------


def _loader(module: str, func: str) -> Callable[[], tree_sitter.Language]:
    def load() -> tree_sitter.Language:
        return tree_sitter.Language(getattr(importlib.import_module(module), func)())

    load.__qualname__ = f"load[{module}.{func}]"
    return load


def _check_abi(lang: tree_sitter.Language) -> None:
    """本体が読める ABI の範囲（S-02。ABI 14 の文法は ``tree-sitter>=0.26,<0.27`` で読める）。"""
    abi = lang.abi_version
    lo, hi = tree_sitter.MIN_COMPATIBLE_LANGUAGE_VERSION, tree_sitter.LANGUAGE_VERSION
    if not lo <= abi <= hi:
        raise RuntimeError(f"grammar ABI {abi} is outside tree-sitter's supported range {lo}..{hi}")


_SP2, _SP3, _SP4, _TAB = (
    IndentSpec("space", 2),
    IndentSpec("space", 3),
    IndentSpec("space", 4),
    IndentSpec("tab", 1),
)

_JS_TESTS = (
    "**/*.test.*",
    "**/*.spec.*",
    "**/__tests__/**",
    "**/test/**",
    "**/tests/**",
)


def _spec(
    language_id: str,
    tier: Tier,
    extensions: tuple[str, ...],
    module: str,
    func: str,
    indent: IndentSpec,
    tests: tuple[str, ...] = (),
) -> LanguageSpec:
    return LanguageSpec(
        language_id=language_id,
        tier=tier,
        extensions=extensions,
        grammar_loader=_loader(module, func),
        indent_default=indent,
        test_path_globs=tests,
    )


# テストパスのグロブは相対パスに対する gitignore 風（``**`` は 0 個以上のディレクトリ、``*`` は ``/`` を越えない）。
_SPECS: tuple[LanguageSpec, ...] = (
    # Tier 1 必須
    _spec(
        "python",
        Tier.T1,
        (".py", ".pyi"),
        "tree_sitter_python",
        "language",
        _SP4,
        ("**/tests/**", "**/test/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"),
    ),
    _spec(
        "typescript",
        Tier.T1,
        (".ts", ".mts", ".cts"),
        "tree_sitter_typescript",
        "language_typescript",
        _SP2,
        _JS_TESTS,
    ),
    _spec("tsx", Tier.T1, (".tsx",), "tree_sitter_typescript", "language_tsx", _SP2, _JS_TESTS),
    _spec(
        "javascript",
        Tier.T1,
        (".js", ".mjs", ".cjs", ".jsx"),
        "tree_sitter_javascript",
        "language",
        _SP2,
        _JS_TESTS,
    ),
    # Tier 3（階層データ・マークアップ。M1 は文法による骨格と部分取得）
    _spec("json", Tier.T3, (".json",), "tree_sitter_json", "language", _SP2),
    _spec("yaml", Tier.T3, (".yml", ".yaml"), "tree_sitter_yaml", "language", _SP2),
    _spec("toml", Tier.T3, (".toml",), "tree_sitter_toml", "language", _SP4),
    _spec("xml", Tier.T3, (".xml", ".svg"), "tree_sitter_xml", "language_xml", _SP2),
    _spec(
        "markdown", Tier.T3, (".md", ".markdown"), "tree_sitter_markdown", "language", _SP2
    ),  # block の言語
    # best effort（extras `langs`。入っていなければ登録を飛ばす）
    _spec("rust", Tier.T1, (".rs",), "tree_sitter_rust", "language", _SP4, ("**/tests/**",)),
    _spec("go", Tier.T1, (".go",), "tree_sitter_go", "language", _TAB, ("**/*_test.go",)),
    _spec("c", Tier.T1, (".c", ".h"), "tree_sitter_c", "language", _SP4, ("**/test/**", "**/tests/**")),
    _spec(
        "cpp",
        Tier.T1,
        (".cc", ".cpp", ".cxx", ".c++", ".hh", ".hpp", ".hxx", ".h++"),
        "tree_sitter_cpp",
        "language",
        _SP4,
        ("**/test/**", "**/tests/**"),
    ),
    _spec(
        "c_sharp",
        Tier.T1,
        (".cs",),
        "tree_sitter_c_sharp",
        "language",
        _SP4,
        ("**/*Tests.cs", "**/*Test.cs", "**/*.Tests/**", "**/Tests/**"),
    ),
    _spec("rst", Tier.T3, (".rst",), "tree_sitter_rst", "language", _SP3),
    _spec("sql", Tier.T2, (".sql",), "tree_sitter_sql", "language", _SP2),
)


# --- 登録 ---------------------------------------------------------------------------------


def build_registry(
    specs: tuple[LanguageSpec, ...] | list[LanguageSpec],
) -> tuple[dict[str, LanguageSpec], dict[str, str]]:
    """各 ``grammar_loader()`` を試し、``(登録できた表, {language_id: 理由})`` を返す。

    ``ModuleNotFoundError`` は「extras が未導入」、その他は読み込み失敗または ABI 不一致として理由に書く。
    成功した ``Language`` は ``LanguageSpec`` に保持され、以後の ``grammar()`` は再読み込みしない。
    """
    ok: dict[str, LanguageSpec] = {}
    bad: dict[str, str] = {}
    for spec in specs:
        try:
            lang = spec.grammar()
            _check_abi(lang)
        except ModuleNotFoundError as e:
            bad[spec.language_id] = f"grammar package not installed: {e.name or e}"
        except Exception as e:  # 文法の読み込み失敗は登録から外すだけで、起動を止めない
            bad[spec.language_id] = f"{type(e).__name__}: {e}"
        else:
            ok[spec.language_id] = spec
    return ok, bad


SUPPORT: dict[str, LanguageSpec]
_UNAVAILABLE: dict[str, str]
SUPPORT, _UNAVAILABLE = build_registry(_SPECS)

ADAPTERS: dict[str, FormatAdapter] = {}  # language_id -> アダプタ（Tier 3 のデータファイルと .ipynb。末尾で ``register_adapter`` する）

_EXT_TO_LANG: dict[str, str] = {}
_lock = threading.Lock()


def _rebuild_ext_index() -> None:
    index: dict[str, str] = {}
    for table in (ADAPTERS, SUPPORT):  # 文法つきの登録が優先
        for lid, entry in table.items():
            for ext in entry.extensions:
                index[ext.lower()] = lid
    _EXT_TO_LANG.clear()
    _EXT_TO_LANG.update(index)


_rebuild_ext_index()


def register_adapter(adapter: FormatAdapter) -> None:
    """``FormatAdapter`` を ``ADAPTERS`` に足し、拡張子の逆引きを作り直す。"""
    with _lock:
        ADAPTERS[adapter.language_id] = adapter
        _rebuild_ext_index()


def get_support(language_id: str) -> LanguageSpec | FormatAdapter | None:
    """言語 ID から登録を引く。文法つきの ``SUPPORT`` を先に、無ければ ``ADAPTERS``。どちらにも無ければ ``None``。

    json / yaml / toml / xml は文法（``LanguageSpec``）もアダプタも持つので、ここではアダプタを引けない。
    アダプタ（骨格・シンボル）が欲しいときは ``get_adapter`` を使う。
    """
    return SUPPORT.get(language_id) or ADAPTERS.get(language_id)


def get_adapter(language_id: str) -> FormatAdapter | None:
    """言語 ID のデータ形式アダプタ（``ADAPTERS`` だけを見る。文法の有無に関わらない）。無ければ ``None``。"""
    return ADAPTERS.get(language_id)


def language_for_extension(ext: str) -> str | None:
    """拡張子（``".py"`` または ``"py"``。大文字小文字を区別しない）から言語 ID。無ければ ``None``。"""
    key = ext.lower()
    if not key.startswith("."):
        key = "." + key
    return _EXT_TO_LANG.get(key)


def unavailable_languages() -> Mapping[str, str]:
    """起動時に読み込めず登録から外した言語と、その理由。"""
    return dict(_UNAVAILABLE)


def _register_builtin_adapters() -> None:
    """Tier 3 のデータ形式アダプタ（JSON / JSONL / YAML / TOML / XML）と ``.ipynb`` を、import の末尾で 1 回だけ登録する。

    登録の向きは ``support`` → ``adapters`` の一方向。``syntax.adapters`` は ``support`` を import しない（循環しない）ので、
    ``support`` を import すれば必ず登録済みになる（``language_for_extension(".ipynb")`` も ``get_adapter("jsonl")`` も使える）。
    """
    from lucianke.syntax.adapters import REGISTERED

    for adapter in REGISTERED:
        register_adapter(adapter)  # type: ignore[arg-type]  # DataAdapter.select は式の文字列を受ける（Protocol は Selector）


_register_builtin_adapters()
