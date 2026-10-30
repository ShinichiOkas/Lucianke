"""言語の組み込み名の固定の一覧。環境の版で索引が変わらないように、実行時に ``dir(builtins)`` などを読まず固定する。

``syntax/relations.py`` が、どこにも定義・import されていない素の名前を外部シンボルに解決するのに使う
（``class A(Exception)`` の ``INHERITS``、``raise ValueError(...)`` の ``CALLS``、TS の ``new Error(...)``・``Promise<T>`` など。
仕様書 §9.3、設計書 §7.4.6）。

* Python: ``lk://_ext/builtins#<名前>``。``dir(builtins)`` から site が足す ``copyright`` / ``credits`` / ``license`` と
  キーワード（``True`` / ``False`` / ``None``）を除いたもの（Python 3.13 で採取）。
* TypeScript / JavaScript: ``lk://_ext/globals#<名前>``。ECMAScript の標準グローバル、``console``・タイマー、TS の標準ユーティリティ型と
  ``lib.es*.d.ts`` の主な型。DOM（``document``・``window``・``fetch`` など）と Node（``process``・``Buffer``・``require``）は含まない。
"""

from __future__ import annotations

__all__ = [
    "BUILTINS_PACKAGE",
    "GLOBALS_PACKAGE",
    "JS_BUILTIN_ATTRIBUTES",
    "JS_BUILTIN_METHODS",
    "JS_GLOBALS",
    "PYTHON_BUILTINS",
    "PYTHON_BUILTIN_ATTRIBUTES",
    "PYTHON_BUILTIN_METHODS",
    "is_builtin_attribute",
]

BUILTINS_PACKAGE = "builtins"
GLOBALS_PACKAGE = "globals"

_PYTHON_NAMES = """
    ArithmeticError AssertionError AttributeError BaseException BaseExceptionGroup
    BlockingIOError BrokenPipeError BufferError BytesWarning ChildProcessError
    ConnectionAbortedError ConnectionError ConnectionRefusedError ConnectionResetError
    DeprecationWarning EOFError Ellipsis EncodingWarning EnvironmentError Exception
    ExceptionGroup FileExistsError FileNotFoundError FloatingPointError FutureWarning
    GeneratorExit IOError ImportError ImportWarning IndentationError IndexError InterruptedError
    IsADirectoryError KeyError KeyboardInterrupt LookupError MemoryError ModuleNotFoundError
    NameError NotADirectoryError NotImplemented NotImplementedError OSError OverflowError
    PendingDeprecationWarning PermissionError ProcessLookupError PythonFinalizationError
    RecursionError ReferenceError ResourceWarning RuntimeError RuntimeWarning StopAsyncIteration
    StopIteration SyntaxError SyntaxWarning SystemError SystemExit TabError TimeoutError
    TypeError UnboundLocalError UnicodeDecodeError UnicodeEncodeError UnicodeError
    UnicodeTranslateError UnicodeWarning UserWarning ValueError Warning WindowsError
    ZeroDivisionError abs aiter all anext any ascii bin bool breakpoint bytearray bytes callable
    chr classmethod compile complex delattr dict dir divmod enumerate eval exec exit filter
    float format frozenset getattr globals hasattr hash help hex id input int isinstance
    issubclass iter len list locals map max memoryview min next object oct open ord pow print
    property quit range repr reversed round set setattr slice sorted staticmethod str sum super
    tuple type vars zip
"""

_JS_NAMES = """
    AggregateError Array ArrayBuffer ArrayLike AsyncGenerator AsyncIterable
    AsyncIterableIterator AsyncIterator Atomics Awaited BigInt BigInt64Array BigUint64Array
    Boolean Capitalize ConstructorParameters DataView Date Error EvalError Exclude Extract
    FinalizationRegistry Float32Array Float64Array Function Generator Infinity InstanceType
    Int16Array Int32Array Int8Array Intl Iterable IterableIterator Iterator JSON Lowercase Map
    Math NaN NonNullable Number Object Omit OmitThisParameter Parameters Partial Pick Promise
    PromiseLike PropertyKey Proxy RangeError Readonly ReadonlyArray ReadonlyMap ReadonlySet
    Record ReferenceError Reflect RegExp Required ReturnType Set SharedArrayBuffer String Symbol
    SyntaxError TemplateStringsArray ThisParameterType ThisType TypeError URIError Uint16Array
    Uint32Array Uint8Array Uint8ClampedArray Uncapitalize Uppercase WeakMap WeakRef WeakSet
    clearImmediate clearInterval clearTimeout console decodeURI decodeURIComponent encodeURI
    encodeURIComponent globalThis isFinite isNaN parseFloat parseInt queueMicrotask setImmediate
    setInterval setTimeout structuredClone
"""

PYTHON_BUILTINS: frozenset[str] = frozenset(_PYTHON_NAMES.split())
JS_GLOBALS: frozenset[str] = frozenset(_JS_NAMES.split())

