"""MCP ``initialize`` の ``instructions``（BUG-0137）。

ホストはこの文字列を毎セッションのシステムプロンプトに入れる。Claude Code では Lucianke のツールは遅延読み込みで
名前しか見えないため、「いつ Read・Grep の代わりに使うか」と、ツールをまたぐ流れをここで 1 か所に書く。
英語（ツールの説明文と同じ）。短く保つ（約 250 トークン。全利用者の全セッションに入る固定費）。
載せるのは既定の ``tools/list`` にあるツールだけ（``tests/e2e/test_initialize_instructions_bug0137.py`` が突き合わせる）。
"""

from __future__ import annotations

INSTRUCTIONS = """\
Lucianke: symbol-level code navigation and editing (Python, TypeScript, JavaScript; outline-level for other files). \
Prefer it over Read/Grep/Glob when you need to understand or change code; it returns signatures, symbol bodies and \
symbol-resolved references instead of whole files or text matches. Keep Read/Grep for non-code text, or when a result says \
degraded/unsupported.

Typical flow:
1. Unfamiliar repo: get_repo_map. Then get_file_skeleton for the files that matter (signatures only, each file header \
shows sha=).
2. Read only what you need: get_symbol_bodies. Find symbols by name with search_workspace_symbols; disambiguate with \
resolve_symbols.
3. Impact: find_references_and_implementations (who uses it), query_call_hierarchy (callers/callees), then \
get_diagnostics (errors after a change).
4. Change: edit_symbol_body, rename_symbol (cross-file, safe), apply_code_action. Pass expected_file_sha256 (the sha= \
from skeleton/bodies) and try dry_run first.

Conventions: urn: values in one tool's output are valid targets for the next tool. Output is compact text by default \
(format=json for a stable shape); if truncated, raise max_tokens or pass the returned cursor. If results look wrong or \
the wrong project is targeted, call configure_workspace (status). For questions the other tools do not answer, run \
read-only SQL with query_project_db over Lucianke's own index of the project (symbols, relations, diagnostics, git \
history); read get_project_db_schema first. It does not open the project's database files.
"""
