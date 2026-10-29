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

## 原因
- **サーバーの不具合ではない。再現せず**: サーバーの stdout / stdin は、環境によらず UTF-8。化けは**観測側**（応答を復号した文字列を cp932 の stdout に print し、その出力を UTF-8 として読んだ）で起きていた。
- サーバー側は `bootstrap_io.open_transport_streams` が本物の stdin / stdout を `encoding="utf-8"` 固定の `TextIOWrapper`（`newline="\n"`）で包み、MCP SDK の `stdio_server(stdin=…, stdout=…)` へ明示的に渡している。`src` のほかに `reconfigure`・`TextIOWrapper` で fd 1 / fd 0 の符号化を触る所は無い（`sys.stdout = sys.stderr` は fd 2 にしか届かない）。ロケール・`PYTHONUTF8`・`PYTHONIOENCODING` は経路に入らない。
- 実測（実プロセスを `python -P -m lucianke` / `uv run --no-sync --directory … lucianke` で起こし、生バイトを読んだ）。いずれも応答の行は厳密な UTF-8 で、`LSP が…`（T-06 の降格の警告）は `\xe3\x81\x8c…`、`→` は `\xe2\x86\x92` だった:
  1. `PYTHONUTF8` なし（日本語 Windows の既定。ロケール = cp932）
  2. `PYTHONUTF8=1`（票の再現条件）
  3. `PYTHONUTF8=0` + `PYTHONIOENCODING=cp932`
  4. `uv run lucianke`（`.mcp.json` の本番の起動経路。コンソールスクリプト、`PYTHONUTF8` なし）
  stdin も同じ: 日本語と `→` を UTF-8 の生バイトで `new_code` に載せた `edit_symbol_body` が、ディスクへ UTF-8 で正しく書かれた。T-10 の応答は `files_changed: a.py (+2 -1)  sha256 4e9e25332c1c…→f5d48f125722…`（`…` は U+2026、`→` は U+2192）。
- 票の化けの形は、cp932 の符号を UTF-8 として復号（不正バイトを `�` に置換）した形と**一致する**: `"LSP が利用できないため構文木のみで解決しました。".encode("cp932").decode("utf-8", "replace")` は `LSP �����p�ł��Ȃ����ߍ\���؂݂̂ŉ������܂����B` になる（`で`→`�ł`、`な`→`�Ȃ`、`用`→`�p`、`解決`→`ŉ`）。T-10 の `…`（cp932 で 0x81 0x63）と `→`（0x81 0xA8）は `�c` と `��` になり、票の `bc�c��5a05` と一致する。
- つまり、観測した Python のプロセスは stdout がパイプ（ロケールの cp932。`PYTHONUTF8` なし）で、復号した正しい文字列を cp932 で書き出し、受け取る側（ツールの出力）が UTF-8 として読んだ。**化けた時点で情報が失われる**（置換復号は不可逆）ため、票の「回収」が失敗したのもこのせい。この調査の途中で、親の `print` が同じ化けを出すのを自分でも再現した（生バイトの行は正しく、`print(line.decode("utf-8"))` の行だけが化けた）。
- 票の「サーバーの応答に、すでに文字化けした文字列が入っている」という前提は成り立たない（バイトのまま読んだのは正しいが、その後に出力した経路が cp932 だった）。ベンチの L のトークン数は、サーバーのバイトを `bytes.decode("utf-8")` した文字列で数えていれば影響を受けない（`bench/run_tokens.py` は `sys.stdout.reconfigure(encoding="utf-8")` 済みで、サーバーの応答は UTF-8 で復号している）。

## 直し方
- 製品コードは変更なし（直す所が無い）。不変条件として固定する回帰テストを足した。
- `tests/invariants/test_stdout_purity.py`: `RawSession` に `env_overrides`（`None` は環境から外す）・`cwd`・`ensure_ascii`（False で非 ASCII を UTF-8 の生バイトのまま stdin に送る）を足し、`test_stdout_and_stdin_are_utf8_whatever_the_locale` を `PYTHONUTF8` なし／`PYTHONUTF8=0` + `PYTHONIOENCODING=cp932` の 2 環境で走らせる。実プロセスを起こし、roots を渡して索引が使えるまで待ち（固定の sleep なし）、日本語と `→` を含む `new_code` を stdin に UTF-8 の生バイトで送って `edit_symbol_body` を呼ぶ。確かめるのは、応答の `files_changed` に `…→`、diff に日本語がそのまま返ること、ディスクのファイルを UTF-8 で読めること、stdout の全行が厳密な UTF-8 で JSON-RPC の純度が保たれること、`→` が UTF-8 の 3 バイトで線に載ること。
- 観測の作法（再発防止）: サーバーの応答を人が読む経路に出すときは、`ascii(text)` で出すか `sys.stdout.buffer.write(text.encode("utf-8"))`、または `sys.stdout.reconfigure(encoding="utf-8")`（クライアント側に `PYTHONUTF8=1`）にする。サーバーの出力が化けたと思ったら、まず生バイトを `.decode("utf-8")` して比べる。

## 確認
- 上の 4 環境で実プロセスの生バイトが UTF-8 であることを確認（T-06 の降格の警告、T-10 の `→` と `…`、stdin の日本語コメントがディスクに UTF-8 で書かれる）。
- `tests/invariants/test_stdout_purity.py` を 3 回続けて green（6 件）。歯の確認: stdout の包みを cp932 に差し替えた複製（`PYTHONPATH` で先に読ませた使い捨て）では、このテストが落ちる。
