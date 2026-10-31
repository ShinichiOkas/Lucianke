"""構文木 → ``relations`` の ``structural`` 行の候補（設計書 §7.4.6、仕様書 §9.4・§8.3、T-06 処理規約 2・3、T-07 処理規約 2・4）。

``queries/<言語>/refs.scm`` の捕捉（``@call`` ``@write`` ``@rw`` ``@base`` ``@impl`` ``@type`` ``@read`` ``@import.*``）を
``QueryCursor.matches()`` で読み、参照箇所 1 つにつき ``RelationCandidate`` を 1 件作る。

* ``source_urn``: 参照箇所を含む最も内側の定義シンボル（Function / Method / Constructor / Class など。Variable・Constant・Field は
  囲みとして数えない）。モジュール直下ならファイルの URN。
* 種別（``relation_kind`` / ``use_kind``）: 呼び出し（``new`` を含む）は ``CALLS`` / ``call``、代入の左辺は ``REFERENCES`` / ``w``、複合代入・
  ``++`` は ``REFERENCES`` / ``rw``、型注釈・ジェネリクス引数・キャストは ``TYPE_USES`` / ``type``、import は ``IMPORTS`` / ``import``、
  基底クラスは ``INHERITS`` / ``impl``（TS の ``implements`` は ``IMPLEMENTS`` / ``impl``）、それ以外は ``REFERENCES`` / ``r``。
* 数えないもの: 定義の名前そのもの、キーワード引数の名前、パラメータ・ローカル変数・for/with/except の束縛（その名前の参照も含む）、
  import 文の中の識別子（``IMPORTS`` として別に出す）。位置ごとに 1 件。
* ``target_urn`` の解決（``structural``。型は推論せず、構文から読める範囲だけ）:

  1. 属性アクセス（``recv.name``）: レシーバの型（``self`` / ``this`` の囲みクラス、注釈つきパラメータ・変数、``x = Foo(...)`` /
     ``new Foo()`` で束ねた変数、``self.attr`` の型）が分かれば、そのクラスのメンバーを探す（同じファイルの定義、または
     ``resolve_name(型名)`` の URN + ``.name``）。レシーバがモジュール（``resolve_name`` が ``#`` なしの URN を返す名前）なら ``<URN>#name``。
     同じファイルのクラスに無ければ、同じファイルの基底クラスを C3 の順（``core/mro.py``）にたどる。基底が別ファイル（または
     この場で解決できない名前・``mod.Base`` の形）なら ``<このクラス>.name`` を返し、索引の整え（``index/inherited.py``）が
     ``INHERITS`` 行をファイルをまたいでたどって基底のメンバーに結ぶ（BUG-0075）。
     ``a.b.name`` の受け手は ``a.b`` で、その型は ``a`` が同じファイルのクラスのときの属性 ``b`` の型（注釈・``self.b: T = ...``・
     dataclass のフィールド・``self.b = T()``・戻り値の注釈つきの property / getter。同じファイルの基底も C3 の順）だけから決める（BUG-0079）。
     TS / JS では、``a`` の型が別ファイルの型・同じファイルのインターフェースなら ``a.b`` の型をメンバーの型 ``<型>.b`` とし（URN は
     ``<型>.b.name``）、整えがメンバーの型注釈の構成から決める（決まらなければ名前だけ。BUG-0113）。
     TS の注釈は先頭の名前に縮めずに読む（BUG-0113。``_TsType``）: 添字の型 ``Opts['retry']`` はメンバー ``Opts.retry`` の型、
     配列 ``Opts[]``・タプルは標準の ``Array``、``Foo | null`` は ``Foo``、構成が 2 つ以上の合併・交差・型リテラル・型引数は分からない、
     ``Promise<T>`` は ``await`` で ``T``、``Partial<Foo>`` などメンバーを持ち越す標準の型は ``Foo``、``x as Foo`` は ``Foo``。
     ``super().name``・``super(C, self).name``・TS / JS の ``super.name`` は ``super_of``（``C`` の URN）にし、先は整えが ``C`` の
     線形化の次のクラスから決める（BUG-0081）。
  2. 受け手の型が構文から分からなければ（受け手が式〈``a.b``・``f()``・``x[0]`` など。BUG-0079〉でも、名前〈注釈の無い引数・
     ローカル・型の分からないモジュール直下の変数・クラスの外の ``self`` / ``this``。BUG-0085〉でも）、同じファイルの同名にも
     import した同名にも当てない（``url._uri_reference.copy_with()`` を同じファイルの唯一の ``URL.copy_with`` に、``x.save()`` を
     同じファイルの唯一の ``Base.save`` に結ばない）。
  3. 素の名前（と属性アクセスのレシーバに使う名前）だけ: ``builtins_package`` が渡されていて、言語の組み込み名
     （Python の ``builtins``、TS / JS の標準グローバル。``syntax/builtin_names.py``）なら ``lk://_ext/<builtins_package>#<name>``。
  4. 解決できなければ ``None``（``target_name`` だけ残す。取り込み側は ``relations`` に入れない）。ただし **受け手の型が分からない
     属性呼び出しと属性の読み書き**（``x.name(...)``・``x.name``・``x.name = v``・``x.name += 1`` で ``x`` の型が構文から読めない。
     注釈の無い引数・ローカル、注釈が ``Any`` のもの、``self.x`` の型が不明なもの、呼び出しの戻り値などを含む）は ``name_only=True``
     にする。取り込み側は内部表 ``_name_calls`` に位置と名前と使用種別（``call`` / ``r`` / ``w`` / ``rw``）を残し、T-06 / T-07 が
     問い合わせのときに名前で対象に結ぶ（BUG-0071・BUG-0077・BUG-0095。``tools/_name_calls.py``）。受け手の型が分かっていて
     メンバーが無い（同じファイルのクラスにその名前が無い）ものは ``name_only`` にしない（別のクラスの同名に結ばない）。

  受け手の名前がモジュール（``import m`` / ``from pkg import m`` の ``m``、TS の ``import * as m``）なら、``m.f()`` はメソッドではなく
  モジュールの関数 ``<m の URN>#f``（1 の ``resolve_name`` が ``#`` なしの URN を返す名前）。点つきのモジュールのパス（``import pkg.helpers`` /
  ``import a.b as ab`` / ``from pkg import sub`` の ``pkg.helpers``・``ab.c``・``sub.inner``）も、索引器の ``resolve_dotted`` がワークスペースの
  サブモジュールのファイルを返せばモジュールの受け手（``pkg.helpers.flush()`` → ``pkg/helpers.py#flush``。BUG-0093）。モジュールの名前の
  属性の連鎖（``conf.Settings.make()``）は ``conf.py#Settings.make``（``Settings`` が変数・再び公開した名前なら整えが決める）。外部 URN は
  属性アクセスの連鎖にも伸びる（``import os`` の ``os.path.join(...)`` → ``lk://_ext/os#path.join``、``from decimal import Decimal`` の
  ``Decimal(x).quantize()`` → ``lk://_ext/decimal#Decimal.quantize``）。

  import した名前（``from conf import settings``）の ``settings.reload()`` は ``conf.py#settings.reload`` になる。``settings`` が変数なら、
  索引の整え（``index/inherited.py``）が定義元の変数の型（``var_types``。``settings = Settings()``・``settings: Settings = ...``）の
  メンバーに結ぶか、型が分からなければ名前だけの呼び出し・参照にする（BUG-0094）。``from pkg import flush`` の ``pkg/__init__.py#flush``
  のような再び公開した名前は、整え（``index/reexports.py``）が定義元までたどる（BUG-0100。TS / JS のバレル ``export { f } from`` も。
  BUG-0104）。``from helpers import *`` を持つ Python のファイルで、同じファイル・import 表・組み込み名のどれでも決まらなかった名前は
  ``<このファイル>#name``（``resolve_star``）にし、整えが ``*`` の先が公開する定義に書き換える（BUG-0105）。

  名前空間パッケージ（``__init__.py`` の無いワークスペースのディレクトリ）を束ねた名前とそのディレクトリ（``import nsp.tools.fmt`` の
  ``nsp``・``nsp.tools``）は、``resolve_dotted`` が ``NAMESPACE_PACKAGE`` と答える。その読みは参照を作らず、外部にもせず、名前だけにも
  しない。``import nsp.tools.fmt`` の import 文の行は import したモジュールのファイルへ（BUG-0101）。

* 抽出の添え物（``extract_relations_detailed``）: インスタンス属性の定義（``self.x = ...`` / ``this.x = ...``。名前の一致の候補。
  BUG-0095）と、モジュール直下の変数の構文から分かる型（BUG-0094）、TS の型の別名の構成（``type Options = Base & {...}``。
  同じファイル・別ファイルの別名を注釈にした受け手の ``o.retry`` は ``<別名>.retry`` にし、整え〈``index/inherited.py``〉が構成から
  メンバーを決める。BUG-0108）。

  素の識別子は、囲みの定義を内側から外へたどった修飾名（``outer.name``）→ トップレベルの定義 → ``resolve_name`` の順。
  ローカルに束縛された名前は参照に数えない。ただし ``def`` / ``class`` の中の ``import`` が束縛した名前は、そのスコープ
  （内側の関数とクラス直下を含む）の使用を、その import の先への参照にする（``resolve_import``。モジュール直下の同名の
  import や、別の関数の同名の import は巻き込まない）。
* ``guard_condition`` は ``CALLS`` のとき ``guards.guard_for`` の結果。``confidence`` は ``structural``。

``syntax`` は ``index`` を import できないので、``index.ingest.RelationFact`` と同じ形の ``RelationCandidate`` をここで持つ。
位置は 0 始まりの行・0 始まりのコードポイント列（ADR-14）。
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from tree_sitter import Language, Node, Query

from lucianke.core.foreground import cooperate
from lucianke.core.model import Confidence, Kind, Position, RelPath, SymbolRecord
from lucianke.core.mro import TRANSPARENT_EXTERNAL_BASES, c3_linearize
from lucianke.core.urn import EXTERNAL_WORKSPACE_ID, SCHEME, CodeSel, Urn, parse
from lucianke.syntax.builtin_names import JS_GLOBALS, PYTHON_BUILTINS
from lucianke.syntax.guards import guard_for
from lucianke.syntax.imports import JS_FUNCTION_NODES, NAMESPACE_PACKAGE, js_require_bindings
from lucianke.syntax.parsers import iter_matches
from lucianke.syntax.types import ParsedFile

__all__ = [
    "AttrDefCandidate",
    "ImportKey",
    "RelationCandidate",
    "RelationExtraction",
    "TypeAliasCandidate",
    "VarTypeCandidate",
    "extract_relations",
    "extract_relations_detailed",
    "load_refs_source",
]

# import 文の開始位置（0 始まりの行, コードポイント列）。``ImportFact.range.start`` と同じ
ImportKey = tuple[int, int]

_EXTERNAL_PREFIX = f"{SCHEME}{EXTERNAL_WORKSPACE_ID}/"
_PYTHON = "python"
_JS = "js"  # typescript / tsx / javascript / jsx は同じ処理（TS 固有のノードは JS の文法には現れない）
_FAMILIES = {"python": _PYTHON, "typescript": _JS, "tsx": _JS, "javascript": _JS, "jsx": _JS}
_QUERY_DIRS = {
    "python": "python",
    "typescript": "typescript",
    "tsx": "typescript",
    "javascript": "javascript",
    "jsx": "javascript",
}

# 同じ識別子が複数の種別で捕捉されたときの優先（大きい方が勝つ）
_PRIORITY = {
    "base": 7,
    "impl": 7,
    "type": 6,
    "call": 5,
    "rw": 4,
    "write": 3,
    "read.attr": 2,
    "read": 1,
}
_KIND_OF = {
    "call": ("CALLS", "call"),
    "write": ("REFERENCES", "w"),
    "rw": ("REFERENCES", "rw"),
    "read": ("REFERENCES", "r"),
    "read.attr": ("REFERENCES", "r"),
    "type": ("TYPE_USES", "type"),
    "base": ("INHERITS", "impl"),
    "impl": ("IMPLEMENTS", "impl"),
}

# 囲みとして数えないシンボル種別（その中の参照は外側の定義かファイルに属する）
_NOT_CONTAINERS = frozenset({Kind.VARIABLE, Kind.CONSTANT, Kind.FIELD})
_CLASS_KINDS = frozenset({Kind.CLASS, Kind.INTERFACE, Kind.STRUCT, Kind.TRAIT, Kind.ENUM})
_RECEIVER_SELF = frozenset({"self", "cls", "this"})
_ANNOTATION_SKIP = frozenset(
    {
        "Optional",
        "Union",
        "Final",
        "ClassVar",
        "Annotated",
        "Required",
        "NotRequired",
        "Literal",
        "None",
        "Self",
        # ``x: Any`` は型が分からないのと同じ（``lk://_ext/typing#Any.name`` という実在しない外部メソッドに結ばない。BUG-0071）
        "Any",
    }
)

_PY_FUNCTIONS = frozenset({"function_definition", "lambda"})
# ``imports.extract_imports`` が関数の中の ``require`` を ``nested`` にする基準と同じ集合
_JS_FUNCTIONS = JS_FUNCTION_NODES
_PY_CLASSES = frozenset({"class_definition"})
_JS_CLASSES = frozenset({"class_declaration", "abstract_class_declaration", "class"})
_PY_SKIP_ANCESTORS = frozenset(
    {
        "import_statement",
        "import_from_statement",
        "future_import_statement",
        "global_statement",
        "nonlocal_statement",
    }
)
_JS_SKIP_ANCESTORS = frozenset({"import_statement", "import_require_clause", "import_alias"})
_CALL_NODES = frozenset({"call", "call_expression", "new_expression"})
_NAME_ONLY_KINDS = frozenset({"CALLS", "REFERENCES"})
_VAR_KINDS = frozenset({Kind.VARIABLE, Kind.CONSTANT})
_ATTRIBUTE_NODES = frozenset({"attribute", "member_expression"})
# TS の型の別名の構成（BUG-0108）: メンバーをそのまま持ち越す標準の型（第 1 型引数の型のメンバーと同じ。Pick / Omit は一部だが、
# 外したメンバーを読むのは型エラー）。ワークスペース・import に同名があれば素通りしない
_TS_TRANSPARENT_GENERICS = frozenset({"Partial", "Required", "Readonly", "NonNullable", "Pick", "Omit"})
_TS_MEMBERLESS = frozenset({"null", "undefined", "void", "never"})  # 合併に混ざってもメンバーを持たない構成
_TS_MEMBER_SCOPES = frozenset({Kind.PROPERTY, Kind.FIELD})  # 型リテラルのメンバーを持つメンバー（``retry: { limit: number }``）
_TS_TYPE_MEMBERS = frozenset({Kind.PROPERTY, Kind.METHOD})  # 型リテラルのメンバーの種別（BUG-0114）
# TS の受け手の注釈の読み方（BUG-0113）: ``Promise<T>`` は await で ``T``、添字の型 ``T['k']`` の鍵（識別子の形の文字列だけ）
_TS_PROMISES = frozenset({"Promise", "PromiseLike"})
_TS_KEY = re.compile(r"[A-Za-z_$][\w$]*")
# 型引数を宣言できるノード（注釈の中の ``T`` が型引数なら型は分からない）
_TS_GENERIC_HOLDERS = frozenset(
    {
        "type_alias_declaration",
        "interface_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "class",
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "function_signature",
        "arrow_function",
        "method_definition",
        "method_signature",
        "abstract_method_signature",
        "call_signature",
        "construct_signature",
    }
)


# --- 公開型 -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class AttrDefCandidate:
    """インスタンス属性の定義（``self.x = ...`` / TS・JS の ``this.x = ...``。クラスのメソッドの中）。シンボルにはならないが、
    型の分からない受け手の属性の参照（``mon.x``）を名前で結ぶときの候補に数える（BUG-0095。``tools/_name_calls.py``）。

    同じクラス・同じ名前は最初の代入 1 つ。クラス自身か同じファイルの基底にその名前のシンボル（クラス属性・dataclass のフィールド・
    property・メソッド）があるものは入れない（その定義と同じ属性）。位置は 0 始まりの名前のトークン。
    """

    class_urn: str
    name: str
    line: int
    col: int


@dataclass(frozen=True, slots=True, kw_only=True)
class VarTypeCandidate:
    """モジュール直下の変数の型が構文から分かるもの（``settings = Settings()``・``settings: Settings = ...``・TS の
    ``export const settings = new Settings()``）。別のファイルが ``from conf import settings`` の ``settings.reload()`` を
    その型のメンバーに結ぶのに使う（BUG-0094。``index/inherited.py``）。``type_urn`` はワークスペースのクラスか外部の型の URN。
    """

    var_urn: str
    type_urn: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TypeAliasCandidate:
    """TS の型の別名（``type A = ...``）の構成 1 つ分（BUG-0108。内部表 ``_type_aliases``。``index/inherited.py`` が受け手の型として読む）。

    * ``op``: ``and``（別名そのもの ``type A = B``・交差 ``B & {...}``。構成のどれかが持つメンバーは別名のメンバー）/ ``or``（合併
      ``B | C``。メンバーを持つ構成がちょうど 1 つのときだけ結ぶ）。
    * ``ord``: 構成の順（同じ型リテラルのメンバーは同じ ``ord``）。
    * ``kind``: ``type``（構成する型の URN。ワークスペースのクラス・インターフェース・別名、外部の型）/ ``member``（別名自身の型リテラルの
      メンバー ``<別名>.<名前>``。シンボルなら結び、無ければ「別名自身のメンバー」と分かる）/ ``opaque``（中身の分からない構成。
      関数型・条件型・``string``・添字のシグネチャ・型引数など。``target_urn`` は空）。``null`` / ``undefined`` / ``void`` / ``never`` は書かない。
    """

    alias_urn: str
    op: str
    ord: int
    kind: str
    target_urn: str


@dataclass(frozen=True, slots=True)
class RelationExtraction:
    """``extract_relations_detailed`` の結果: 関係の候補と、インスタンス属性の定義・モジュール直下の変数の型・TS の型の別名の構成。"""

    relations: list[RelationCandidate]
    attr_defs: list[AttrDefCandidate]
    var_types: list[VarTypeCandidate]
    type_aliases: list[TypeAliasCandidate] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class RelationCandidate:
    """``index.ingest.RelationFact`` と同じ形の候補。URN は正準形の文字列、位置は 0 始まり。

    ``target_urn`` が ``None`` のときは解決できなかった（``target_name`` は参照された名前そのまま）。
    ``name_only`` は、``target_urn`` が ``None`` の属性呼び出し・属性の参照（読み・書き。BUG-0095）のうち、受け手の型が分からないもの
    （名前でしか結べない。BUG-0071）。
    ``super_of`` は ``super().name``（``super(C, self).name``・TS / JS の ``super.name``）の ``C`` のクラスの URN。先は ``C`` の C3 線形化で
    ``C`` の次から最初にその名前を持つクラスのメンバーで、索引の整え（``index/inherited.py``）が決める（``target_urn`` は ``None``。BUG-0081）。
    """

    source_urn: str
    target_name: str
    target_urn: str | None
    relation_kind: str
    call_site_line: int
    call_site_col: int
    use_kind: str
    guard_condition: str | None = None
    confidence: Confidence = field(default=Confidence.STRUCTURAL)
    name_only: bool = False
    super_of: str | None = None


def load_refs_source(language_id: str) -> str:
    """``queries/<言語>/refs.scm`` の原文。``tsx`` は typescript、``jsx`` は javascript のものを使う。"""
    try:
        directory = _QUERY_DIRS[language_id]
    except KeyError:
        raise ValueError(f"no refs query for language {language_id!r}") from None
    path = resources.files("lucianke.syntax") / "queries" / directory / "refs.scm"
    return path.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=32)
def _compile(language: Language, language_id: str) -> Query:
    return Query(language, load_refs_source(language_id))


# --- 公開関数 ---------------------------------------------------------------------------


def extract_relations(
    parsed: ParsedFile,
    *,
    language_id: str,
    language: Language,
    symbols: list[SymbolRecord],
    workspace_id: str,
    rel: RelPath,
    resolve_name: Callable[[str], str | None] | None = None,
    builtins_package: str | None = None,
    resolve_import: Callable[[ImportKey, str], str | None] | None = None,
    resolve_dotted: Callable[[str], str | None] | None = None,
    resolve_star: Callable[[str], str | None] | None = None,
) -> list[RelationCandidate]:
    """構文木から ``structural`` の関係の候補を作る。文書順（行、列）。構文エラーがあっても拾えた分を返す。

    ``symbols`` はこのファイルの ``symbols.extract`` の結果。``resolve_name`` は import 表から作った「名前 → URN」
    （``imports.imported_name_map`` と ``resolve_module`` から、定義元ファイルの URN に直したもの。解決できない import は外部 URN）。
    ``builtins_package`` は言語の組み込み名の外部パッケージ名（Python は ``"builtins"``、TS / JS は ``"globals"``）。``None`` なら
    組み込み名を解決しない。``resolve_import`` は ``def`` / ``class`` の中の import（``ImportFact.nested``）が束縛した名前の解決
    （``(import 文の開始位置, 名前) → URN``）。``None`` なら ``resolve_name`` で引く（名前表がスコープを区別しないときの代用）。
    ``resolve_dotted`` は点つきのモジュールのパス（``pkg.helpers``。先頭の名前はモジュール直下の import が束ねたもの）→ その
    サブモジュールのファイルの URN（ワークスペースに在るときだけ。BUG-0093）。``None`` なら点つきのパスをモジュールとして引かない。
    ``resolve_star`` は、素の名前（と受け手・型に使う名前）が同じファイル・import 表・組み込み名のどれでも決まらなかったときの最後の
    引き先（Python の ``from x import *`` を持つファイルで ``<このファイル>#name``。整え〈``index/reexports.py``〉が ``*`` の先が公開する
    定義に書き換える。BUG-0105）。``None`` なら引かない。
    """
    return extract_relations_detailed(
        parsed,
        language_id=language_id,
        language=language,
        symbols=symbols,
        workspace_id=workspace_id,
        rel=rel,
        resolve_name=resolve_name,
        builtins_package=builtins_package,
        resolve_import=resolve_import,
        resolve_dotted=resolve_dotted,
        resolve_star=resolve_star,
    ).relations


def extract_relations_detailed(
    parsed: ParsedFile,
    *,
    language_id: str,
    language: Language,
    symbols: list[SymbolRecord],
    workspace_id: str,
    rel: RelPath,
    resolve_name: Callable[[str], str | None] | None = None,
    builtins_package: str | None = None,
    resolve_import: Callable[[ImportKey, str], str | None] | None = None,
    resolve_dotted: Callable[[str], str | None] | None = None,
    resolve_star: Callable[[str], str | None] | None = None,
) -> RelationExtraction:
    """``extract_relations`` と同じ候補に、インスタンス属性の定義（BUG-0095）とモジュール直下の変数の型（BUG-0094）を添える。"""
    family = _FAMILIES.get(language_id)
    if family is None:
        raise ValueError(f"relation extraction is not supported for language {language_id!r}")
    ex = _Extractor(
        parsed,
        language_id,
        family,
        language,
        symbols,
        workspace_id,
        rel,
        resolve_name,
        builtins_package,
        resolve_import,
        resolve_dotted,
        resolve_star,
    )
    relations = ex.run()
    return RelationExtraction(relations, ex.attr_defs(), ex.var_types(), ex.type_aliases())


# --- 内部 -------------------------------------------------------------------------------


@dataclass(slots=True)
class _Ref:
    node: Node
    category: str
    recv: Node | None = None


@dataclass(slots=True)
class _Import:
    node: Node
    alias: str | None
    module: bool = False  # ``import a.b.c`` の形（名前でなくモジュール）


@dataclass(frozen=True, slots=True)
class _TsType:
    """TS の注釈から読んだ受け手の型（BUG-0113）。注釈の綴りの先頭の名前に縮めない。

    * ``name``: 型の名前（``Foo``・``ns.Foo``）。総称の型 ``Foo<T>`` は基底 ``Foo``、メンバーを持ち越す標準の型 ``Partial<Foo>`` は ``Foo``。
    * ``path``: 添字の型 ``Foo['a']['b']`` のメンバーの列（受け手は ``Foo`` のメンバー ``a`` の型のメンバー ``b`` の型。URN は
      ``<Foo>.a.b`` で、型は索引の整え〈``index/inherited.py``〉がメンバーの型の構成から決める）。
    * ``builtin``: 配列 ``T[]``・タプル（標準の ``Array``。同じファイル・import の同名に隠されない）。``Array<T>`` は名前のまま。
    * ``promised``: ``Promise<T>`` / ``PromiseLike<T>`` の ``T``（``await`` で外す）。
    """

    name: str
    path: tuple[str, ...] = ()
    builtin: bool = False
    promised: _TsType | None = None


type _TypeName = str | _TsType
"""走査で控える型: Python と構築（``new Foo()``）は名前の綴り、TS の注釈は ``_TsType``。"""


@dataclass(slots=True)
class _Scan:
    """構文木 1 回の走査で集める、束縛と型の表。"""

    binders: set[int] = field(default_factory=set)  # 束縛位置の名前ノード（参照に数えない）
    locals: dict[int, set[str]] = field(default_factory=dict)  # 関数ノード → その関数で束縛した名前
    globals: dict[int, set[str]] = field(default_factory=dict)
    var_types: dict[int, dict[str, _TypeName]] = field(default_factory=dict)  # スコープノード → 変数名 → 型
    attr_types: dict[int, dict[str, _TypeName]] = field(default_factory=dict)  # クラスノード → 属性名 → 型
    # 関数・クラスノード → import で束縛した名前 → 文の位置
    imports: dict[int, dict[str, ImportKey]] = field(default_factory=dict)
    nested_imports: set[ImportKey] = field(default_factory=set)  # def / class の中の import 文の位置
    # クラスノード → インスタンス属性の名前 → 最初の代入の名前ノード（``self.x = ...`` / ``this.x = ...``。BUG-0095）
    instance_attrs: dict[int, dict[str, Node]] = field(default_factory=dict)


class _Extractor:
    def __init__(
        self,
        parsed: ParsedFile,
        language_id: str,
        family: str,
        language: Language,
        symbols: list[SymbolRecord],
        workspace_id: str,
        rel: RelPath,
        resolve_name: Callable[[str], str | None] | None,
        builtins_package: str | None = None,
        resolve_import: Callable[[ImportKey, str], str | None] | None = None,
        resolve_dotted: Callable[[str], str | None] | None = None,
        resolve_star: Callable[[str], str | None] | None = None,
    ) -> None:
        self.parsed = parsed
        self._resolve_dotted_raw = resolve_dotted
        self._resolve_star_raw = resolve_star
        self._dotted_cache: dict[str, str | None] = {}
        self.language_id = language_id
        self.family = family
        self.language = language
        self.workspace_id = workspace_id
        self.rel = rel
        self.file_urn = str(Urn(workspace_id, str(rel)))
        self._resolve_raw = resolve_name
        self._resolve_import_raw = resolve_import
        self._import_cache: dict[tuple[ImportKey, str], str | None] = {}
        self._builtins_package = builtins_package
        self._builtin_names = PYTHON_BUILTINS if family == _PYTHON else JS_GLOBALS
        self._resolve_cache: dict[str, str | None] = {}
        self.functions = _PY_FUNCTIONS if family == _PYTHON else _JS_FUNCTIONS
        self.classes = _PY_CLASSES if family == _PYTHON else _JS_CLASSES
        self.skip_ancestors = _PY_SKIP_ANCESTORS if family == _PYTHON else _JS_SKIP_ANCESTORS

        self.by_qualified: dict[str, SymbolRecord] = {}
        self.by_selection: dict[tuple[int, int], SymbolRecord] = {}
        for s in symbols:
            self.by_qualified[s.qualified_name] = (
                s  # 同じ修飾名はあとの宣言が勝つ（オーバーロードの実装、再束縛）
            )
            if s.selection is not None:
                self.by_selection[(s.selection.start.line, s.selection.start.col)] = s
        self.symbols = sorted(symbols, key=lambda s: (s.range.start, _neg_pos(s.range.end)))
        self.scan = _Scan()
        self.class_bases: dict[str, list[str]] = {}  # クラスの修飾名 → 基底クラス名（同じファイルでの解決用）
        self.class_ids: dict[str, int] = {}  # クラスの修飾名 → クラスノードの id（``scan.attr_types`` を引く。BUG-0079）
        self.alias_nodes: list[Node] = []  # TS の ``type A = ...``（走査で文書順に集める。``type_aliases`` が読む。BUG-0108）
        # TS の型注釈つきのメンバー（インターフェース・型リテラルのプロパティ、クラスのフィールド）。``type_aliases`` がメンバーの
        # 型の構成を書く（受け手 ``r: Opts['retry']`` ・``o.retry.limit`` の型。BUG-0113）
        self.member_nodes: list[Node] = []
        # ノードの id → (最も近い囲みのスコープ〈関数・クラス・根〉, 祖先に読み飛ばす文があるか)。祖先を 1 回だけたどって覚える（BUG-0112）
        self._up_memo: dict[int, tuple[Node | None, bool]] = {}
        self._root_id = parsed.tree.root_node.id

    # --- 名前解決の窓口 ---

    def _text(self, node: Node) -> str:
        """ノードの綴り。``Node.text`` は使わない（read_callback で解析した木では読み取り関数を呼び直し、
        解放済みの領域を読みうる。BUG-0020・BUG-0051）。"""
        src = self.parsed.source
        return src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def resolve(self, name: str) -> str | None:
        if self._resolve_raw is None:
            return None
        if name not in self._resolve_cache:
            self._resolve_cache[name] = self._resolve_raw(name)
        return self._resolve_cache[name]

    def resolve_scoped(self, key: ImportKey, name: str) -> str | None:
        """``def`` / ``class`` の中の import 文（開始位置 ``key``）が束ねた ``name`` の URN。"""
        if self._resolve_import_raw is None:
            return self.resolve(name)
        ck = (key, name)
        if ck not in self._import_cache:
            self._import_cache[ck] = self._resolve_import_raw(key, name)
        return self._import_cache[ck]

    def resolve_star(self, name: str) -> str | None:
        """``from x import *`` が持ち込んだかもしれない名前の仮の URN（``<このファイル>#name``。BUG-0105）。"""
        if self._resolve_star_raw is None:
            return None
        return self._resolve_star_raw(name)

    def resolve_builtin(self, name: str) -> str | None:
        """言語の組み込み名なら ``lk://_ext/<builtins_package>#<name>``。"""
        if self._builtins_package is None or name not in self._builtin_names:
            return None
        try:
            return str(Urn.external(self._builtins_package, None, name))
        except ValueError:
            return None

    # --- 位置 ---

    def pos(self, node: Node) -> Position:
        return self.parsed.line_index.offset_to_position(node.start_byte).value

    # --- 実行 ---

    def run(self) -> list[RelationCandidate]:
        root = self.parsed.tree.root_node
        self._scan_tree(root)
        refs, imports = self._collect(root)

        # 参照を位置順に並べ、囲みのシンボルを掃引で決める
        items: list[tuple[Position, str, Any]] = []
        for ref in refs.values():
            items.append((self.pos(ref.node), "ref", ref))
        for imp in imports:
            items.append((self.pos(imp.node), "import", imp))
        items.sort(key=lambda t: (t[0], t[1] != "ref"))

        out: list[RelationCandidate] = []
        stack: list[SymbolRecord] = []
        pending = iter(self.symbols)
        nxt = next(pending, None)
        for position, what, item in items:
            cooperate()  # 背景の索引なら、前景のツールの呼び出しに GIL を譲る（BUG-0112）
            while nxt is not None and nxt.range.start <= position:
                # 積む前に、nxt より前に終わった定義を降ろす（以後のどの位置も囲まない）。降ろさないと、並んだ定義（圧縮した JS の
                # 数千の関数）が積み上がり、参照ごとの囲みの絞り込みが定義の数に比例して、参照の数 × 定義の数になる（BUG-0112）
                while stack and not (nxt.range.start < stack[-1].range.end):
                    stack.pop()
                stack.append(nxt)
                nxt = next(pending, None)
            while stack and not (position < stack[-1].range.end):
                stack.pop()
            chain = [s for s in stack if s.range.start <= position < s.range.end]
            if what == "ref":
                cand = self._ref_candidate(item, position, chain)
            else:
                cand = self._import_candidate(item, position, chain)
            if cand is not None:
                out.append(cand)
        return out

    def attr_defs(self) -> list[AttrDefCandidate]:
        """インスタンス属性の定義（``run`` の後に呼ぶ）。クラス自身か同じファイルの基底に同名のシンボル・インスタンス属性があれば入れない。"""
        out: list[AttrDefCandidate] = []
        for qualified, cid in self.class_ids.items():
            attrs = self.scan.instance_attrs.get(cid)
            class_urn = self._local_urn(qualified) if attrs else None
            if not attrs or class_urn is None:
                continue
            order = c3_linearize(("local", qualified), self._local_bases) or [("local", qualified)]
            for name, node in attrs.items():
                if f"{qualified}.{name}" in self.by_qualified or self._declared_in_local_base(order, name):
                    continue
                p = self.pos(node)
                out.append(AttrDefCandidate(class_urn=class_urn, name=name, line=p.line, col=p.col))
        out.sort(key=lambda a: (a.line, a.col))
        return out

    def _declared_in_local_base(self, order: list[tuple[str, str]], name: str) -> bool:
        """線形化 ``order`` の基底（先頭のクラス自身を除く）のうち、別ファイル・外部の基底より前の同じファイルの基底が ``name`` を持つか。"""
        for kind, value in order[1:]:
            if kind != "local":
                return False  # その先は分からない（索引の問い合わせで基底をたどって同じ属性にまとめる）
            if f"{value}.{name}" in self.by_qualified:
                return True
            cid = self.class_ids.get(value)
            if cid is not None and name in self.scan.instance_attrs.get(cid, {}):
                return True
        return False

    def var_types(self) -> list[VarTypeCandidate]:
        """モジュール直下の変数・定数のうち、型が構文から分かるもの（``run`` の後に呼ぶ）。"""
        out: list[VarTypeCandidate] = []
        root = self.parsed.tree.root_node
        for name, tname in self.scan.var_types.get(root.id, {}).items():
            sym = self.by_qualified.get(name)
            if sym is None or sym.kind not in _VAR_KINDS:
                continue
            ref = self._tname_ref(tname, [])
            if ref is None:
                continue
            kind, value = ref
            if kind == "local":
                local = self.by_qualified.get(value)
                type_urn = self._local_urn(value) if local is not None and local.kind in _CLASS_KINDS else None
            else:
                type_urn = value if "#" in value else None  # モジュールそのものは型ではない
            if type_urn is not None:
                out.append(VarTypeCandidate(var_urn=str(sym.urn), type_urn=type_urn))
        return out

    def type_aliases(self) -> list[TypeAliasCandidate]:
        """TS の型の別名（``type A = ...``。シンボルになったもの）の構成（BUG-0108。``run`` の後に呼ぶ）。TS / JS 以外は空。

        値が名前の型（``B``・``ns.B``・``B<T>``）なら ``and`` の構成 1 つ、交差（``B & {...} & C``）は ``and``、合併（``B | C``）は ``or`` で、
        書いた順に並べる（左に入れ子の二項を平らにする。括弧の中の同じ演算子も）。型リテラル ``{ a: T; m(): void }`` のメンバーは
        別名自身のメンバー（``member``）。``Partial<B>`` などメンバーを持ち越す標準の型は ``B``。それ以外（関数型・条件型・``string``・
        添字のシグネチャ・別名の型引数・合併の中の交差など）は ``opaque``。配列・タプルは標準の ``Array``、添字の型 ``B['k']`` は
        ``<B>.k``（メンバーの型。BUG-0113）。

        型注釈つきのメンバー（インターフェース・型リテラルのプロパティ ``retry?: RetryOptions | number``、クラスのフィールド。シンボルに
        なったもの）の型も同じ形で書く（``alias_urn`` はメンバーの URN。型リテラル ``retry: { limit: number }`` は ``member`` の
        ``<メンバー>.limit``。BUG-0113・BUG-0114）。受け手 ``r: Opts['retry']``・``o.retry.limit`` の型を整えが構成から決める。中身の
        分からない構成しか無いメンバー（``limit: number``）は書かない（行が無ければ型が分からないのと同じ。量を抑える）。
        """
        if self.family != _JS:
            return []
        out = [part for node in self.alias_nodes for part in self._alias_parts(node)]
        for node in self.member_nodes:
            out.extend(self._member_parts(node))
        return out

    def _alias_parts(self, decl: Node) -> list[TypeAliasCandidate]:
        name = decl.child_by_field_name("name")
        value = decl.child_by_field_name("value")
        if name is None or value is None:
            return []
        p = self.pos(name)
        sym = self.by_selection.get((p.line, p.col))
        if sym is None or sym.kind is not Kind.TYPE:
            return []
        return self._type_parts(str(sym.urn), value, self._type_params(decl))

    def _member_parts(self, member: Node) -> list[TypeAliasCandidate]:
        """型注釈つきのメンバー（``property_signature``・``public_field_definition``）の型の構成（BUG-0113）。"""
        name = member.child_by_field_name("name")
        annotation = member.child_by_field_name("type")
        value = next((c for c in annotation.named_children if c.type != "comment"), None) if annotation else None
        if name is None or value is None:
            return []
        p = self.pos(name)
        sym = self.by_selection.get((p.line, p.col))
        if sym is None or sym.kind not in (Kind.PROPERTY, Kind.FIELD):
            return []
        parts = self._type_parts(str(sym.urn), value, self._type_params(member))
        return parts if any(part.kind != "opaque" for part in parts) else []

    def _type_parts(self, owner_urn: str, value: Node, hidden: frozenset[str]) -> list[TypeAliasCandidate]:
        """型 ``value`` の構成（``owner_urn`` は別名・メンバー。型リテラルのメンバーは ``<owner_urn>.<名前>``）。"""
        value = self._unparen(value)
        op = "or" if value.type == "union_type" else "and"
        parts = self._flatten(value, value.type) if value.type in ("union_type", "intersection_type") else [value]
        out: list[TypeAliasCandidate] = []
        for ord_, part in enumerate(parts):
            for kind, target in self._alias_part(part, owner_urn, hidden, op):
                out.append(TypeAliasCandidate(alias_urn=owner_urn, op=op, ord=ord_, kind=kind, target_urn=target))
        return out

    def _type_params(self, node: Node) -> frozenset[str]:
        """``node`` から見える型引数の名前（自身と囲みの別名・インターフェース・クラス・関数・メソッドの ``<T>``）。"""
        names: set[str] = set()
        cur: Node | None = node
        while cur is not None:
            if cur.type in _TS_GENERIC_HOLDERS:
                params = cur.child_by_field_name("type_parameters")
                for tp in params.named_children if params is not None else ():
                    n = tp.child_by_field_name("name") if tp.type == "type_parameter" else None
                    if n is not None:
                        names.add(self._text(n))
            cur = cur.parent
        return frozenset(names)

    def _unparen(self, node: Node) -> Node:
        while node.type == "parenthesized_type" and node.named_children:
            node = node.named_children[0]
        return node

    def _flatten(self, node: Node, op_type: str) -> list[Node]:
        """左に入れ子の ``A & B & C``（``A | B | C``）の構成を書いた順に。括弧の中の同じ演算子も平らにする。"""
        out: list[Node] = []
        for child in node.named_children:
            inner = self._unparen(child)
            if inner.type == op_type:
                out.extend(self._flatten(inner, op_type))
            elif child.type != "comment":
                out.append(inner)
        return out

    def _alias_part(self, node: Node, alias_urn: str, hidden: frozenset[str], op: str) -> list[tuple[str, str]]:
        """構成 1 つ → ``(kind, target_urn)`` の列（空ならメンバーを持たない構成）。"""
        node = self._unparen(node)
        t = node.type
        if t == "literal_type" or (t == "predefined_type" and self._text(node) in _TS_MEMBERLESS):
            text = self._text(node)
            return [] if text in _TS_MEMBERLESS else [("opaque", "")]
        if t in ("array_type", "tuple_type", "readonly_type", "lookup_type"):
            # 配列・タプルは標準の Array（ReadonlyArray）、添字の型 ``B['k']`` はメンバーの型 ``<B>.k``（BUG-0113）
            ref = self._ts_ref(self._ts_type(node, hidden), [])
            urn = ref[1] if ref is not None and ref[0] == "urn" and "#" in ref[1] else None
            return [("type", urn)] if urn is not None else [("opaque", "")]
        if t == "object_type":
            out: list[tuple[str, str]] = []
            for member in node.named_children:
                if member.type in ("property_signature", "method_signature"):
                    mname = member.child_by_field_name("name")
                    joined = (
                        _append_member(alias_urn, self._text(mname))
                        if mname is not None and mname.type == "property_identifier"
                        else None
                    )
                    out.append(("member", joined) if joined is not None else ("opaque", ""))
                elif member.type == "index_signature":
                    out.append(("opaque", ""))  # どの名前のメンバーもありうる
            return out
        if t == "generic_type":
            base = node.child_by_field_name("name")
            args = node.child_by_field_name("type_arguments")
            if (
                base is not None
                and base.type == "type_identifier"
                and self._text(base) in _TS_TRANSPARENT_GENERICS
                and self._is_global_type(self._text(base))
                and args is not None
                and args.named_children
            ):
                inner = self._unparen(args.named_children[0])
                if op == "and" and inner.type == "intersection_type":
                    return [x for sub in self._flatten(inner, inner.type) for x in self._alias_part(sub, alias_urn, hidden, op)]
                return self._alias_part(inner, alias_urn, hidden, op)
            node, t = (base, base.type) if base is not None else (node, t)
        if t == "type_identifier":
            text = self._text(node)
            urn = None if text in hidden else self._type_urn(text)
        elif t == "nested_type_identifier":
            module = node.child_by_field_name("module")
            leaf = node.child_by_field_name("name")
            ref = self._name_ref(self._text(module), []) if module is not None and module.type == "identifier" else None
            urn = None
            if ref is not None and leaf is not None:
                kind, value = ref
                if kind == "local":
                    hit = self.by_qualified.get(f"{value}.{self._text(leaf)}")
                    urn = str(hit.urn) if hit is not None else None
                else:
                    urn = _append_member(value, self._text(leaf))
        else:
            return [("opaque", "")]
        return [("type", urn)] if urn is not None else [("opaque", "")]

    def _type_urn(self, name: str) -> str | None:
        """型の名前 → URN（同じファイルのクラス・インターフェース・別名・列挙、import した名前、標準のグローバル）。"""
        local = self.by_qualified.get(name)
        if local is not None and local.kind in _CLASS_KINDS | {Kind.TYPE}:
            return str(local.urn)
        urn = self.resolve(name) or self.resolve_builtin(name)
        return urn if urn is not None and "#" in urn else None

    def _is_global_type(self, name: str) -> bool:
        """``name`` が標準のグローバルの型か（同じファイルの定義・import した同名に隠されていない）。"""
        return name not in self.by_qualified and self.resolve(name) is None and self.resolve_builtin(name) is not None

    # --- TS の注釈の読み方（BUG-0113） ---

    def _ts_type(self, node: Node | None, hidden: frozenset[str] = frozenset()) -> _TsType | None:
        """TS の型注釈（``type_annotation`` かその中の型）を受け手の型に読む。決まらなければ ``None``（型の分からない受け手）。

        先頭の名前に縮めない（``Opts['retry']`` は ``Opts`` ではなく ``Opts`` のメンバー ``retry`` の型、``Opts[]`` は配列）:

        * 名前の型 ``Foo``・``ns.Foo``（型引数 ``T`` は分からない）。総称の型 ``Foo<T>`` は ``Foo``（``Array<T>`` は ``Array``）。
        * ``null`` / ``undefined`` / ``void`` / ``never`` を除いて構成が 1 つの合併（``Foo | null``）はその構成。2 つ以上は分からない。
        * メンバーを持ち越す標準の型（``Partial<Foo>``・``Readonly<Foo>`` など）は ``Foo``。``Promise<T>`` は ``Promise``（``await`` で ``T``）、
          ``Awaited<Promise<T>>`` は ``T``。
        * 配列 ``T[]``・タプル ``[A, B]`` は標準の ``Array``、``readonly T[]`` は ``ReadonlyArray``（要素の型にしない）。
        * 添字の型 ``Foo['k']``（鍵が識別子の形の文字列のときだけ）は ``Foo`` のメンバー ``k`` の型。
        * それ以外（型リテラル・交差・関数型・条件型・``keyof``・``typeof``・``string`` など）は分からない。
        """
        if node is None:
            return None
        if node.type == "type_annotation":
            node = next((c for c in node.named_children if c.type != "comment"), None)
            if node is None:
                return None
        node = self._unparen(node)
        t = node.type
        if t == "union_type":
            keep = [p for p in self._flatten(node, t) if not self._memberless(p)]
            return self._ts_type(keep[0], hidden) if len(keep) == 1 else None
        if t == "type_identifier":
            text = self._text(node)
            return None if text in hidden else _TsType(text)
        if t == "nested_type_identifier":
            return _TsType("".join(self._text(node).split()))
        if t in ("array_type", "tuple_type"):
            return _TsType("Array", builtin=True)
        if t == "readonly_type":
            inner = next((c for c in node.named_children if c.type != "comment"), None)
            spec = self._ts_type(inner, hidden) if inner is not None else None
            return _TsType("ReadonlyArray", builtin=True) if spec is not None and spec.builtin else None
        if t == "generic_type":
            base = node.child_by_field_name("name")
            if base is None:
                return None
            args_node = node.child_by_field_name("type_arguments")
            args = [c for c in args_node.named_children if c.type != "comment"] if args_node is not None else []
            text = self._text(base)
            if base.type == "type_identifier" and text not in hidden and self._is_global_type(text):
                if text in _TS_TRANSPARENT_GENERICS:
                    return self._ts_type(args[0], hidden) if args else None
                if text in _TS_PROMISES:
                    return _TsType(text, promised=self._ts_type(args[0], hidden) if args else None)
                if text == "Awaited":
                    inner = self._ts_type(args[0], hidden) if args else None
                    return inner.promised if inner is not None and inner.name in _TS_PROMISES else inner
            return self._ts_type(base, hidden)
        if t == "lookup_type":
            parts = [c for c in node.named_children if c.type != "comment"]
            if len(parts) != 2:
                return None
            obj = self._ts_type(parts[0], hidden)
            key = self._string_key(parts[1])
            if obj is None or key is None or obj.builtin or obj.promised is not None or obj.name in _TS_PROMISES:
                return None
            return _TsType(obj.name, (*obj.path, key))
        return None

    def _memberless(self, node: Node) -> bool:
        node = self._unparen(node)
        return node.type in ("literal_type", "predefined_type") and self._text(node) in _TS_MEMBERLESS

    def _string_key(self, node: Node) -> str | None:
        """添字の型の鍵 ``'retry'``（識別子の形の文字列リテラルの型だけ）。"""
        if node.type != "literal_type" or node.named_child_count != 1:
            return None
        lit = node.named_children[0]
        if lit.type != "string":
            return None
        frags = [c for c in lit.named_children if c.type == "string_fragment"]
        text = self._text(frags[0]) if len(frags) == 1 and lit.named_child_count == 1 else ""
        return text if _TS_KEY.fullmatch(text) else None

    def _tname_ref(self, tname: _TypeName, chain: list[SymbolRecord], node: Node | None = None) -> tuple[str, str] | None:
        """走査で控えた型（名前の綴り・``_TsType``）→ ``("local", 修飾名)`` / ``("urn", URN)``。"""
        if isinstance(tname, str):
            return self._name_ref(tname, chain, node)
        return self._ts_ref(tname, chain, node)

    def _ts_ref(self, spec: _TsType | None, chain: list[SymbolRecord], node: Node | None = None) -> tuple[str, str] | None:
        """``_TsType`` → ``("local", 修飾名)`` / ``("urn", URN)``。添字の型はメンバーの URN ``<型>.k``（ワークスペースの型だけ。
        外部の型のメンバーの型は分からない）。"""
        if spec is None:
            return None
        if spec.builtin:
            urn = self.resolve_builtin(spec.name)
            return ("urn", urn) if urn is not None else None
        base = self._dotted_type_ref(spec.name, chain, node)
        if base is None or not spec.path:
            return base
        kind, value = base
        urn = self._local_urn(value) if kind == "local" else value
        if urn is None or urn.startswith(_EXTERNAL_PREFIX) or "#" not in urn:
            return None
        for key in spec.path:
            urn = _append_member(urn, key)
            if urn is None:
                return None
        return ("urn", urn)

    def _dotted_type_ref(self, name: str, chain: list[SymbolRecord], node: Node | None) -> tuple[str, str] | None:
        """型の名前（``Foo``・``ns.Foo``・``a.b.Foo``）→ ``_name_ref`` の形。点つきは先頭の名前（名前空間・``import * as ns``）から
        たどる（``express.Request`` を同名の標準のグローバル ``Request`` にしない）。"""
        head, _, rest = name.partition(".")
        if not rest:
            return self._name_ref(name, chain, node)
        ref = self._name_ref(head, chain, node)
        if ref is None:
            return None
        kind, value = ref
        if kind == "local":
            hit = self.by_qualified.get(f"{value}.{rest}")
            if hit is None:
                return None
            if hit.kind in _CLASS_KINDS:
                return ("local", hit.qualified_name)
            return ("urn", str(hit.urn)) if hit.kind is Kind.TYPE else None
        urn: str | None = value
        for seg in rest.split("."):
            urn = _append_member(urn, seg) if urn is not None else None
        return ("urn", urn) if urn is not None else None

    def _member_type(self, annotation: Node | None, owner: str | None, name: str) -> _TsType | None:
        """クラスのメンバー（フィールド・getter）の型。名前の型に読めない注釈（型リテラル・合併・交差）は、メンバー自身の型
        ``<クラス>['name']``（整えがメンバーの型の構成〈``_type_aliases``〉から決める。``this.opts.z`` の ``opts: { z: number }``）。"""
        if annotation is None or self.family != _JS:
            return None
        spec = self._ts_type(annotation, self._type_params(annotation))
        if spec is not None or owner is None:
            return spec
        inner = next((c for c in annotation.named_children if c.type != "comment"), None)
        inner = self._unparen(inner) if inner is not None else None
        if inner is not None and inner.type in ("object_type", "union_type", "intersection_type"):
            return _TsType(owner, (name,))
        return None

    def _class_qualified(self, cls: Node | None) -> str | None:
        """クラスノードの修飾名（シンボルにならなかったクラスは ``None``）。"""
        name = cls.child_by_field_name("name") if cls is not None else None
        if name is None:
            return None
        sym = self.by_selection.get((self.pos(name).line, self.pos(name).col))
        return sym.qualified_name if sym is not None else None

    # --- 候補の収集 ---

    def _collect(self, root: Node) -> tuple[dict[int, _Ref], list[_Import]]:
        refs: dict[int, _Ref] = {}
        imports: dict[int, _Import] = {}

        def put(node: Node, category: str, recv: Node | None) -> None:
            cur = refs.get(node.start_byte)
            if cur is None or _PRIORITY[category] > _PRIORITY[cur.category]:
                refs[node.start_byte] = _Ref(node, category, recv)
            elif _PRIORITY[category] == _PRIORITY[cur.category] and cur.recv is None and recv is not None:
                cur.recv = recv

        for _pattern, caps in iter_matches(_compile(self.language, self.language_id), root, len(self.parsed.source)):
            cooperate()
            for key, nodes in caps.items():
                if key == "type.annotation":
                    for n in nodes:
                        for name_node, recv in self._annotation_names(n):
                            put(name_node, "type", recv)
                elif key in _PRIORITY:
                    recv_nodes = caps.get(f"{key.split('.')[0]}.recv")
                    for n in nodes:
                        put(n, key, recv_nodes[0] if recv_nodes else None)
                elif key.startswith("import.") and not key.endswith(".alias"):
                    alias = caps.get("import.alias")
                    for n in nodes:
                        cur = imports.get(n.start_byte)
                        text = self._text(alias[0]) if alias else None
                        if cur is None:
                            imports[n.start_byte] = _Import(n, text, module=key == "import.module")
                        elif cur.alias is None and text is not None:
                            cur.alias = text
        return refs, list(imports.values())

    def _annotation_names(self, node: Node) -> Iterator[tuple[Node, Node | None]]:
        """型注釈の中の型名（``a.B`` は ``B`` とレシーバ ``a``）。文字列（前方参照）は読まない。"""
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                yield n, None
            elif n.type == "attribute":
                attr = n.child_by_field_name("attribute")
                if attr is not None:
                    yield attr, n.child_by_field_name("object")
            elif n.type not in ("string", "comment"):
                stack.extend(reversed(n.children))

    # --- 参照 1 件 ---

    def _ref_candidate(
        self, ref: _Ref, position: Position, chain: list[SymbolRecord]
    ) -> RelationCandidate | None:
        node = ref.node
        if (position.line, position.col) in self.by_selection:
            return None  # 定義の名前そのもの
        if node.id in self.scan.binders:
            return None
        if self._in_skipped_context(node):
            return None
        name = self._text(node)
        if not name:
            return None
        container = self._container(chain)
        source = str(container.urn) if container is not None else self.file_urn

        is_member = self._is_member_name(node)
        receiver_known = False
        relation_kind, use_kind = _KIND_OF[ref.category]
        super_of: str | None = None
        if is_member:
            is_super, super_of = (
                self._super_receiver(ref.recv, node, chain) if ref.recv is not None else (False, None)
            )
            if is_super:
                # ``super()`` の受け手は囲みのクラスの基底（構文から分かる）。名前では結ばない（BUG-0081）
                target, receiver_known = None, True
                if relation_kind not in ("CALLS", "REFERENCES"):
                    super_of = None
            else:
                target, receiver_known = self._resolve_member(ref.recv, name, node, chain)
        else:
            is_local, target = self._resolve_bare(name, node, chain)
            if is_local:
                return None
        guard = None
        if relation_kind == "CALLS":
            call_node = self._call_node(node)
            if call_node is not None:
                guard = guard_for(call_node, self.parsed, language_id=self.language_id)
        return RelationCandidate(
            source_urn=source,
            target_name=name,
            target_urn=target,
            relation_kind=relation_kind,
            call_site_line=position.line,
            call_site_col=position.col,
            use_kind=use_kind,
            guard_condition=guard,
            # 呼び出しも属性の読み書き（BUG-0095）も、受け手の型が分からなければ名前の一致に任せる（型注釈・基底クラスの
            # ``a.B`` は対象外: 受け手はモジュールかクラスの名前で、型が分からないことはまず無い）
            name_only=is_member and target is None and not receiver_known and relation_kind in _NAME_ONLY_KINDS,
            super_of=super_of,
        )

    def _import_candidate(
        self, imp: _Import, position: Position, chain: list[SymbolRecord]
    ) -> RelationCandidate | None:
        node = imp.node
        name = self._text(node)
        if not name:
            return None
        target: str | None = None
        key = self._import_key(node)
        resolve = self.resolve if key is None else (lambda n: self.resolve_scoped(key, n))
        if imp.alias:
            target = resolve(imp.alias)
        if target is None:
            target = resolve(name)
        if target is None and imp.module and imp.alias is None:
            target = resolve(name.split(".")[0])
        if key is None and (target is None or target.startswith(_EXTERNAL_PREFIX)):
            bound = imp.alias or (name.split(".")[0] if imp.module else name)
            if self._namespace_name(bound):
                # 名前空間パッケージ（BUG-0101）: 外部にしない。``import nsp.tools.fmt`` は import したモジュールのファイルへ
                # （束ねる先頭の ``nsp`` にはファイルが無い）。ディレクトリそのもの（``import nsp.tools as nt``）は行を作らない
                leaf = self._resolve_dotted_raw(name) if imp.module and imp.alias is None and "." in name else None
                target = leaf if leaf is not None and leaf != NAMESPACE_PACKAGE else None
        container = self._container(chain)
        return RelationCandidate(
            source_urn=str(container.urn) if container is not None else self.file_urn,
            target_name=name,
            target_urn=target,
            relation_kind="IMPORTS",
            call_site_line=position.line,
            call_site_col=position.col,
            use_kind="import",
        )

    def _import_key(self, node: Node) -> ImportKey | None:
        """名前ノードを含む import 文が ``def`` / ``class`` の中にあれば、その開始位置。"""
        stmt: Node | None = node
        while stmt is not None and stmt.type not in ("import_statement", "import_from_statement"):
            stmt = stmt.parent
        if stmt is None:
            return None
        p = self.pos(stmt)
        return (p.line, p.col) if (p.line, p.col) in self.scan.nested_imports else None

    def _container(self, chain: list[SymbolRecord]) -> SymbolRecord | None:
        for s in reversed(chain):
            if s.kind not in _NOT_CONTAINERS:
                return s
        return None

    # --- 祖先（覚えて使い回す） ---

    def _up(self, node: Node) -> tuple[Node | None, bool]:
        """``(node の最も近い囲みのスコープ, node の祖先に読み飛ばす文〈skip_ancestors〉があるか)``。

        スコープは関数・クラス・根（``node`` 自身は数えない。根の囲みは ``None``）。祖先をたどった結果をノードごとに覚え、次は覚えた
        祖先で止まる（BUG-0112）。``Node.parent`` は木の根から下りて親を探すので、参照ごとに根まで登ると、深く入れ子になった
        圧縮済みの JS（1 ファイル 数 MB）で参照の数 × 深さ × 深さになり、1 ファイルの関係の抽出に数分かかっていた。
        """
        memo = self._up_memo
        hit = memo.get(node.id)
        if hit is not None:
            return hit
        chain = [node]
        while True:
            parent = chain[-1].parent
            if parent is None:  # 根
                memo[chain[-1].id] = (None, False)
                break
            if parent.id in memo:
                chain.append(parent)
                break
            chain.append(parent)
        for i in range(len(chain) - 2, -1, -1):
            parent = chain[i + 1]
            scope_above, skipped_above = memo[parent.id]
            is_scope = parent.type in self.functions or parent.type in self.classes or parent.id == self._root_id
            memo[chain[i].id] = (parent if is_scope else scope_above, skipped_above or parent.type in self.skip_ancestors)
        return memo[node.id]

    def _scopes(self, node: Node) -> Iterator[Node]:
        """``node`` を囲むスコープ（関数・クラス・根）を内側から外へ（``node`` 自身は含まない）。"""
        scope = self._up(node)[0]
        while scope is not None:
            yield scope
            scope = self._up(scope)[0]

    # --- 構文上の位置の判定 ---

    def _is_member_name(self, node: Node) -> bool:
        parent = node.parent
        if parent is None or parent.type not in _ATTRIBUTE_NODES:
            return False
        field_name = "attribute" if parent.type == "attribute" else "property"
        target = parent.child_by_field_name(field_name)
        return target is not None and target.id == node.id

    def _in_skipped_context(self, node: Node) -> bool:
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "keyword_argument":
            n = parent.child_by_field_name("name")
            if n is not None and n.id == node.id:
                return True
        if parent.type in ("function_definition", "class_definition"):
            n = parent.child_by_field_name("name")
            if n is not None and n.id == node.id:
                return True
        if parent.type == "type_parameter":  # TS ``<T>``
            return True
        return self._up(node)[1]

    def _call_node(self, name_node: Node) -> Node | None:
        """名前ノードを関数位置（``new`` の constructor）に持つ呼び出しノード。"""
        node: Node | None = name_node
        while node is not None and node.parent is not None:
            parent = node.parent
            if parent.type in _CALL_NODES:
                fn = parent.child_by_field_name(
                    "constructor" if parent.type == "new_expression" else "function"
                )
                if fn is not None and fn.id == node.id:
                    return parent
                return None
            if parent.type not in _ATTRIBUTE_NODES:
                return None
            node = parent
        return None

    # --- 素の識別子の解決 ---

    def _binding(self, name: str, node: Node) -> tuple[str, ImportKey | None]:
        """``node`` から見た ``name`` の束縛: ``("local", None)``（パラメータ・代入など）、``("import", 文の位置)``
        （``def`` / ``class`` の中の import）、``("none", None)``（関数・クラスの中では束縛されていない）。

        内側のスコープから外へ、最初に見つかったものが勝つ（Python の名前解決と同じ）。クラス直下の束縛は、使用箇所がその
        クラス直下（間に関数が無い）のときだけ見える。``global`` で宣言された名前はモジュールの名前。
        """
        innermost = True
        for anc in self._scopes(node):
            if anc.type in self.functions:
                if name in self.scan.globals.get(anc.id, ()):
                    return "none", None
                key = self.scan.imports.get(anc.id, {}).get(name)
                if key is not None:
                    return "import", key
                if name in self.scan.locals.get(anc.id, ()):
                    return "local", None
                innermost = False
            elif anc.type in self.classes:
                if innermost:
                    key = self.scan.imports.get(anc.id, {}).get(name)
                    if key is not None:
                        return "import", key
                innermost = False
        return "none", None

    def _resolve_bare(self, name: str, node: Node, chain: list[SymbolRecord]) -> tuple[bool, str | None]:
        """``(ローカルか, URN)``。ローカルに束縛された名前は ``(True, None)``。"""
        # 囲みの定義を内側から外へ（クラスは、参照がそのクラスの直下にあるときだけ）。TS の型の別名と、型のメンバーの中の型リテラルの
        # メンバーは素の名前の範囲ではない（``readonly stop: typeof stop`` の ``stop`` は import した値で、メンバー自身ではない。BUG-0114）
        for i in range(len(chain) - 1, -1, -1):
            s = chain[i]
            if s.kind in _CLASS_KINDS and i != len(chain) - 1:
                continue
            if self.family == _JS and s.kind is Kind.TYPE:
                continue
            hit = self.by_qualified.get(f"{s.qualified_name}.{name}")
            if hit is not None and self.family == _JS and s.kind in _TS_MEMBER_SCOPES and hit.kind in _TS_TYPE_MEMBERS:
                continue
            if hit is not None:
                return False, str(hit.urn)
        kind, key = self._binding(name, node)
        if kind == "local":
            return True, None
        if kind == "import" and key is not None:  # def / class の中の import が束ねた名前: その import の先
            return False, self.resolve_scoped(key, name)
        top = self.by_qualified.get(name)
        if top is not None:
            return False, str(top.urn)
        if self._namespace_name(name):
            return False, None  # 名前空間パッケージ（ワークスペースのディレクトリ）そのもの: 参照でも外部でもない（BUG-0101）
        return False, self.resolve(name) or self.resolve_builtin(name) or self.resolve_star(name)

    def _is_local(self, name: str, node: Node) -> bool:
        return self._binding(name, node)[0] == "local"

    # --- 属性アクセスの解決 ---

    def _resolve_member(
        self, recv: Node | None, name: str, node: Node, chain: list[SymbolRecord]
    ) -> tuple[str | None, bool]:
        """``(URN, 受け手の型が分かったか)``。URN が ``None`` で型も分からないものが ``name_only`` の候補（BUG-0071）。

        受け手の型が構文から分からなければ（式の受け手〈``a.b``・``f()``・``x[0]``〉も、名前の受け手〈注釈の無い引数・ローカル・
        型の分からないモジュール直下の変数・クラスの外の ``self`` / ``this``〉も）、同じファイルの同名にも import した同名にも当てない。
        名前で結ぶか曖昧と書くかは問い合わせのとき（BUG-0071・BUG-0077 の近さ）に任せる（式の受け手は BUG-0079、名前の受け手は
        BUG-0085。``x.save()`` を同じファイルの唯一の ``Base.save`` に ``structural`` で結ぶと、``x`` が別のファイルのクラスでも
        確かな行に見える）。
        """
        if recv is None:
            return None, False
        whole = node.parent
        if whole is not None and whole.type == "attribute":
            module = self._dotted_module(whole, node)
            if module == NAMESPACE_PACKAGE:
                return None, True  # ``nsp.tools`` の ``tools`` は名前空間パッケージのディレクトリ: 参照でも外部でもない（BUG-0101）
            if module is not None:
                return module, True  # ``pkg.helpers`` の ``helpers`` はサブモジュールそのものへの参照（BUG-0093）
        ref = self._type_ref(recv, node, chain)
        if ref is not None:
            hit = self._member_of(ref, name)
            if hit is not None or ref[0] == "local":
                return hit, True  # 同じファイルのクラスと分かっているのに無いメンバーは、別のクラスの同名で埋めない
        return None, False

    def _super_receiver(
        self, recv: Node, node: Node, chain: list[SymbolRecord]
    ) -> tuple[bool, str | None]:
        """``(super の受け手か, 探し始めるクラスの URN)``（BUG-0081）。

        Python の ``super()``（囲みのクラス）と ``super(C, self)``（``C``）、TS / JS の ``super``（囲みのクラスのメソッド・フィールド
        初期化子の中。オブジェクトリテラルのメソッドの ``super`` はそのオブジェクトのプロトタイプなので受け手にしない）。
        ``super`` を自分で束ねた名前（引数・代入・import・同じファイルの定義）は組み込みの ``super`` ではない。クラスが分からない
        （``super(type(self), self)``・外部のクラス・クラスの外）ときは ``(True, None)``（結ばない。名前でも結ばない）。
        """
        if self.family == _PYTHON:
            if recv.type != "call":
                return False, None
            fn = recv.child_by_field_name("function")
            if fn is None or fn.type != "identifier" or self._text(fn) != "super":
                return False, None
            if self._binding("super", fn)[0] != "none" or "super" in self.by_qualified or self.resolve("super"):
                return False, None
            args = recv.child_by_field_name("arguments")
            named = [a for a in args.named_children if a.type != "comment"] if args is not None else []
            if not named:
                cls = self._class_node(node)
                ref = self._class_ref(cls) if cls is not None else None
            elif len(named) == 2 and named[0].type == "identifier":
                ref = self._name_ref(self._text(named[0]), chain, node)
            else:
                return True, None
        else:
            if recv.type != "super":
                return False, None
            cls = self._js_super_class(node)
            ref = self._class_ref(cls) if cls is not None else None
        return True, self._class_urn(ref)

    def _js_super_class(self, node: Node) -> Node | None:
        """TS / JS の ``super`` が指す基底を持つクラス（アロー関数は外側の ``super`` を使う）。"""
        anc = node.parent
        while anc is not None:
            if anc.type in self.classes:
                return anc
            if anc.type == "method_definition":
                body = anc.parent
                return body.parent if body is not None and body.type == "class_body" else None
            if anc.type in self.functions and anc.type != "arrow_function":
                return None  # function 式・宣言の中の super は構文エラー
            anc = anc.parent
        return None

    def _class_urn(self, ref: tuple[str, str] | None) -> str | None:
        """``("local", 修飾名)`` / ``("urn", URN)`` → ワークスペースのクラスの URN（外部・セレクタの無い URN は ``None``）。"""
        if ref is None:
            return None
        kind, value = ref
        if kind == "local":
            return self._local_urn(value)
        if value.startswith(_EXTERNAL_PREFIX) or "#" not in value:
            return None
        return value

    def _member_path(self, owner_urn: str | None, name: str) -> tuple[str, str] | None:
        """型 ``owner_urn`` のメンバー ``name`` の型を表す ``("urn", <型>.name)``（BUG-0113。整えが決める）。"""
        joined = _append_member(owner_urn, name) if owner_urn is not None else None
        return ("urn", joined) if joined is not None else None

    def _local_urn(self, qualified: str) -> str | None:
        try:
            return str(Urn(self.workspace_id, str(self.rel), CodeSel(qualified)))
        except ValueError:
            return None

    def _member_of(self, ref: tuple[str, str], name: str) -> str | None:
        """型 ``ref`` のメンバー ``name`` の URN。

        * ``("urn", 型)``（別ファイル・外部の型）: ``<型>.name``。実在するか、基底クラスから継承したものかは索引の整え
          （``index/inherited.py``）が決める（BUG-0075）。
        * ``("local", 修飾名)``（同じファイルのクラス）: クラス自身 → 同じファイルの基底クラスを C3 の順（``core/mro.py``）に探す。
          たどる途中に別ファイルの基底・この場で解決できない基底があれば、``<このクラス>.name`` を返して整えに任せる（索引の
          ``INHERITS`` 行をファイルをまたいでたどる）。中身の分からない外部の基底（``object``・``ABC`` などの素通りするもの以外）に
          先に当たったら ``None``（その基底が同名を定義しているかもしれないので、後ろの基底の同名に結ばない）。
        """
        kind, value = ref
        if kind != "local":
            return _append_member(value, name)
        hit = self.by_qualified.get(f"{value}.{name}")
        if hit is not None:
            return str(hit.urn)
        order = c3_linearize(("local", value), self._local_bases)
        if order is None:
            return None
        if any(k == "other" for k, _ in order):
            return self._requested_member(value, name)
        for k, v in order[1:]:
            if k == "ext":
                if v in TRANSPARENT_EXTERNAL_BASES:
                    continue
                return None
            hit = self.by_qualified.get(f"{v}.{name}")
            if hit is not None:
                return str(hit.urn)
        return None

    def _local_bases(self, entry: tuple[str, str]) -> list[tuple[str, str]]:
        """``_member_of`` の C3 に渡す直接の基底: ``("local", 修飾名)`` / ``("ext", 外部 URN)`` / ``("other", 綴り)``（別ファイルの
        クラス・この場で解決できない名前・``mod.Base`` の形。整えに任せる印）。外部と ``other`` は葉（基底を持たない）として扱う。"""
        kind, value = entry
        if kind != "local":
            return []
        out: list[tuple[str, str]] = []
        for base in self.class_bases.get(value, ()):
            ref = None if "." in base else self._name_ref(base, [])
            if ref is not None and ref[0] == "local":
                out.append(("local", ref[1]))
            elif ref is not None and ref[1].startswith(_EXTERNAL_PREFIX):
                out.append(("ext", ref[1]))
            else:
                out.append(("other", base))
        return out

    def _attr_type(self, class_qualified: str, attr: str) -> _TypeName | None:
        """同じファイルのクラス ``class_qualified`` の属性 ``attr`` の型名（注釈・property の戻り値・構築の呼び出し）。

        クラス自身 → 同じファイルの基底を C3 の順に見る。別ファイルの基底・この場で解決できない基底・中身の分からない外部の基底
        （素通りするもの以外）に先に当たったら ``None``（その基底が同名の属性を別の型で持つかもしれない）。
        """
        order = c3_linearize(("local", class_qualified), self._local_bases) or [("local", class_qualified)]
        for kind, value in order:
            if kind == "local":
                cid = self.class_ids.get(value)
                tname = self.scan.attr_types.get(cid, {}).get(attr) if cid is not None else None
                if tname is not None:
                    return tname
            elif not (kind == "ext" and value in TRANSPARENT_EXTERNAL_BASES):
                return None
        return None

    def _requested_member(self, class_qualified: str, name: str) -> str | None:
        """同じファイルのクラスのメンバーとして求めた URN（``<ファイル>#<クラス>.name``。シンボルが無ければ整えが基底をたどる）。"""
        try:
            return str(Urn(self.workspace_id, str(self.rel), CodeSel(f"{class_qualified}.{name}")))
        except ValueError:
            return None

    def _type_ref(self, recv: Node, node: Node, chain: list[SymbolRecord]) -> tuple[str, str] | None:
        """レシーバ式の型を ``("local", 修飾名)`` か ``("urn", URN)`` で。分からなければ ``None``。"""
        t = recv.type
        if t in ("identifier", "this"):
            text = self._text(recv)
            if text in _RECEIVER_SELF or t == "this":
                cls = self._class_node(node)
                if cls is None:
                    return None
                return self._class_ref(cls)
            tname = self._variable_type(text, node)
            if tname is not None:
                return self._tname_ref(tname, chain, node)
            if self._is_local(text, node):
                return None  # 型の分からないローカル変数（同名の import を隠す）
            return self._name_ref(
                text, chain, node
            )  # クラス名・モジュール名そのもの（``Foo.create()``、``mod.f()``）
        if t in _ATTRIBUTE_NODES:
            module = self._dotted_module(recv, node)
            if module is not None and module != NAMESPACE_PACKAGE:
                # ``import pkg.helpers`` の ``pkg.helpers.flush()``: 受け手はサブモジュール（``flush`` はその関数。BUG-0093）。
                # 名前空間パッケージのディレクトリ（``nsp.ext``）の先がワークスペースに無ければ、下の外部の連鎖（別の場所の部分）に倒す
                return ("urn", module)
            # ``a.b`` の型は ``b`` の型（``a`` の型ではない。BUG-0079）。構文から分かるときだけ決める
            obj = recv.child_by_field_name("object")
            prop = recv.child_by_field_name("attribute" if t == "attribute" else "property")
            if obj is None or prop is None or obj.type not in ("identifier", "this", *_ATTRIBUTE_NODES):
                return None
            inner = self._type_ref(obj, node, chain)
            if inner is None:
                return None
            if inner[0] == "local":
                # 同じファイルのクラス（``self`` の囲みのクラスを含む）の注釈つきの属性・``self.b: T = ...``・dataclass の
                # フィールド・戻り値の注釈つきの property（``self.b = T()`` もここに入る）。同じファイルの基底も C3 の順に見る
                tname = self._attr_type(inner[1], self._text(prop))
                if tname is not None:
                    return self._tname_ref(tname, chain, node)
                if self.family == _JS and inner[1] not in self.class_ids:
                    # TS: 同じファイルのインターフェースのプロパティ ``b`` の型は、メンバーの型 ``<型>.b``（整えがメンバーの型の構成
                    # から決める。決まらなければ名前だけ。BUG-0113）。クラスは上の属性の型で決まらなければ分からない（今までどおり）
                    return self._member_path(self._local_urn(inner[1]), self._text(prop))
                return None
            if inner[1].startswith(_EXTERNAL_PREFIX) or "#" not in inner[1]:
                # 外部モジュール・クラスの属性の連鎖（``os.path``、``Decimal.ROUND_HALF_UP``）と、ワークスペースのモジュールの名前
                # （``model.Capability.DEFINITION`` の ``model.Capability`` は ``model.py#Capability``。それが変数なら整えが型で結び、
                # シンボルでない〈再び公開した名前〉なら名前だけにする。BUG-0093・BUG-0094）
                joined = _append_member(inner[1], self._text(prop))
                return ("urn", joined) if joined is not None else None
            if self.family == _JS:
                # TS / JS: 別ファイルの型（クラス・インターフェース・型の別名・メンバーの型）の属性 ``b`` の型は、メンバーの型
                # ``<型>.b``（``o.retry.limit`` の ``o.retry``。整えがメンバーの型の構成から決め、決まらなければ名前だけ。BUG-0113）
                return self._member_path(inner[1], self._text(prop))
            return None  # 別ファイルのクラスの属性の型はこの場では分からない
        if t in ("call", "new_expression"):
            fn = recv.child_by_field_name("constructor" if t == "new_expression" else "function")
            if fn is not None and fn.type == "identifier":
                text = self._text(fn)
                if t == "new_expression" or self._looks_like_class(text):
                    return self._name_ref(text, chain, node)
        return None

    def _dotted_module(self, recv: Node, node: Node) -> str | None:
        """受け手 ``recv`` が点つきのモジュールのパス（``pkg.helpers``・``a.b.c``）なら、そのサブモジュールのファイルの URN（BUG-0093）。

        Python だけ。識別子だけの連鎖で、先頭の名前がモジュール直下の import が束ねた名前（ローカル・関数の中の import・同じ
        ファイルの定義に隠されていない）のとき、``resolve_dotted`` に引く（索引器が import 表とファイルの在り処から決める）。
        """
        if self._resolve_dotted_raw is None or self.family != _PYTHON:
            return None
        parts: list[str] = []
        cur = recv
        while cur.type == "attribute":
            attr = cur.child_by_field_name("attribute")
            obj = cur.child_by_field_name("object")
            if attr is None or obj is None or attr.type != "identifier":
                return None
            parts.append(self._text(attr))
            cur = obj
        if cur.type != "identifier" or not parts:
            return None
        head = self._text(cur)
        if head in _RECEIVER_SELF or head in self.by_qualified or self._binding(head, node)[0] != "none":
            return None
        dotted = ".".join([head, *reversed(parts)])
        if dotted not in self._dotted_cache:
            self._dotted_cache[dotted] = self._resolve_dotted_raw(dotted)
        return self._dotted_cache[dotted]

    def _namespace_name(self, name: str) -> bool:
        """モジュール直下の import が名前空間パッケージ（``__init__.py`` の無いワークスペースのディレクトリ）を束ねた名前か（BUG-0101）。"""
        if self._resolve_dotted_raw is None or self.family != _PYTHON:
            return False
        if name not in self._dotted_cache:
            self._dotted_cache[name] = self._resolve_dotted_raw(name)
        return self._dotted_cache[name] == NAMESPACE_PACKAGE

    def _looks_like_class(self, name: str) -> bool:
        local = self.by_qualified.get(name)
        if local is not None:
            return local.kind in _CLASS_KINDS
        return name[:1].isupper()

    def _name_ref(
        self, name: str, chain: list[SymbolRecord], node: Node | None = None
    ) -> tuple[str, str] | None:
        """型名・モジュール名 ``name``（``node`` の位置から見える束縛）を ``("local", 修飾名)`` か ``("urn", URN)`` に。"""
        kind, key = self._binding(name, node) if node is not None else ("none", None)
        if kind == "import" and key is not None:
            urn = self.resolve_scoped(key, name)
            return ("urn", urn) if urn is not None else None
        local = self.by_qualified.get(name)
        if local is not None and local.kind in _CLASS_KINDS | {Kind.MODULE}:
            return ("local", local.qualified_name)
        if local is not None and local.kind is Kind.TYPE and self.family == _JS:
            # 同じファイルの TS の型の別名: メンバーは整え（index/inherited.py）が別名の構成（_type_aliases）から決める（BUG-0108）
            return ("urn", str(local.urn))
        urn = self.resolve(name) or self.resolve_builtin(name) or self.resolve_star(name)
        if urn is not None:
            return ("urn", urn)
        return None

    def _class_node(self, node: Node) -> Node | None:
        for anc in self._scopes(node):
            if anc.type in self.classes:
                return anc
        return None

    def _class_ref(self, cls: Node) -> tuple[str, str] | None:
        name = cls.child_by_field_name("name")
        if name is None:
            return None
        sym = self.by_selection.get((self.pos(name).line, self.pos(name).col))
        return ("local", sym.qualified_name) if sym is not None else None

    def _variable_type(self, name: str, node: Node) -> _TypeName | None:
        for anc in self._scopes(node):  # 関数・クラス・根
            got = self.scan.var_types.get(anc.id, {}).get(name)
            if got is not None:
                return got
            if anc.type in self.functions and (
                name in self.scan.locals.get(anc.id, ()) or name in self.scan.imports.get(anc.id, {})
            ):
                return None  # 型の分からないローカル（import を含む）が外側の同名変数を隠す
        return None

    # --- 走査: 束縛と型 ---

    def _scan_tree(self, root: Node) -> None:
        if self.family == _PYTHON:
            self._scan_python(root)
        else:
            self._scan_js(root)

    def _bind(self, node: Node, fn: Node | None) -> None:
        name = self._text(node)
        if fn is not None and name in self.scan.globals.get(fn.id, ()):
            return
        self.scan.binders.add(node.id)
        if fn is not None:
            self.scan.locals.setdefault(fn.id, set()).add(name)

    def _set_var_type(self, scope: Node, name: str, tname: _TypeName | None) -> None:
        if tname:
            self.scan.var_types.setdefault(scope.id, {}).setdefault(name, tname)

    def _set_attr_type(self, cls: Node | None, name: str, tname: _TypeName | None) -> None:
        if cls is not None and tname:
            self.scan.attr_types.setdefault(cls.id, {}).setdefault(name, tname)

    # Python

    def _py_targets(self, node: Node | None) -> Iterator[Node]:
        if node is None:
            return
        if node.type == "identifier":
            yield node
        elif node.type in (
            "pattern_list",
            "tuple_pattern",
            "list_pattern",
            "list_splat_pattern",
            "tuple",
            "list",
        ):
            for c in node.named_children:
                yield from self._py_targets(c)

    def _py_ann_name(self, node: Node | None) -> str | None:
        """注釈の最初の型名（``Optional`` などの包みは飛ばす）。前方参照の文字列（``x: "Foo"``・``Optional["Foo"]``）は中身が
        名前 1 つのときだけ読む（型の分からない名前の受け手を同じファイルの同名に当てなくなったので、注釈を書いた受け手は型で結ぶ。BUG-0085）。"""
        if node is None:
            return None
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                name: str | None = self._text(n)
            elif n.type == "attribute":
                attr = n.child_by_field_name("attribute")
                name = self._text(attr) if attr is not None else None
            elif n.type == "string":
                parts = [c for c in n.named_children if c.type == "string_content"]
                text = self._text(parts[0]).strip() if len(parts) == 1 and len(n.named_children) == 3 else ""
                name = text if text.isidentifier() else None
            else:
                if n.type != "comment":
                    stack.extend(reversed(n.children))
                continue
            if name and name not in _ANNOTATION_SKIP:
                return name
        return None

    def _py_value_type(self, value: Node | None, scope: Node | None) -> _TypeName | None:
        if value is None:
            return None
        if value.type == "await":
            inner = value.named_children[0] if value.named_children else None
            return self._py_value_type(inner, scope)
        if value.type == "call":
            fn = value.child_by_field_name("function")
            if fn is not None and fn.type == "identifier" and self._looks_like_class(self._text(fn)):
                return self._text(fn)
        if value.type == "identifier" and scope is not None:
            return self.scan.var_types.get(scope.id, {}).get(self._text(value))
        return None

    def _scan_python(self, root: Node) -> None:
        # スタック要素: (ノード, 囲む関数, 囲むスコープ〈関数・クラス・ルート〉, 囲むクラス)
        stack: list[tuple[Node, Node | None, Node, Node | None]] = [(root, None, root, None)]
        while stack:
            node, fn, scope, cls = stack.pop()
            cooperate()
            t = node.type
            child_fn, child_scope, child_cls = fn, scope, cls
            if t == "function_definition":
                name = node.child_by_field_name("name")
                if name is not None:
                    self._bind(name, fn)
                    if cls is not None and scope.id == cls.id and self._py_is_property(node):
                        # 戻り値の注釈つきの property は属性として型を持つ（``a.b.m()`` の ``a.b``。BUG-0079）
                        self._set_attr_type(
                            cls, self._text(name), self._py_ann_name(node.child_by_field_name("return_type"))
                        )
                params = node.child_by_field_name("parameters")
                child_fn = child_scope = node
                if params is not None:
                    self._py_params(params, node)
            elif t == "lambda":
                child_fn = child_scope = node
                params = node.child_by_field_name("parameters")
                if params is not None:
                    self._py_params(params, node)
            elif t == "class_definition":
                name = node.child_by_field_name("name")
                if name is not None:
                    self._bind(name, fn)
                    self._record_bases(name, self._py_base_names(node))
                    self._record_class(name, node)
                child_scope = child_cls = node
            elif t == "assignment":
                self._py_assignment(node, fn, scope, cls)
            elif t in ("for_statement", "for_in_clause"):
                for n in self._py_targets(node.child_by_field_name("left")):
                    self._bind(n, fn)
            elif t == "named_expression":
                n = node.child_by_field_name("name")
                if n is not None:
                    self._bind(n, fn)
            elif t == "as_pattern_target":
                for n in node.named_children:
                    if n.type == "identifier":
                        self._bind(n, fn)
            elif t in ("import_statement", "import_from_statement"):
                if scope.parent is not None:  # def / class の中（モジュール直下は名前表がそのまま使える）
                    self._py_scoped_import(node, scope)
            elif t == "global_statement" and fn is not None:
                for n in node.named_children:
                    self.scan.globals.setdefault(fn.id, set()).add(self._text(n))
            for c in reversed(node.children):
                stack.append((c, child_fn, child_scope, child_cls))

    def _py_scoped_import(self, stmt: Node, scope: Node) -> None:
        """``def`` / ``class`` の中の import 文が、そのスコープで束縛する名前を控える（ローカル変数とは別に。使用箇所は import の先を指す）。"""
        p = self.pos(stmt)
        key = (p.line, p.col)
        self.scan.nested_imports.add(key)
        table = self.scan.imports.setdefault(scope.id, {})
        for n in stmt.children_by_field_name("name"):
            bound = n.child_by_field_name("alias") if n.type == "aliased_import" else n
            if bound is None:
                continue
            first = bound if bound.type == "identifier" else (bound.named_children or [None])[0]
            if first is None:
                continue
            name = self._text(first)
            if name in self.scan.globals.get(scope.id, ()):
                continue  # ``global X`` + ``import X``: モジュールの名前を束ねる
            table[name] = key

    def _record_class(self, name_node: Node, cls: Node) -> None:
        position = self.pos(name_node)
        sym = self.by_selection.get((position.line, position.col))
        if sym is not None:
            self.class_ids[sym.qualified_name] = cls.id  # 同じ修飾名はあとの定義が勝つ（``by_qualified`` と同じ）

    def _py_is_property(self, fn: Node) -> bool:
        """``@property`` / ``@functools.cached_property`` / ``@cached_property`` で飾った関数か。"""
        parent = fn.parent
        if parent is None or parent.type != "decorated_definition":
            return False
        for dec in (c for c in parent.named_children if c.type == "decorator"):
            expr = dec.named_children[0] if dec.named_children else None
            if expr is not None and expr.type in ("identifier", "attribute"):
                if self._text(expr).rsplit(".", 1)[-1] in ("property", "cached_property"):
                    return True
        return False

    def _record_bases(self, name_node: Node, bases: list[str]) -> None:
        position = self.pos(name_node)
        sym = self.by_selection.get((position.line, position.col))
        if sym is not None and bases:
            self.class_bases.setdefault(sym.qualified_name, bases)

    def _py_base_names(self, cls: Node) -> list[str]:
        """基底クラスの綴り（書いた順）。``mod.Base`` は点つきのまま（``_local_bases`` が整えに任せる印にする）。"""
        out: list[str] = []
        sup = cls.child_by_field_name("superclasses")
        for c in sup.named_children if sup is not None else ():
            if c.type in ("identifier", "attribute"):
                out.append(self._text(c))
            elif c.type == "subscript":
                value = c.child_by_field_name("value")
                if value is not None and value.type in ("identifier", "attribute"):
                    out.append(self._text(value))
        return out

    def _js_base_names(self, cls: Node) -> list[str]:
        """``extends`` の基底クラス（TS の ``implements`` は入れない。実装を持たないので継承したメンバーの探し先にならない）。"""
        out: list[str] = []
        for heritage in (c for c in cls.children if c.type == "class_heritage"):
            for clause in heritage.named_children:
                if clause.type == "implements_clause":
                    continue
                value = clause.child_by_field_name("value") if clause.type == "extends_clause" else clause
                if value is not None and value.type in ("identifier", "member_expression"):
                    out.append(self._text(value))
        return out

    def _ts_interface_base_names(self, node: Node) -> list[str]:
        """TS の ``interface I extends A, ns.B<T>`` の基底（書いた順）。"""
        out: list[str] = []
        for clause in (c for c in node.named_children if c.type == "extends_type_clause"):
            for t in clause.named_children:
                if t.type == "generic_type":
                    t = t.child_by_field_name("name") or t
                if t.type in ("type_identifier", "nested_type_identifier"):
                    out.append(self._text(t))
        return out

    def _py_params(self, params: Node, fn: Node) -> None:
        for p in params.named_children:
            if p.type == "identifier":
                self._bind(p, fn)
            elif p.type == "typed_parameter":
                first = next((c for c in p.named_children if c.type == "identifier"), None)
                if first is not None:
                    self._bind(first, fn)
                    self._set_var_type(fn, self._text(first), self._py_ann_name(p.child_by_field_name("type")))
            elif p.type in ("default_parameter", "typed_default_parameter"):
                n = p.child_by_field_name("name")
                if n is not None:
                    self._bind(n, fn)
                    self._set_var_type(fn, self._text(n), self._py_ann_name(p.child_by_field_name("type")))
            elif p.type in ("list_splat_pattern", "dictionary_splat_pattern"):
                for n in p.named_children:
                    if n.type == "identifier":
                        self._bind(n, fn)

    def _py_assignment(self, node: Node, fn: Node | None, scope: Node, cls: Node | None) -> None:
        left = node.child_by_field_name("left")
        if left is None:
            return
        tname = self._py_ann_name(node.child_by_field_name("type")) or self._py_value_type(
            node.child_by_field_name("right"), fn
        )
        if left.type == "identifier":
            self._bind(left, fn)
            self._set_var_type(scope, self._text(left), tname)
            if scope is not fn and cls is not None and scope.id == cls.id:
                self._set_attr_type(
                    cls, self._text(left), tname
                )  # クラス直下の ``x: T``（dataclass のフィールドなど）
        elif left.type in ("pattern_list", "tuple_pattern", "list_pattern"):
            for n in self._py_targets(left):
                self._bind(n, fn)
            for n in left.named_children:  # ``self.a, self.b = ...``
                if n.type == "attribute":
                    self._py_self_attr(n, fn, cls)
        elif left.type == "attribute":
            attr = self._py_self_attr(left, fn, cls)
            if attr is not None:
                self._set_attr_type(cls, self._text(attr), tname)

    def _py_self_attr(self, left: Node, fn: Node | None, cls: Node | None) -> Node | None:
        """``self.x``（``cls.x``）の左辺なら ``x`` の名前ノード。メソッドの中ならインスタンス属性の定義として控える（BUG-0095）。"""
        obj = left.child_by_field_name("object")
        attr = left.child_by_field_name("attribute")
        if obj is None or attr is None or obj.type != "identifier" or self._text(obj) not in _RECEIVER_SELF:
            return None
        if fn is not None and cls is not None:
            self.scan.instance_attrs.setdefault(cls.id, {}).setdefault(self._text(attr), attr)
        return attr

    # TypeScript / JavaScript

    def _js_targets(self, node: Node | None) -> Iterator[Node]:
        if node is None:
            return
        t = node.type
        if t in ("identifier", "shorthand_property_identifier_pattern"):
            yield node
        elif t == "object_pattern":
            for c in node.named_children:
                if c.type == "pair_pattern":
                    yield from self._js_targets(c.child_by_field_name("value"))
                else:
                    yield from self._js_targets(c)
        elif t == "array_pattern":
            for c in node.named_children:
                yield from self._js_targets(c)
        elif t == "rest_pattern":
            for c in node.named_children:
                yield from self._js_targets(c)
        elif t in ("assignment_pattern", "object_assignment_pattern"):  # 後者は ``{ foo = 1 }`` の ``foo``
            yield from self._js_targets(node.child_by_field_name("left"))

    def _js_ann(self, node: Node | None) -> _TsType | None:
        """TS の型注釈 → 受け手の型（``_ts_type``。前は最初の型の名前に縮めていた: ``Opts['retry']`` も ``Opts[]`` も ``Opts``。BUG-0113）。"""
        return self._ts_type(node, self._type_params(node)) if node is not None else None

    def _js_value_type(self, value: Node | None, fn: Node | None) -> _TypeName | None:
        if value is None:
            return None
        if value.type == "await_expression":
            inner = value.named_children[0] if value.named_children else None
            return _awaited(self._js_value_type(inner, fn))  # ``await p``（``p: Promise<T>``）は T（BUG-0113）
        if value.type == "as_expression" and value.named_child_count >= 2:
            # ``x as Foo`` は Foo（``as const`` は型の子を持たない）
            return self._ts_type(value.named_children[-1], self._type_params(value))
        if value.type in ("parenthesized_expression", "as_expression", "satisfies_expression"):
            inner = value.named_children[0] if value.named_children else None
            return self._js_value_type(inner, fn)
        if value.type == "new_expression":
            ctor = value.child_by_field_name("constructor")
            if ctor is not None and ctor.type == "identifier":
                return self._text(ctor)
        if value.type == "identifier" and fn is not None:
            return self.scan.var_types.get(fn.id, {}).get(self._text(value))
        return None

    def _scan_js(self, root: Node) -> None:
        stack: list[tuple[Node, Node | None, Node, Node | None]] = [(root, None, root, None)]
        while stack:
            node, fn, scope, cls = stack.pop()
            cooperate()
            t = node.type
            child_fn, child_scope, child_cls = fn, scope, cls
            if t in self.functions:
                if t in ("function_declaration", "generator_function_declaration"):
                    name = node.child_by_field_name("name")
                    if name is not None:
                        self._bind(name, fn)
                elif t == "method_definition" and cls is not None and any(c.type == "get" for c in node.children):
                    # 戻り値の注釈つきの getter は属性として型を持つ（``a.b.m()`` の ``a.b``。BUG-0079）
                    name = node.child_by_field_name("name")
                    if name is not None:
                        self._set_attr_type(
                            cls,
                            self._text(name),
                            self._member_type(
                                node.child_by_field_name("return_type"), self._class_qualified(cls), self._text(name)
                            ),
                        )
                child_fn = child_scope = node
                params = node.child_by_field_name("parameters")
                if params is not None:
                    self._js_params(params, node, cls)
                single = node.child_by_field_name("parameter")
                if single is not None and single.type == "identifier":
                    self._bind(single, node)
            elif t in self.classes:
                name = node.child_by_field_name("name")
                if name is not None:
                    if name.type == "identifier":
                        self._bind(name, fn)
                    self._record_bases(name, self._js_base_names(node))
                    self._record_class(name, node)
                child_scope = child_cls = node
            elif t == "interface_declaration":
                name = node.child_by_field_name("name")
                if name is not None:
                    self._record_bases(name, self._ts_interface_base_names(node))
            elif t == "type_alias_declaration":
                self.alias_nodes.append(node)
            elif t == "property_signature":
                if node.child_by_field_name("type") is not None:
                    self.member_nodes.append(node)  # メンバーの型の構成（BUG-0113）
            elif t == "variable_declarator":
                name = node.child_by_field_name("name")
                if name is not None:
                    imported = (
                        js_require_bindings(node, self.parsed.source) if fn is not None else []
                    )
                    for n in self._js_targets(name):
                        if fn is not None and self._text(n) in imported:
                            self._js_scoped_require(node, n, fn)
                        else:
                            self._bind(n, fn)
                    if name.type == "identifier":
                        annotation = node.child_by_field_name("type")
                        tname = (
                            self._js_ann(annotation)
                            if annotation is not None
                            else self._js_value_type(node.child_by_field_name("value"), fn)
                        )
                        self._set_var_type(scope, self._text(name), tname)
            elif t in ("public_field_definition", "field_definition"):
                name = node.child_by_field_name("name") or node.child_by_field_name("property")
                annotation = node.child_by_field_name("type")
                if annotation is not None and t == "public_field_definition":
                    self.member_nodes.append(node)  # メンバーの型の構成（BUG-0113）
                if name is not None and cls is not None:
                    tname = (
                        self._member_type(annotation, self._class_qualified(cls), self._text(name))
                        if annotation is not None
                        else self._js_value_type(node.child_by_field_name("value"), fn)
                    )
                    self._set_attr_type(cls, self._text(name), tname)
            elif t == "assignment_expression":
                left = node.child_by_field_name("left")
                if left is not None and left.type == "member_expression":
                    obj = left.child_by_field_name("object")
                    prop = left.child_by_field_name("property")
                    if obj is not None and prop is not None and obj.type == "this":
                        self._set_attr_type(
                            cls, self._text(prop), self._js_value_type(node.child_by_field_name("right"), fn)
                        )
                        if fn is not None and cls is not None:  # インスタンス属性の定義（BUG-0095）
                            self.scan.instance_attrs.setdefault(cls.id, {}).setdefault(self._text(prop), prop)
            elif t == "catch_clause":
                for n in self._js_targets(node.child_by_field_name("parameter")):
                    self._bind(n, fn)
            elif t == "for_in_statement":
                for n in self._js_targets(node.child_by_field_name("left")):
                    self._bind(n, fn)
            for c in reversed(node.children):
                stack.append((c, child_fn, child_scope, child_cls))

    def _js_scoped_require(self, declarator: Node, name_node: Node, fn: Node) -> None:
        """関数の中の ``const { foo } = require("x")`` が束縛する ``foo`` を控える（ローカル変数とは別に。使用箇所は ``x`` の先を指す）。

        キーは宣言文（``const ...``）の開始位置。``extract_imports`` が ``require`` の ``ImportFact.range`` に使う文と同じ。
        """
        decl = declarator.parent or declarator
        p = self.pos(decl)
        self.scan.binders.add(name_node.id)
        self.scan.imports.setdefault(fn.id, {})[self._text(name_node)] = (p.line, p.col)

    def _js_params(self, params: Node, fn: Node, cls: Node | None) -> None:
        for p in params.named_children:
            if p.type in ("required_parameter", "optional_parameter"):
                pattern = p.child_by_field_name("pattern")
                names = list(self._js_targets(pattern))
                for n in names:
                    self._bind(n, fn)
                tname = self._js_ann(p.child_by_field_name("type"))
                if pattern is not None and pattern.type == "identifier":
                    self._set_var_type(fn, self._text(pattern), tname)
                    if any(
                        c.type in ("accessibility_modifier", "override_modifier") for c in p.children
                    ) or any(c.type == "readonly" for c in p.children):
                        self._set_attr_type(
                            cls, self._text(pattern), tname
                        )  # コンストラクタのパラメータプロパティ
            else:
                for n in self._js_targets(p):
                    self._bind(n, fn)


def _neg_pos(p: Position) -> tuple[int, int]:
    return (-p.line, -p.col)


def _awaited(tname: _TypeName | None) -> _TypeName | None:
    """``await`` した値の型: ``Promise<T>`` / ``PromiseLike<T>`` は ``T``（型引数の無い Promise は分からない）、それ以外はそのまま。"""
    if isinstance(tname, _TsType) and (tname.promised is not None or (tname.name in _TS_PROMISES and not tname.path)):
        return tname.promised
    return tname


def _append_member(urn_text: str, member: str) -> str | None:
    """``urn_text`` の修飾名に ``.member`` を足した URN（ファイル URN なら ``#member``）。"""
    try:
        urn = parse(urn_text)
    except ValueError:
        return None
    sel = urn.selector
    if sel is None:
        return str(urn.with_selector(CodeSel(member)))
    if isinstance(sel, CodeSel):
        return str(urn.with_selector(CodeSel(f"{sel.qualified}.{member}")))
    return None
