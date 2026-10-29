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

__all__ = ["BUILTINS_PACKAGE", "GLOBALS_PACKAGE", "JS_GLOBALS", "PYTHON_BUILTINS"]

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
