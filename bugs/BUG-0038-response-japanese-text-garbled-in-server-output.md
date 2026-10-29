# BUG-0038 サーバーの応答に含まれる日本語と「→」が文字化けしている（UTF-8 のバイト列を cp932 として読んだ形）

- 報告: 2026-10-29 15:15 / 見た人: トークン削減ベンチ 18c（bench/run_tokens.py）
- 見たこと（実測）:
  - サーバーを `python -P -m lucianke`（環境変数 `PYTHONUTF8=1`、`LUCIANKE_CACHE_DIR` 指定、cwd = F-PY の httpx）で起こし、stdout を**バイトのまま** UTF-8 として読んだ JSON-RPC の `tools/call` 応答に、すでに文字化けした文字列が入っている（クライアント側の復号の問題ではない）。
  - `find_references_and_implementations`（target = `httpx/_urls.py` の `URL.copy_with`、query_type=references。LSP が standby のとき）の応答の警告行:
    `! Warning: LSP �����p�ł��Ȃ����ߍ\���؂݂̂ŉ������܂����B�Q�Ƃ͎��ʎq�̈�v�ŁA…`
    （ソースは `src/lucianke/tools/t06_find_references_and_implementations.py:68` の「LSP が利用できないため構文木のみで解決しました。…」。半角カナと `�` に化けている）。
  - `edit_symbol_body`（F-PY の `URL.is_absolute_url`、replace_body）の応答: `files_changed: httpx/_urls.py (+0 -0)  sha256 5a05d1b4e3bc�c��5a05d1b4e3bc�c`（sha の間は `→` のはず）。
  - ASCII の部分は正しい。日本語・矢印だけが化ける。
  - 回収を試みた（化けた文字列を cp932 に符号化して UTF-8 で復号）が `invalid start byte` で失敗した＝化けた時点で情報が失われている。
- 影響の見込み: エージェントが受け取る警告・案内（降格の理由など）が読めない。トークンも実際より多く数えられる（化けた文字は 1 文字あたり多くのトークンになる）。ベンチの L は、日本語の警告を含む応答（構文木のみの降格応答）でこの分だけ大きく出る。
- 再現手順:
  1. `uv run python bench/fetch_fixtures.py F-PY`
  2. `PYTHONUTF8=1` で `python -P -m lucianke` を cwd = `bench/.cache/httpx` で起こし、`initialize` → `notifications/initialized`（roots/list にその cwd を返す）→ 走査が済んだ後、LSP が ready になる前に `tools/call find_references_and_implementations`（上の引数）を呼び、応答の `result.content[0].text` を UTF-8 で復号して警告行を見る。
- 調査はしていない（直す役の仕事）。`PYTHONUTF8` を外した場合は未確認。
