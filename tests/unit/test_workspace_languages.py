"""workspace/languages.py: 言語判定・ティア・秘密ファイル・バイナリ・is_test（設計書 §7.3.3、仕様書 §8.1、§13.5）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lucianke.core.model import RelPath, Tier
from lucianke.workspace.languages import (
    HEAD_BYTES,
    LANGUAGE_TIERS,
    Detected,
    detect,
    dir_names_of_tests,
    fixture_roots,
    is_secret_name,
    is_test,
    tier_of,
)

POLY = Path(__file__).resolve().parents[1] / "fixtures" / "poly"

# F-POLY の全ファイル → (language_id, tier)
POLY_EXPECTED: dict[str, tuple[str, int]] = {
    "README.md": ("markdown", 3),
    "conf/.env": ("secret", 4),
    "conf/app.yaml": ("yaml", 3),
    "conf/rules.custom": ("unknown", 4),
    "conf/settings.json": ("json", 3),
    "db/schema.sql": ("sql", 2),
    "docs/api.rst": ("rst", 3),
    "docs/flow.dot": ("dot", 2),
    "docs/flow.mmd": ("mermaid", 2),
    "legacy/calc.cob": ("cobol", 4),
    "notebooks/eda.ipynb": ("ipynb", 2),
    "proto/order.proto": ("proto", 2),
    "pyproject.toml": ("toml", 3),
    "pyrightconfig.json": ("json", 3),  # basedpyright の設定の根をフィクスチャに止める（BUG-0129）
    "reports/junit.xml": ("xml", 3),
    "src/shop/__init__.py": ("python", 1),
    "src/shop/api/__init__.py": ("python", 1),
    "src/shop/api/routes.py": ("python", 1),
    "src/shop/models/__init__.py": ("python", 1),
    "src/shop/models/crlf_bom.py": ("python", 1),
    "src/shop/models/no_final_newline.py": ("python", 1),
    "src/shop/models/receipt.py": ("python", 1),
    "src/shop/services/__init__.py": ("python", 1),
    "src/shop/services/limits.py": ("python", 1),
    "src/shop/services/payment.py": ("python", 1),
    "templates/receipt.j2": ("jinja2", 3),
    "tests/test_payment.py": ("python", 1),
    "web/src/app.ts": ("typescript", 1),
    "web/src/payment.ts": ("typescript", 1),
    "web/tsconfig.json": ("json", 3),
}


def rel_of(path: str) -> RelPath:
    return RelPath(path)


def head_of(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read(HEAD_BYTES)


# --- フィクスチャ F-POLY ------------------------------------------------------------------


def test_poly_expectation_table_covers_every_fixture_file():
    on_disk = {p.relative_to(POLY).as_posix() for p in POLY.rglob("*") if p.is_file()}
    assert on_disk == set(POLY_EXPECTED)


@pytest.mark.parametrize(("rel", "expected"), sorted(POLY_EXPECTED.items()))
def test_poly_files(rel: str, expected: tuple[str, int]):
    result = detect(rel_of(rel), head_of(POLY / rel))
    assert (result.language_id, int(result.tier)) == expected
    assert result.is_secret is (rel == "conf/.env")
    assert result.is_binary is False


def test_poly_covers_all_four_tiers():
    tiers = {t for _, t in POLY_EXPECTED.values()}
    assert tiers == {1, 2, 3, 4}


def test_poly_bom_and_crlf_python_is_not_binary():
    head = head_of(POLY / "src/shop/models/crlf_bom.py")
    assert head.startswith(b"\xef\xbb\xbf")
    assert detect(rel_of("src/shop/models/crlf_bom.py"), head).language_id == "python"


# --- ティア表 ----------------------------------------------------------------------------

TIER1 = ["python", "typescript", "tsx", "javascript", "rust", "go", "c", "cpp", "c_sharp", "kotlin", "swift"]
TIER2 = ["cython", "ipynb", "sql", "dot", "mermaid", "openapi", "proto", "graphql"]
TIER3 = ["xml", "json", "jsonl", "yaml", "toml", "jinja2", "markdown", "rst"]


def test_language_tiers_follow_spec_section_8_1():
    assert {lang for lang, t in LANGUAGE_TIERS.items() if t is Tier.T1} == set(TIER1)
    assert {lang for lang, t in LANGUAGE_TIERS.items() if t is Tier.T2} == set(TIER2)
    assert set(TIER3) <= {lang for lang, t in LANGUAGE_TIERS.items() if t is Tier.T3}
    assert {lang for lang, t in LANGUAGE_TIERS.items() if t is Tier.T3} == set(TIER3)
    for special in ("binary", "secret", "unknown", "cobol", "fortran"):
        assert LANGUAGE_TIERS[special] is Tier.T4
    assert tier_of("python") is Tier.T1
    assert tier_of("no-such-language") is Tier.T4


@pytest.mark.parametrize(
    ("name", "language"),
    [
        ("a.py", "python"),
        ("a.pyi", "python"),
        ("a.PY", "python"),
        ("a.ts", "typescript"),
        ("a.d.ts", "typescript"),
        ("a.mts", "typescript"),
        ("a.tsx", "tsx"),
        ("a.js", "javascript"),
        ("a.mjs", "javascript"),
        ("a.jsx", "javascript"),
        ("a.rs", "rust"),
        ("a.go", "go"),
        ("a.c", "c"),
        ("a.h", "c"),
        ("a.cpp", "cpp"),
        ("a.hpp", "cpp"),
        ("a.cc", "cpp"),
        ("a.cs", "c_sharp"),
        ("a.kt", "kotlin"),
        ("a.kts", "kotlin"),
        ("a.swift", "swift"),
        ("a.pyx", "cython"),
        ("a.pxd", "cython"),
        ("a.ipynb", "ipynb"),
        ("a.sql", "sql"),
        ("a.dot", "dot"),
        ("a.gv", "dot"),
        ("a.mmd", "mermaid"),
        ("a.proto", "proto"),
        ("a.graphql", "graphql"),
        ("a.gql", "graphql"),
        ("a.xml", "xml"),
        ("a.svg", "xml"),
        ("a.json", "json"),
        ("a.jsonl", "jsonl"),
        ("a.ndjson", "jsonl"),
        ("a.yaml", "yaml"),
        ("a.yml", "yaml"),
        ("a.toml", "toml"),
        ("a.j2", "jinja2"),
        ("a.md", "markdown"),
        ("a.rst", "rst"),
        ("a.cob", "cobol"),
        ("a.cbl", "cobol"),
        ("a.f90", "fortran"),
        ("a.for", "fortran"),
        ("a.custom", "unknown"),
        ("a.txt", "unknown"),
        ("noext", "unknown"),
        (".gitignore", "unknown"),
        ("Dockerfile", "dockerfile"),
        ("Makefile", "makefile"),
        ("Cargo.lock", "toml"),
        ("dir.py/noext", "unknown"),
    ],
)
def test_extension_table(name: str, language: str):
    result = detect(rel_of(f"x/{name}"), b"plain text\n")
    assert result.language_id == language
    assert result.tier is tier_of(language)


def test_extension_decides_before_shebang_and_modeline():
    head = b"#!/usr/bin/env python3\n# -*- mode: ruby -*-\n"
    assert detect(rel_of("tool.sh"), head).language_id == "shell"
    assert detect(rel_of("tool.rs"), head).language_id == "rust"


# --- OpenAPI -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "head", "expected"),
    [
        ("openapi.yaml", b"anything\n", "openapi"),
        ("api/openapi.yml", b"", "openapi"),
        ("swagger.json", b"{}", "openapi"),
        ("petstore.openapi.yaml", b"", "openapi"),
        ("spec.yaml", b"openapi: 3.0.3\ninfo:\n  title: x\n", "openapi"),
        ("spec.yaml", b"# comment\nopenapi: '3.1.0'\n", "openapi"),
        ("spec.yaml", b'swagger: "2.0"\n', "openapi"),
        ("spec.json", b'{\n  "openapi": "3.0.0",\n  "info": {}\n}', "openapi"),
        ("spec.json", b'{"swagger": "2.0"}', "openapi"),
        # 迷ったら yaml / json
        ("openapi-notes.yaml", b"title: notes\n", "yaml"),
        ("app.yaml", b"name: openapi\nversion: 3\n", "yaml"),
        ("app.yaml", b"nested:\n  openapi: 3.0.0\n", "yaml"),
        ("settings.json", b'{"name": "x"}', "json"),
        ("openapi.toml", b"", "toml"),
    ],
)
def test_openapi(name: str, head: bytes, expected: str):
    result = detect(rel_of(name), head)
    assert result.language_id == expected
    assert result.tier is tier_of(expected)


def test_openapi_is_tier_2_and_yaml_is_tier_3():
    assert detect(rel_of("openapi.yaml"), b"").tier is Tier.T2
    assert detect(rel_of("other.yaml"), b"").tier is Tier.T3


# --- シバン・モードライン -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"#!/usr/bin/env python3\nprint(1)\n", "python"),
        (b"#!/usr/bin/python\n", "python"),
        (b"#!/usr/bin/python3.12 -u\n", "python"),
        (b"#!/usr/bin/env python\n", "python"),
        (b"#!/usr/bin/env -S python3 -u\n", "python"),
        (b"#!/usr/bin/env PYTHONPATH=x python3\n", "python"),
        (b"#!/opt/pypy3/bin/pypy3\n", "python"),
        (b"#!/usr/bin/env node\n", "javascript"),
        (b"#!/usr/bin/node --harmony\n", "javascript"),
        (b"#!/usr/bin/env ts-node\n", "typescript"),
        (b"#!/bin/sh\n", "shell"),
        (b"#!/bin/bash -e\n", "shell"),
        (b"#!/usr/bin/env bash\n", "shell"),
        (b"#!/usr/bin/env perl\n", "perl"),
        (b"\xef\xbb\xbf#!/usr/bin/env python3\n", "python"),
        (b"#!/usr/bin/env python3\r\nprint(1)\r\n", "python"),
        (b"#!\n", "unknown"),
        (b"#!/usr/bin/env\n", "unknown"),
        (b"#!/usr/bin/unknown-thing\n", "unknown"),
        (b"# not a shebang #!/usr/bin/python\n", "unknown"),
        (b"\n#!/usr/bin/python\n", "unknown"),
    ],
)
def test_shebang(head: bytes, expected: str):
    result = detect(rel_of("scripts/run"), head)
    assert result.language_id == expected
    assert result.tier is tier_of(expected)


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"# -*- mode: python -*-\n", "python"),
        (b"# -*- coding: utf-8; mode: python; -*-\n", "python"),
        (b"# -*- python -*-\n", "python"),
        (b"/* -*- mode: c++ -*- */\n", "cpp"),
        (b";; -*- mode: Python-mode -*-\n", "python"),
        (b"// -*- mode: rust -*-\n", "rust"),
        (b"# vim: ft=python\n", "python"),
        (b"# vim: set ft=python:\n", "python"),
        (b"# vim: set sw=4 ts=4 filetype=yaml :\n", "yaml"),
        (b"/* vi: ft=c */\n", "c"),
        (b"-- ex: ft=sql\n", "sql"),
        (b"# vim: set ft=typescriptreact:\n", "tsx"),
        (b"first\nsecond\nthird\nfourth\n# vim: ft=go\n", "go"),
        (b"# -*- mode: no-such-mode -*-\n", "unknown"),
        (b"# vim: ft=no-such\n", "unknown"),
        (b"just text mentioning mode: python\n", "unknown"),
    ],
)
def test_modeline(head: bytes, expected: str):
    result = detect(rel_of("notes/NOEXT"), head)
    assert result.language_id == expected
    assert result.tier is tier_of(expected)


def test_modeline_in_last_lines_only_counts_for_short_files():
    tail_line = b"# vim: ft=python\n"
    short = b"line\n" * 20 + tail_line
    assert detect(rel_of("x/NOEXT"), short).language_id == "python"
    # 先頭 8 KB で切れている（= 末尾が見えていない）ときは、末尾の行を末尾とは見なさない
    truncated = (b"x" * 99 + b"\n") * 82 + tail_line
    assert len(truncated) >= HEAD_BYTES
    assert detect(rel_of("x/NOEXT"), truncated).language_id == "unknown"


def test_shebang_wins_over_modeline():
    assert detect(rel_of("x/run"), b"#!/usr/bin/env python3\n# vim: ft=ruby\n").language_id == "python"


# --- バイナリ ----------------------------------------------------------------------------


def test_nul_byte_is_binary():
    result = detect(rel_of("data/blob.bin"), b"ab\x00cd")
    assert result == Detected("binary", Tier.T4, is_secret=False, is_binary=True)
    assert result.index_content is False


def test_binary_beats_extension():
    assert detect(rel_of("src/a.py"), b"print(1)\x00\x00").language_id == "binary"
    assert detect(rel_of("logo.png"), b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR").language_id == "binary"


def test_utf16_with_bom_is_binary():
    head = "print(1)\n".encode("utf-16")
    assert detect(rel_of("src/a.py"), head).is_binary is True


def test_invalid_utf8_is_binary():
    assert detect(rel_of("legacy/sjis.txt"), "日本語".encode("cp932")).language_id == "binary"
    assert detect(rel_of("a.py"), b"x = '\xff\xfe'\n").language_id == "binary"
    assert (
        detect(rel_of("a.py"), b"x = '\xe3\x81'\n").language_id == "binary"
    )  # 切れたマルチバイト（短い入力は全体）


def test_valid_utf8_text_is_not_binary():
    result = detect(rel_of("src/a.py"), "名前 = '日本語🙂'\n".encode())
    assert result.is_binary is False
    assert result.language_id == "python"
    assert result.index_content is True


def test_multibyte_char_cut_at_the_head_limit_is_not_binary():
    body = ("あ" * 5000).encode()
    assert len(body) > HEAD_BYTES
    assert HEAD_BYTES % 3 != 0  # 8192 は 3 バイト文字の途中で切れる
    result = detect(rel_of("docs/long.md"), body[:HEAD_BYTES])
    assert result.is_binary is False
    assert result.language_id == "markdown"


def test_longer_head_than_limit_is_truncated_before_checking():
    head = b"x = 1\n" * 2000 + b"\x00"  # NUL は 8 KB より後ろ
    assert len(head) > HEAD_BYTES
    assert detect(rel_of("a.py"), head).is_binary is False


def test_empty_file_uses_the_extension():
    assert detect(rel_of("src/__init__.py"), b"").language_id == "python"
    assert detect(rel_of("empty"), b"").language_id == "unknown"


# --- 秘密ファイル ------------------------------------------------------------------------

SECRET_NAMES = [
    ".env",
    ".env.local",
    ".env.production",
    "server.pem",
    "tls.key",
    "id_rsa",
    "id_rsa.pub",
    "keystore.p12",
    "gcp-credentials.json",
    "credentials.json",
    "my_credentials_prod.json",
    "CERT.PEM",
    ".ENV",
]
NOT_SECRET_NAMES = [
    "environment.py",
    "env.py",
    "dotenv.py",
    "keyboard.py",
    "key.py",
    "monkey.txt",
    "pem.txt",
    "credentials.py",
    "credentials.json.md",
    "id_dsa_notes.txt",
    "a.env.py",
    "p12.txt",
]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_secret_names(name: str):
    for rel in (name, f"conf/{name}", f"a/b/c/{name}"):
        assert is_secret_name(rel) is True
        result = detect(rel_of(rel), b"TOKEN=abc\n")
        assert result.is_secret is True
        assert result.language_id == "secret"
        assert result.tier is Tier.T4
        assert result.is_binary is False
        assert result.index_content is False


@pytest.mark.parametrize("name", NOT_SECRET_NAMES)
def test_not_secret_names(name: str):
    assert is_secret_name(f"src/{name}") is False
    assert detect(rel_of(f"src/{name}"), b"x\n").is_secret is False


def test_secret_directory_name_does_not_make_children_secret():
    assert is_secret_name("keys.pem/readme.txt") is False
    assert is_secret_name(".env/readme.txt") is False


def test_binary_secret_is_secret_and_binary():
    result = detect(rel_of("certs/keystore.p12"), b"\x30\x82\x00\x01\xff")
    assert result.language_id == "secret"
    assert result.is_secret is True
    assert result.is_binary is True
    assert result.index_content is False


def test_secret_beats_known_extension():
    assert detect(rel_of("conf/prod-credentials.json"), b"{}").language_id == "secret"
    assert detect(rel_of("conf/.env"), b"").language_id == "secret"


# --- is_test -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "test/a.py",
        "tests/a.py",
        "tests/unit/deep/a.txt",
        "src/pkg/tests/helpers.py",
        "pkg/test/data/x.json",
        "Tests/A.py",
        "src/calc_test.go",
        "src/calc_test.rs",
        "web/app.spec.ts",
        "web/app.test.ts",
        "web/components/Button.spec.tsx",
        "web/app.test.js",
        "src/test_payment.py",
        "test_top.py",
        "legacy/calc_test.cob",
    ],
)
def test_is_test_true(rel: str):
    assert is_test(rel_of(rel)) is True


@pytest.mark.parametrize(
    "rel",
    [
        "src/shop/services/payment.py",
        "src/contest/a.py",
        "src/latest/a.py",
        "src/testing/a.py",
        "src/protest.py",
        "src/attest_utils.py",
        "src/test.py",
        "src/test_helpers.ts",  # test_*.py は .py だけ
        "src/conftest.py",
        "web/specs/a.ts",
        "web/app.spec",
        "src/mytest",
        "src/calc_tests.go",
        "README.md",
    ],
)
def test_is_test_false(rel: str):
    assert is_test(rel_of(rel)) is False


def test_is_test_on_the_poly_fixture():
    flagged = {p for p in POLY_EXPECTED if is_test(rel_of(p))}
    assert flagged == {"tests/test_payment.py"}


def test_is_test_extra_globs_from_language_support():
    js = rel_of("src/a/__tests__/x.js")
    assert is_test(js) is False
    assert is_test(js, ["__tests__"]) is False  # "/" を含まないパターンは名前だけに当てる
    assert is_test(js, ["**/__tests__/**"]) is True  # fnmatch なので * は / を越える
    assert is_test(js, ["src/*/__tests__/*"]) is True
    assert is_test(js, ["lib/*/__tests__/*"]) is False
    assert is_test(rel_of("src/a/x_spec.rb"), ["*_spec.rb"]) is True
    assert is_test(rel_of("src/a/x.rb"), ["*_spec.rb"]) is False


@pytest.mark.parametrize(
    ("rel", "roots"),
    [
        ("tests/fixtures/untyped_calls/app/engine.py", ("tests/fixtures/untyped_calls",)),
        ("tests/fixtures/untyped_calls/tests/test_pack.py", ("tests/fixtures/untyped_calls",)),
        # 大文字小文字は区別せず、綴りはそのまま返す
        ("Tests/Fixtures/Poly/src/a.py", ("Tests/Fixtures/Poly",)),
        ("src/pkg/test/testdata/case1/x.py", ("src/pkg/test/testdata/case1",)),
        # __tests__ は JS のグロブから
        ("web/__tests__/__fixtures__/p/a.ts", ("web/__tests__/__fixtures__/p",)),
        ("tests/fixtures/a/tests/fixtures/b/x.py", ("tests/fixtures/a", "tests/fixtures/a/tests/fixtures/b")),
        # ルートの名前は数え直しに使わない
        ("tests/fixtures/tests/fixtures/q/z.py", ("tests/fixtures/tests",)),
        ("tests/fixtures/db.py", ()),  # fixtures の直下のファイルはルートを作らない
        ("src/fixtures/x/a.py", ()),  # テストディレクトリの外の fixtures は見本ではない
        ("tests/unit/test_a.py", ()),
        ("tests/contract_fixtures/_rig.py", ()),
        ("fixtures/x/a.py", ()),
    ],
)
def test_fixture_roots(rel: str, roots: tuple[str, ...]):
    """BUG-0077: 名前だけの一致の段（呼び出し箇所を含むテストフィクスチャ）。"""
    assert fixture_roots(rel, ["**/__tests__/**"]) == roots


def test_dir_names_of_tests_from_language_globs():
    assert dir_names_of_tests() == {"test", "tests"}
    assert dir_names_of_tests(["**/__tests__/**", "**/*.spec.*", "**/conftest.py", "src/*/__tests__/*"]) == {
        "test",
        "tests",
        "__tests__",
    }
    # __tests__ を知らなければルートにならない
    assert fixture_roots("web/__tests__/__fixtures__/p/a.ts") == ()


def test_detected_is_hashable_and_frozen():
    d = detect(rel_of("a.py"), b"")
    assert {d: 1}[Detected("python", Tier.T1)] == 1
    with pytest.raises(AttributeError):
        d.language_id = "x"  # type: ignore[misc]
