"""syntax.support（言語の登録表）と syntax.parsers（TreeCache）のテスト（設計書 §7.4.1・§7.4.2、S-02）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
import tree_sitter

from lucianke.core.errors import ErrorCode, LuciankeError
from lucianke.core.limits import MAX_FILE_BYTES
from lucianke.core.model import Position, Range, RelPath, Tier
from lucianke.core.textpos import BOM, IndentSpec
from lucianke.syntax import parsers
from lucianke.syntax.parsers import TreeCache, estimate_tree_bytes, parse_source
from lucianke.syntax.support import (
    ADAPTERS,
    SUPPORT,
    LanguageSpec,
    build_registry,
    get_support,
    language_for_extension,
    unavailable_languages,
)
from lucianke.syntax.types import LanguageQueries

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# 設計書 §7.4.1 の表: M1 で必須の言語と、その拡張子
M1_REQUIRED: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi"),
    "typescript": (".ts", ".mts", ".cts"),
    "tsx": (".tsx",),
    "javascript": (".js", ".mjs", ".cjs", ".jsx"),
    "json": (".json",),
    "yaml": (".yml", ".yaml"),
    "toml": (".toml",),
    "xml": (".xml", ".svg"),
    "markdown": (".md",),
}
BEST_EFFORT = ("rust", "go", "c", "cpp", "c_sharp", "rst", "sql")


def rp(s: str) -> RelPath:
    return RelPath(s)


def wait_for_waiters(cache: TreeCache, n: int, timeout: float = 20.0) -> None:
    """``n`` 本が進行中の解析を待つ状態になるまで、呼んだスレッド（解析する側）を止める。"""
    end = time.monotonic() + timeout
    while cache.stats().waits < n:
        assert time.monotonic() < end, "waiters never arrived"
        time.sleep(0.005)


def py_src(n: int) -> bytes:
    """n 番目だけ違う、小さな正しい Python。"""
    return f"def f{n}(x):\n    return x + {n}\n".encode()


# --- 登録表 -------------------------------------------------------------------------------


def test_m1_required_languages_are_registered() -> None:
    unavailable = unavailable_languages()
    for lid in M1_REQUIRED:
        assert lid in SUPPORT, f"{lid}: {unavailable.get(lid)}"
        assert lid not in unavailable
        assert SUPPORT[lid].language_id == lid


def test_best_effort_languages_are_registered_or_explained() -> None:
    unavailable = unavailable_languages()
    for lid in BEST_EFFORT:
        assert (lid in SUPPORT) != (lid in unavailable), lid  # 登録されたか、外した理由が返るかのどちらか一方
    assert set(SUPPORT).isdisjoint(unavailable)


def test_every_grammar_loader_returns_a_language() -> None:
    for lid, spec in SUPPORT.items():
        lang = spec.grammar_loader()
        assert isinstance(lang, tree_sitter.Language), lid
        assert spec.grammar() is spec.grammar(), lid  # 2 回目以降は同じ Language（読み込み直さない）
        lo, hi = tree_sitter.MIN_COMPATIBLE_LANGUAGE_VERSION, tree_sitter.LANGUAGE_VERSION
        assert lo <= lang.abi_version <= hi, lid


def test_registered_metadata() -> None:
    assert SUPPORT["python"].tier is Tier.T1
    assert SUPPORT["python"].indent_default == IndentSpec("space", 4)
    assert SUPPORT["typescript"].indent_default == IndentSpec("space", 2)
    assert SUPPORT["json"].tier is Tier.T3
    assert all(spec.tier in set(Tier) for spec in SUPPORT.values())
    if "go" in SUPPORT:
        assert SUPPORT["go"].indent_default == IndentSpec("tab", 1)
    assert "**/test_*.py" in SUPPORT["python"].test_path_globs
    assert all(isinstance(spec.queries, LanguageQueries) for spec in SUPPORT.values())  # 中身は別の役が足す


def test_typescript_and_tsx_share_a_package_but_differ_in_grammar() -> None:
    ts, tsx = SUPPORT["typescript"].grammar(), SUPPORT["tsx"].grammar()
    cache = TreeCache()
    jsx = b"const a = <div>hi</div>;\n"
    assert cache.get_or_parse("w", rp("a.tsx"), "j1", "tsx", lambda: jsx).has_errors is False
    assert cache.get_or_parse("w", rp("a.ts"), "j2", "typescript", lambda: jsx).has_errors is True
    assert ts is not tsx


def test_extension_reverse_lookup() -> None:
    for lid, exts in M1_REQUIRED.items():
        for ext in exts:
            assert language_for_extension(ext) == lid, ext
    assert language_for_extension("PY") == "python"  # 先頭の "." と大文字小文字は問わない
    assert language_for_extension(".Svg") == "xml"
    assert language_for_extension(".nope") is None
    assert language_for_extension("") is None


def test_each_extension_belongs_to_one_language() -> None:
    seen: dict[str, str] = {}
    for lid, spec in SUPPORT.items():
        assert spec.extensions, lid
        for ext in spec.extensions:
            assert ext.startswith(".") and ext == ext.lower(), (lid, ext)
            assert ext not in seen, f"{ext} is claimed by {seen[ext]} and {lid}"
            seen[ext] = lid


def test_get_support() -> None:
    assert get_support("python") is SUPPORT["python"]
    assert get_support("cobol") is None
    assert isinstance(get_support("json"), LanguageSpec)
    assert isinstance(ADAPTERS, dict)


def _spec(lid: str, loader: Any) -> LanguageSpec:
    return LanguageSpec(
        language_id=lid,
        tier=Tier.T1,
        extensions=(f".{lid}",),
        grammar_loader=loader,
        indent_default=IndentSpec("space", 4),
    )


def test_build_registry_drops_languages_that_fail_to_load() -> None:
    class BadAbi:
        abi_version = tree_sitter.LANGUAGE_VERSION + 1

    def missing() -> tree_sitter.Language:
        import tree_sitter_no_such_grammar  # type: ignore[import-not-found]  # noqa: F401

        raise AssertionError("unreachable")

    def boom() -> tree_sitter.Language:
        raise RuntimeError("boom")

    good = SUPPORT["python"]
    specs = [
        _spec("good", good.grammar_loader),
        _spec("missing", missing),
        _spec("boom", boom),
        _spec("badabi", lambda: BadAbi()),  # type: ignore[arg-type,return-value]
    ]
    ok, bad = build_registry(specs)
    assert list(ok) == ["good"]
    assert set(bad) == {"missing", "boom", "badabi"}
    assert "not installed" in bad["missing"] and "tree_sitter_no_such_grammar" in bad["missing"]
    assert "RuntimeError" in bad["boom"] and "boom" in bad["boom"]
    assert "ABI" in bad["badabi"]


def test_unavailable_languages_is_a_copy() -> None:
    d = dict(unavailable_languages())
    d["x"] = "y"
    assert "x" not in unavailable_languages()


# --- キャッシュ: 解析は同じ版で 1 回 --------------------------------------------------------


def test_same_sha256_is_parsed_once() -> None:
    cache = TreeCache()
    loads = 0

    def load() -> bytes:
        nonlocal loads
        loads += 1
        return py_src(1)

    a = cache.get_or_parse("w", rp("a.py"), "sha-1", "python", load)
    b = cache.get_or_parse("w", rp("a.py"), "sha-1", "python", load)
    assert a is b
    s = cache.stats()
    assert (s.parses, s.hits, s.misses, loads) == (1, 1, 1, 1)
    assert cache.peek("w", rp("a.py"), "sha-1") is a
    assert cache.peek("w", rp("a.py"), "other") is None
    assert cache.stats().hits == 1  # peek は統計を変えない


def test_changed_sha256_is_reparsed() -> None:
    cache = TreeCache()
    a = cache.get_or_parse("w", rp("a.py"), "sha-1", "python", lambda: py_src(1))
    b = cache.get_or_parse("w", rp("a.py"), "sha-2", "python", lambda: py_src(2))
    assert a is not b
    assert cache.stats().parses == 2
    assert b.source == py_src(2) and b.sha256 == "sha-2" and b.language_id == "python"
    # 古い版もまだ残っている（LRU で落ちるまで）
    assert cache.peek("w", rp("a.py"), "sha-1") is a


def test_key_includes_workspace_and_path() -> None:
    cache = TreeCache()
    cache.get_or_parse("w1", rp("a.py"), "s", "python", lambda: py_src(1))
    cache.get_or_parse("w2", rp("a.py"), "s", "python", lambda: py_src(1))
    cache.get_or_parse("w1", rp("b.py"), "s", "python", lambda: py_src(1))
    assert cache.stats().parses == 3


def test_language_change_for_same_version_reparses() -> None:
    cache = TreeCache()
    src = b"x = 1\n"
    a = cache.get_or_parse("w", rp("a"), "s", "python", lambda: src)
    b = cache.get_or_parse("w", rp("a"), "s", "javascript", lambda: src)
    assert a.language_id == "python" and b.language_id == "javascript"
    assert cache.stats().files == 1


def test_evict_drops_every_version_of_a_path() -> None:
    cache = TreeCache()
    cache.get_or_parse("w", rp("a.py"), "s1", "python", lambda: py_src(1))
    cache.get_or_parse("w", rp("a.py"), "s2", "python", lambda: py_src(2))
    cache.get_or_parse("w", rp("b.py"), "s1", "python", lambda: py_src(3))
    cache.evict("w", rp("a.py"))
    assert cache.peek("w", rp("a.py"), "s1") is None and cache.peek("w", rp("a.py"), "s2") is None
    assert cache.peek("w", rp("b.py"), "s1") is not None
    s = cache.stats()
    assert s.files == 1 and s.bytes == estimate_tree_bytes("python", len(py_src(3)))
    cache.evict("w", rp("nothing.py"))  # 無いものを落としても何も起きない
    cache.clear("w")
    assert cache.stats().files == 0 and cache.stats().bytes == 0


# --- キャッシュ: 上限 -----------------------------------------------------------------------


def test_file_count_limit_keeps_the_most_recent() -> None:
    cache = TreeCache(max_files=3)
    for i in range(3):
        cache.get_or_parse("w", rp(f"f{i}.py"), f"s{i}", "python", lambda i=i: py_src(i))
    cache.get_or_parse("w", rp("f0.py"), "s0", "python", lambda: py_src(0))  # f0 を最近にする（ヒット）
    for i in range(3, 6):
        cache.get_or_parse("w", rp(f"f{i}.py"), f"s{i}", "python", lambda i=i: py_src(i))
        assert cache.stats().files <= 3
    s = cache.stats()
    assert s.files == 3 and s.evictions == 3
    for i in (3, 4, 5):
        assert cache.peek("w", rp(f"f{i}.py"), f"s{i}") is not None
    assert cache.peek("w", rp("f0.py"), "s0") is None  # f0 も、後から入った 3 件に押し出された


def test_byte_limit_uses_source_length_times_factor() -> None:
    src = py_src(1)
    cost = estimate_tree_bytes("python", len(src))
    assert cost == len(src) * 21
    cache = TreeCache(max_bytes=cost * 2 + 1)  # 2 件までは入り、3 件目で最古が落ちる
    for i in range(5):
        cache.get_or_parse("w", rp(f"f{i}.py"), f"s{i}", "python", lambda: src)
        s = cache.stats()
        assert s.bytes <= s.max_bytes
        assert s.files <= 2
    s = cache.stats()
    assert (s.files, s.bytes) == (2, cost * 2)
    assert cache.peek("w", rp("f4.py"), "s4") is not None and cache.peek("w", rp("f3.py"), "s3") is not None
    assert cache.peek("w", rp("f0.py"), "s0") is None


def test_entry_larger_than_the_byte_limit_is_returned_but_not_kept() -> None:
    cache = TreeCache(max_bytes=10)
    parsed = cache.get_or_parse("w", rp("a.py"), "s", "python", lambda: py_src(1))
    assert parsed.source == py_src(1) and not parsed.has_errors
    s = cache.stats()
    assert (s.files, s.bytes, s.parses) == (0, 0, 1)


def test_tree_memory_factors() -> None:
    assert estimate_tree_bytes("python", 100) == 2100
    assert estimate_tree_bytes("json", 100) == 2600
    assert estimate_tree_bytes("markdown", 100) == 2600
    assert estimate_tree_bytes("rust", 100) == 2400
    assert estimate_tree_bytes("python", 0) == 0


# --- キャッシュ: 並行 -----------------------------------------------------------------------


def test_concurrent_requests_for_the_same_key_parse_once() -> None:
    cache = TreeCache()
    n_threads = 4
    barrier = threading.Barrier(n_threads)
    loads = 0
    results: list[Any] = [None] * n_threads
    errors: list[BaseException] = []

    def load() -> bytes:
        nonlocal loads
        loads += 1
        wait_for_waiters(
            cache, n_threads - 1
        )  # 他のスレッドが待ちに入るまで解析を始めさせない（時間に頼らない）
        return py_src(7) * 200

    def worker(i: int) -> None:
        try:
            barrier.wait()
            results[i] = cache.get_or_parse("w", rp("a.py"), "same", "python", load)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert loads == 1
    assert cache.stats().parses == 1
    assert all(r is results[0] and r is not None for r in results)


def test_concurrent_requests_for_different_keys_all_parse() -> None:
    cache = TreeCache()
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(10):
                n = i * 10 + j
                parsed = cache.get_or_parse("w", rp(f"f{n}.py"), f"s{n}", "python", lambda n=n: py_src(n))
                assert parsed.source == py_src(n) and not parsed.has_errors
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert cache.stats().parses == 40 and cache.stats().files == 40


def test_failure_of_the_loader_reaches_waiters_and_does_not_poison_the_key() -> None:
    cache = TreeCache()
    barrier = threading.Barrier(2)
    seen: list[BaseException] = []

    def bad_load() -> bytes:
        wait_for_waiters(cache, 1)
        raise OSError("disk gone")

    def worker() -> None:
        barrier.wait()
        try:
            cache.get_or_parse("w", rp("a.py"), "k", "python", bad_load)
        except OSError as e:
            seen.append(e)

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(seen) == 2  # 待っていた側にも同じ失敗が届く（遅れて来て再実行した側も同じ失敗）
    assert cache.stats().files == 0
    ok = cache.get_or_parse("w", rp("a.py"), "k", "python", lambda: py_src(1))  # 次の要求は改めて読める
    assert not ok.has_errors


async def test_aget_or_parse_runs_in_a_worker_thread() -> None:
    cache = TreeCache()
    main = threading.get_ident()
    seen: list[int] = []

    def load() -> bytes:
        seen.append(threading.get_ident())
        return py_src(1)

    a = await cache.aget_or_parse("w", rp("a.py"), "s", "python", load)
    b = await cache.aget_or_parse("w", rp("a.py"), "s", "python", load)
    assert a is b and len(seen) == 1 and seen[0] != main
    assert cache.stats().parses == 1


# --- ParsedFile -----------------------------------------------------------------------------


def test_valid_source_has_no_errors() -> None:
    cache = TreeCache()
    p = cache.get_or_parse("w", rp("a.py"), "s", "python", lambda: py_src(1))
    assert p.has_errors is False and p.error_ranges == ()
    assert p.tree.root_node.type == "module"
    assert p.line_index.line_count == 3 and p.source == py_src(1)
    assert p.tree.root_node.children[0].text == b"def f1(x):\n    return x + 1"


def test_error_ranges_from_python_syntax_errors() -> None:
    src = b"x = = 1\ndef f(:\n"
    p = TreeCache().get_or_parse("w", rp("a.py"), "s", "python", lambda: src)
    assert p.has_errors
    assert p.error_ranges == (
        Range(Position(0, 4), Position(0, 5)),  # ERROR: 2 つ目の "="
        Range(Position(1, 6), Position(1, 6)),  # MISSING ")" （幅 0）
    )


def test_error_ranges_use_codepoint_columns() -> None:
    # "😀"（UTF-8 で 4 バイト）と "あ"（3 バイト）の後ろ。Tree-sitter の列はバイトなので 19、コードポイントでは 14
    src = "s = '😀あ'; x = = 1\n".encode()
    p = TreeCache().get_or_parse("w", rp("a.py"), "s", "python", lambda: src)
    assert p.tree.root_node.has_error
    (r,) = p.error_ranges
    assert r == Range(Position(0, 14), Position(0, 15))


def test_error_ranges_with_lone_cr_follow_the_line_index_not_tree_sitter_rows() -> None:
    # 単独の "\r" は Tree-sitter では改行にならないが、Lucianke の行（LSP と同じ）では改行
    src = b"a = 1\rb = = 2\n"
    p = TreeCache().get_or_parse("w", rp("a.py"), "s", "python", lambda: src)
    assert p.error_ranges == (
        Range(Position(0, 4), Position(0, 5)),
        Range(Position(1, 4), Position(1, 5)),
    )
    assert p.tree.root_node.start_point.row == 0


def test_missing_node_in_json_has_zero_width() -> None:
    src = b'{"a": 1,\r\n"b": }\r\n'
    p = TreeCache().get_or_parse("w", rp("a.json"), "s", "json", lambda: src)
    assert p.has_errors
    (r,) = p.error_ranges
    assert r.start == r.end == Position(1, 4)


def test_crlf_and_bom_fixture_parses_cleanly() -> None:
    data = (FIXTURES / "poly" / "src" / "shop" / "models" / "crlf_bom.py").read_bytes()
    assert data.startswith(BOM) and b"\r\n" in data
    p = TreeCache().get_or_parse("w", rp("src/shop/models/crlf_bom.py"), "s", "python", lambda: data)
    assert p.has_errors is False and p.error_ranges == ()
    assert p.source == data  # 渡されたバイト列そのまま（BOM つき）
    func = next(c for c in p.tree.root_node.children if c.type == "function_definition")
    pos = p.line_index.offset_to_position(func.start_byte).value
    assert pos == Position(3, 0)  # BOM は列に数えない。CRLF は 1 つの区切り
    assert func.start_point.row == 3  # Tree-sitter の行とも一致する（CRLF のみのファイル）


def test_bom_error_columns_do_not_count_the_bom() -> None:
    src = BOM + b"x = = 1\r\ndef f(:\r\n"
    p = TreeCache().get_or_parse("w", rp("a.py"), "s", "python", lambda: src)
    assert p.error_ranges == (
        Range(Position(0, 4), Position(0, 5)),
        Range(Position(1, 6), Position(1, 6)),
    )


def test_empty_file() -> None:
    p = TreeCache().get_or_parse("w", rp("empty.py"), "s", "python", lambda: b"")
    assert p.has_errors is False and p.source == b"" and p.line_index.line_count == 1


def test_error_ranges_are_capped() -> None:
    src = b"x = = 1\n" * 50
    p = TreeCache().get_or_parse("w", rp("a.py"), "s", "python", lambda: src)
    assert p.has_errors and 1 <= len(p.error_ranges) <= parsers.ERROR_RANGES_MAX


# --- 大きすぎるファイル・未登録の言語 -------------------------------------------------------------


def test_file_larger_than_max_file_bytes_is_unsupported_and_not_parsed() -> None:
    cache = TreeCache()
    with pytest.raises(LuciankeError) as ei:
        cache.get_or_parse("w", rp("big.py"), "s", "python", lambda: b"\n" * (MAX_FILE_BYTES + 1))
    assert ei.value.code is ErrorCode.E_UNSUPPORTED
    assert ei.value.details["reason"] == "file_too_large"
    assert ei.value.details["limit"] == MAX_FILE_BYTES
    s = cache.stats()
    assert (s.parses, s.files) == (0, 0)


def test_max_file_bytes_boundary() -> None:
    cache = TreeCache(max_file_bytes=len(py_src(1)))
    assert cache.get_or_parse("w", rp("a.py"), "s", "python", lambda: py_src(1)) is not None
    with pytest.raises(LuciankeError) as ei:
        cache.get_or_parse("w", rp("b.py"), "s", "python", lambda: py_src(1) + b"\n")
    assert ei.value.details["reason"] == "file_too_large"


def test_unregistered_language_is_unsupported_before_loading() -> None:
    cache = TreeCache()

    def load() -> bytes:
        raise AssertionError("must not be called")

    with pytest.raises(LuciankeError) as ei:
        cache.get_or_parse("w", rp("a.cob"), "s", "cobol", load)
    assert ei.value.code is ErrorCode.E_UNSUPPORTED
    assert ei.value.details["reason"] == "no_grammar" and ei.value.details["language_id"] == "cobol"


# --- 期限つき解析 ---------------------------------------------------------------------------------


def test_deadline_generous_matches_a_plain_parse() -> None:
    src = (py_src(1) + b"class A:\n    def m(self): pass\n") * 2000  # 16 KiB のチャンクを数回またぐ大きさ
    assert len(src) > parsers.PARSE_CHUNK_BYTES * 2
    p, timed_out = parse_source(src, "python", "s", deadline_ms=60_000)
    assert timed_out is False and not p.has_errors
    plain = tree_sitter.Parser(SUPPORT["python"].grammar()).parse(src)
    assert str(p.tree.root_node) == str(plain.root_node)
    assert p.tree.root_node.end_byte == len(src)


def test_deadline_exceeded_truncates_flags_errors_and_is_not_cached() -> None:
    src = py_src(1) * 5000
    cache = TreeCache(parse_deadline_ms=0)  # 最初の read で期限切れ
    p = cache.get_or_parse("w", rp("big.py"), "s", "python", lambda: src)
    assert p.has_errors is True
    assert p.source == src  # ソースは全体（木だけが途中まで）
    tail = p.error_ranges[-1]
    assert tail.start == Position(0, 0) and tail.end == p.line_index.offset_to_position(len(src)).value
    s = cache.stats()
    assert (s.parses, s.timeouts, s.files) == (1, 1, 0)
    assert cache.peek("w", rp("big.py"), "s") is None
    # 期限を延ばした次の要求は、改めて完全に解析してキャッシュする
    q = cache.get_or_parse("w", rp("big.py"), "s", "python", lambda: src, parse_deadline_ms=60_000)
    assert q.has_errors is False and q.tree.root_node.end_byte == len(src)
    assert cache.stats().files == 1


def test_deadline_stops_in_the_middle_with_a_partial_tail() -> None:
    src = b"x = 1\n" * 200_000  # 約 1.2 MB。期限 1 ms では末尾まで届かない
    p, timed_out = parse_source(src, "python", "s", deadline_ms=1)
    assert timed_out is True and p.has_errors
    tail = p.error_ranges[-1]
    assert tail.end == p.line_index.offset_to_position(len(src)).value
    assert Position(0, 0) <= tail.start <= tail.end
    assert p.tree.root_node.end_byte < len(src)


def test_deadline_none_means_unbounded() -> None:
    cache = TreeCache(parse_deadline_ms=None)
    p = cache.get_or_parse("w", rp("a.py"), "s", "python", lambda: py_src(1) * 3000)
    assert not p.has_errors and cache.stats().timeouts == 0


def test_parser_is_per_thread_and_language() -> None:
    lang = SUPPORT["python"].grammar()
    a = parsers._parser_for("python", lang)
    assert parsers._parser_for("python", lang) is a
    other: list[Any] = []
    t = threading.Thread(target=lambda: other.append(parsers._parser_for("python", lang)))
    t.start()
    t.join()
    assert other[0] is not a
    assert parsers._parser_for("javascript", SUPPORT["javascript"].grammar()) is not a


# --- BUG-0020: 解析後の Node.text（木が read_callback を保持している） --------------------------------

_TEXT_AFTER_DEADLINE_SCRIPT = r"""
import sys, time
from lucianke.syntax.parsers import parse_source