# 組み込み型のメソッド名（BUG-0071）。受け手の型が分からない ``x.name(...)`` は、この名前なら組み込み型（``list.append``・
# ``dict.get``・``Array.push`` など）の呼び出しでもありうるので、ワークスペースに同名のメソッドが 1 つしか無くても名前では結ばない
# （``tools/_name_calls.py``）。Python は str / bytes / bytearray / list / tuple / dict / set / frozenset / int / float / complex /
# memoryview / io の基底クラスの公開メソッド（Python 3.13 の ``dir()`` から採取。属性は除く）。TS / JS は Array / String / Map / Set /
# Promise / Object.prototype / Number / RegExp / Function / Date の主なメソッド。
_PYTHON_METHOD_NAMES = """
    add append as_integer_ratio bit_count bit_length capitalize casefold cast center clear close
    conjugate copy count decode detach difference difference_update discard encode endswith
    expandtabs extend fileno find flush format format_map from_bytes fromhex fromkeys get hex index
    insert intersection intersection_update is_integer isalnum isalpha isascii isatty isdecimal
    isdigit isdisjoint isidentifier islower isnumeric isprintable isspace issubset issuperset
    istitle isupper items join keys ljust lower lstrip maketrans partition pop popitem read
    read1 readable readall readinto readinto1 readline readlines release remove removeprefix
    removesuffix replace reverse rfind rindex rjust rpartition rsplit rstrip seek seekable
    setdefault sort split splitlines startswith strip swapcase symmetric_difference
    symmetric_difference_update tell title to_bytes tobytes tolist toreadonly translate truncate
    union update upper values writable write writelines zfill
"""

_JS_METHOD_NAMES = """
    add apply at bind call catch charAt charCodeAt clear codePointAt concat copyWithin delete
    difference endsWith entries every exec fill filter finally find findIndex findLast
    findLastIndex flat flatMap forEach get getTime has hasOwnProperty includes indexOf
    intersection isDisjointFrom isPrototypeOf isSubsetOf isSupersetOf isWellFormed join keys
    lastIndexOf localeCompare map match matchAll normalize padEnd padStart pop
    propertyIsEnumerable push reduce reduceRight repeat replace replaceAll reverse search set
    shift slice some sort splice split startsWith substr substring symmetricDifference test then
    toExponential toFixed toISOString toJSON toLocaleLowerCase toLocaleString toLocaleUpperCase
    toLowerCase toPrecision toReversed toSorted toSpliced toString toUpperCase toWellFormed trim
    trimEnd trimStart union unshift valueOf values with
"""

PYTHON_BUILTIN_METHODS: frozenset[str] = frozenset(_PYTHON_METHOD_NAMES.split())
JS_BUILTIN_METHODS: frozenset[str] = frozenset(_JS_METHOD_NAMES.split())

# 組み込み型の属性の名前（BUG-0095）。受け手の型が分からない属性の読み書き ``x.name`` は、この名前（と組み込み型のメソッド名。
# ``x.append`` を渡すのはメソッドの参照）なら組み込み型の属性でもありうるので、ワークスペースに同名の属性が 1 つしか無くても名前では
# 結ばない（``tools/_name_calls.py``）。Python は str / bytes / list / dict / set / int / float / complex / memoryview / range / slice /
# 例外（BaseException・OSError・SyntaxError・UnicodeDecodeError・StopIteration・SystemExit・ImportError）/ io のファイル /
# 関数・メソッド・モジュール・type・property の公開のデータ属性（Python 3.12 の ``dir()`` から採取。メソッドは除く）。``__x__`` の
# 名前はすべて組み込みのプロトコルとして扱う（``is_builtin_attribute``）。TS / JS は Array / String / Map / Set / Error / RegExp /
# ArrayBuffer / Function / Symbol / 反復の結果（``done`` / ``value``）の属性。
_PYTHON_ATTRIBUTE_NAMES = """
    args buffer c_contiguous characters_written closed closefd code contiguous denominator encoding end
    end_lineno end_offset errno errors f_contiguous fdel fget filename filename2 format fset imag itemsize
    line_buffering lineno mode msg name name_from nbytes ndim newlines numerator obj object offset path
    print_file_and_line raw readonly real reason shape start step stop strerror strides suboffsets text value
    winerror write_through
"""

_JS_ATTRIBUTE_NAMES = """
    buffer byteLength byteOffset cause constructor description done dotAll flags global hasIndices
    ignoreCase lastIndex length message multiline name prototype size source stack sticky unicode value
"""

PYTHON_BUILTIN_ATTRIBUTES: frozenset[str] = frozenset(_PYTHON_ATTRIBUTE_NAMES.split()) | PYTHON_BUILTIN_METHODS
JS_BUILTIN_ATTRIBUTES: frozenset[str] = frozenset(_JS_ATTRIBUTE_NAMES.split()) | JS_BUILTIN_METHODS


def is_builtin_attribute(name: str, family: str) -> bool:
    """``x.name``（読み書き）の ``name`` が組み込み型の属性・メソッドの名前か（``family`` は ``"python"`` / ``"js"``）。"""
    if family == "python":
        return name in PYTHON_BUILTIN_ATTRIBUTES or (name.startswith("__") and name.endswith("__"))
    return name in JS_BUILTIN_ATTRIBUTES
