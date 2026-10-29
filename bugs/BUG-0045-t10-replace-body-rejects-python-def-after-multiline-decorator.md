# BUG-0045 T-10 replace_body: 複数行のデコレータを持つ Python の定義全体を渡すと「定義で始まらない」と拒否される

- 報告: 2026-10-29 / 見た人: 22c（bench/run_edit_safety.py、F-PY=httpx b5addb6 の複製、HEAD 7cc12a3 時点の作業ツリー）
- 症状: `edit_symbol_body` の `replace_body` に、対象の定義を **そのまま** 渡した恒等編集（`new_code` = 定義の範囲の行頭〜行末。デコレータ行を含む）が `E_SYNTAX_INVALID`（"new_code does not start with a definition (pass the whole definition, signature included)"）で拒否された。ファイルは変わらない。1,000 件の標本のうち F-PY の 13 件（恒等 6・変換つき 7）。
  - 該当は全て、デコレータの引数が複数行にわたる定義（`@pytest.mark.parametrize(\n    ("encoding", "expected"),\n    (...),\n)` の次に `def test_...`）。例: `tests/test_utils.py` の `test_guess_by_bom`（39〜47 行: `@pytest.mark.parametrize(` … `)` … `def test_guess_by_bom(encoding, expected):`）、`tests/test_utils.py` の `test_get_environment_proxies`、`tests/client/test_proxies.py` の `test_for_deprecated_proxy_params`・`test_proxies_environ`、`tests/client/test_auth.py` の `test_digest_auth`、`tests/models/test_responses.py` の `test_link_headers`・`test_json_without_specified_charset`、`tests/models/test_headers.py` の `test_obfuscate_sensitive_headers`、`tests/models/test_queryparams.py` の `test_queryparams`、`tests/test_decoders.py` の `test_text_decoder_with_autodetect`、`tests/models/test_url.py` の `test_ipv6_url_copy_with_host`、`tests/test_wsgi.py` の `test_wsgi_server_port`。
  - 1 行のデコレータ（`@property` など）を持つ定義は通っている。
- 再現手順: `uv run python bench/run_edit_safety.py --fixtures F-PY --scale 0.3 --no-lsp --no-stdio`、結果の JSON の `refused_samples`。または `bench/.cache/httpx` の複製で `edit_symbol_body`（target: path=tests/test_utils.py, symbol_name=test_guess_by_bom, operation=replace_body）に、その定義の行（39〜行末）をそのまま `new_code` で渡す。
- 調査はしていない（直す役の仕事）。

## 原因
（未）

## 直し方
（未）

## 確認
（未）