def comments(root):
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "comment":
            yield n
        stack.extend(n.children)

# (a) 期限内に解析を終えた木でも、期限を過ぎたあとの Node.text が戻る（以前は空回りして戻らなかった）
src = b"# lead\ndef f():\n    return 1\n"
p, timed_out = parse_source(src, "python", "s", deadline_ms=200)
assert not timed_out
time.sleep(0.4)
assert [c.text for c in comments(p.tree.root_node)] == [b"# lead"]
assert p.tree.root_node.children[1].text == b"def f():\n    return 1"

# (b) 期限で打ち切られた木でも、解析できた部分の Node.text は戻る
big = b"# head\n" + b"x = 1\n" * 200_000
q, timed_out = parse_source(big, "python", "s", deadline_ms=1)
assert timed_out
time.sleep(0.05)
assert next(iter(comments(q.tree.root_node))).text == b"# head"
print("OK")
"""


def test_node_text_after_the_parse_deadline_does_not_spin() -> None:
    """木は ``read_callback`` を保持し、``Node.text`` のたびに呼び直す。期限切れの後に ``b""`` を返すと ``Node.text`` が戻らず、
    イベントループが空回りしてサーバーが無応答になった（BUG-0020 / 0024 / 0026）。空回りは別プロセスで見張る（回帰時にテスト実行を巻き込まない）。"""
    import subprocess
    import sys

    cp = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _TEXT_AFTER_DEADLINE_SCRIPT], capture_output=True, text=True, timeout=60, check=False
    )  # fmt: skip
    assert cp.returncode == 0 and cp.stdout.strip() == "OK", cp.stderr
