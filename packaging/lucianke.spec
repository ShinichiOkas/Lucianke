# PyInstaller の spec（exe 配布。師匠の判断 2026-10-31: 配布は exe 形式）。build.ps1 から呼ぶ。
#
# onedir にする: 起動は約 0.7 秒。onefile は起動のたびに一時フォルダへ展開するので 3〜4 秒かかり、MCP のホストはセッションごとに
# 起動し直すため毎回待たされる（2026-10-31 実測）。実行時の中身は exe の隣の ``lucianke_internal/`` に置く（PATH 上の bin に
# 直接置いても、ほかの PyInstaller 製 exe の ``_internal/`` とぶつからない）。
#
# 同梱しないもの: LSP サーバー（basedpyright・ruff・typescript-language-server など）は外部のまま。ワークスペースの venv・
# node_modules・PATH・``LUCIANKE_LSP_*_CMD`` から探す（``lsp/servers.find_command``）。無ければ Tree-sitter（structural）に落ちる。
# -*- mode: python -*-

import importlib.util
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent  # noqa: F821  SPECPATH は PyInstaller が与える
SRC = ROOT / "src"

# 文法の import 名は ``syntax/support.py`` の表が唯一の定義。そこから拾う（言語を足しても同梱が漏れない）。
# 動的 import（``importlib.import_module``）なので PyInstaller は自分では見つけられない。配布物は全言語そろえる（langs extra 込み）。
GRAMMARS = sorted(set(re.findall(r'"(tree_sitter_\w+)"', (SRC / "lucianke" / "syntax" / "support.py").read_text(encoding="utf-8"))))
_missing = [m for m in GRAMMARS if importlib.util.find_spec(m) is None]
if _missing:
    raise SystemExit(f"grammar modules not installed: {_missing} (build with: uv run --extra langs ...)")
# 文法の配布の metadata（langs extra の分は lucianke の必須依存をたどっても入らない）。doctor が同梱済みと数えられるように
_DISTS = packages_distributions()
GRAMMAR_DISTS = sorted({d for g in GRAMMARS for d in _DISTS.get(g, [])})

hiddenimports = [
    *collect_submodules("lucianke"),  # tools/ は pkgutil で列挙して import する（tools/__init__.py）
    *[name for g in GRAMMARS for name in collect_submodules(g)],
    "re._parser",  # index/public.py が importlib.import_module で読む
    "re._constants",
]

datas = [
    *collect_data_files("lucianke"),  # index/schema.sql・syntax/queries/**/*.scm（importlib.resources で読む）
    *copy_metadata("lucianke", recursive=True),  # doctor が importlib.metadata で依存の版を読む
    *[entry for d in GRAMMAR_DISTS for entry in copy_metadata(d)],
    (str(ROOT / "LICENSE"), "licenses"),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "licenses"),  # 同梱した依存の再配布の告知
]
# exe は Python の実行系そのものを同梱する（再配布）。PSF のライセンスは同梱物に残す約束（PSF License 第 2 項）。
# python.org の Windows 版の LICENSE.txt は、同梱の OpenSSL・libffi・zlib・SQLite などの告知も含む
_PYTHON_LICENSE = Path(sys.base_prefix) / "LICENSE.txt"
if not _PYTHON_LICENSE.is_file():
    raise SystemExit(f"Python runtime license not found: {_PYTHON_LICENSE}")
datas.append((str(_PYTHON_LICENSE), "licenses/python"))

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "_pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lucianke",
    console=True,  # stdio で MCP を話す
    upx=False,
    contents_directory="lucianke_internal",
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="lucianke",
    upx=False,
)
