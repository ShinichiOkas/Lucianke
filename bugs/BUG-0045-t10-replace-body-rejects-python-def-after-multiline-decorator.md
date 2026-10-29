# BUG-0045 T-10 replace_body: 複数行のデコレータを持つ Python の定義全体を渡すと「定義で始まらない」と拒否される

- 報告: 2026-10-29 / 見た人: 22c（bench/run_edit_safety.py、F-PY=httpx b5addb6 の複製、HEAD 7cc12a3 時点の作業ツリー）
- 症状: `edit_symbol_body` の `replace_body` に、対象の定義を **そのまま** 渡した恒等編集（`new_code` = 定義の範囲の行頭〜行末。デコレータ行を含む）が `E_SYNTAX_INVALID`（"new_code does not start with a definition (pass the whole definition, signature included)"）で拒否された。ファイルは変わらない。1,000 件の標本のうち F-PY の 13 件（恒等 6・変換つき 7）。
  - 該当は全て、デコレータの引数が複数行にわたる定義（`@pytest.mark.parametrize(\n    ("encoding", "expected"),\n    (...),\n)` の次に `def test_...`）。例: `tests/test_utils.py` の `test_guess_by_bom`（39〜47 行: `@pytest.mark.parametrize(` … `)` … `def test_guess_by_bom(encoding, expected):`）、`tests/test_utils.py` の `test_get_environment_proxies`、`tests/client/test_proxies.py` の `test_for_deprecated_proxy_params`・`test_proxies_environ`、`tests/client/test_auth.py` の `test_digest_auth`、`tests/models/test_responses.py` の `test_link_headers`・`test_json_without_specified_charset`、`tests/models/test_headers.py` の `test_obfuscate_sensitive_headers`、`tests/models/test_queryparams.py` の `test_queryparams`、`tests/test_decoders.py` の `test_text_decoder_with_autodetect`、`tests/models/test_url.py` の `test_ipv6_url_copy_with_host`、`tests/test_wsgi.py` の `test_wsgi_server_port`。
  - 1 行のデコレータ（`@property` など）を持つ定義は通っている。
- 再現手順: `uv run python bench/run_edit_safety.py --fixtures F-PY --scale 0.3 --no-lsp --no-stdio`、結果の JSON の `refused_samples`。または `bench/.cache/httpx` の複製で `edit_symbol_body`（target: path=tests/test_utils.py, symbol_name=test_guess_by_bom, operation=replace_body）に、その定義の行（39〜行末）をそのまま `new_code` で渡す。
- 調査はしていない（直す役の仕事）。

## 原因
- `edit/builders.py` の `_starts_definition` が、Python と Rust の `new_code` について「最初の意味のある行が定義の開始語（`def ` / `async def ` / `class `、Rust は `fn ` / `pub ` / `struct ` …）で始まるか」を **行頭の文字だけで** 判定していた。読み飛ばす行は `@` `#` `//` `/*` `*` `"""` で始まる行に限られていた。
- 1 行のデコレータ（`@property`）は `@` の行を飛ばして次の `def` に届く。複数行のデコレータでは、2 行目以降（`    ("encoding", "expected"),` `    (` `)` など、デコレータの引数の続き）が `@` でも `#` でもないので「最初の意味のある行」になり、開始語で始まらないと判定されて `E_SYNTAX_INVALID`（"new_code does not start with a definition"）になった。判定は構文検証より前なので、`new_code` が有効な構文でも拒否された。
- Rust も同じ形で壊れる（`#[derive(` … `)]` や `#[cfg_attr(` … `)]` の 2 行目以降、`/* … */` の 2 行目以降が `*` で始まらないブロックコメント）。ただし Rust はシンボル抽出が未対応（`E_UNSUPPORTED`）なので T-10 の入口で止まり、ベンチには出ていなかった。
- 範囲側（`syntax/boundaries.py` の `definition_range`）と TypeScript（開始語の表が無く構文検証だけで判定）は正しかった。仕様書 T-10 処理規約 1・2 どおり、`new_code` はデコレータを含む定義の範囲全体を置き換える。

## 直し方
- 行頭の文字による判定をやめ、**構文木で判定** する。`edit/syntax_check.py` に `starts_with_definition(language_id, new_code)` を足した。`new_code` を共通先頭空白を除いて解析し、ルート直下の最初の文（Python はコメントを、Rust はコメントと `attribute_item` を読み飛ばす）が定義ノード（Python: `function_definition` / `class_definition` / `decorated_definition`、Rust: `*_item` と `use_declaration` / `extern_crate_declaration`）かを返す。デコレータは Python では `decorated_definition` の中に入るので、複数行でも入れ子の括弧・文字列やコメントの中の `)` でも影響しない。
- 判定できないとき（Python・Rust 以外、文法なし、`new_code` 自体に構文エラーがある）は `None` を返し、`builders.replace_body_builder` は従来どおり後続の `check_fragment`（構文検証）に任せる。本体の文だけ（`return a + b`、`let y = 2;`）は引き続き「定義で始まらない」で拒否する（`details.hint` も同じ）。
- `builders.py` の `_DEFINITION_STARTS` / `_SKIPPED_LINE_STARTS` / `_starts_definition` は削除。
- テスト: `tests/contract/test_T10_replace_decorated.py`（新規 22 件。複数行のデコレータ・入れ子の括弧・コメント混じり・積み重ね・async・装飾つきクラス・字下げ/字下げなしのメソッド・CRLF の `new_code` の恒等 replace_body がバイト一致〈EP-6〉、本体/デコレータだけ変えた replace_body が 1 か所だけ変わる、定義でない `new_code` は引き続き拒否、TypeScript の `@Component({…})`・`@Input({…})`・`@HostListener(…, […])` の複数行デコレータ）、`tests/unit/test_edit_syntax_check.py`（`starts_with_definition` の Python・Rust・対象外言語 25 件）。HEAD（直す前）では Python の 10 件が赤、TypeScript は元から通る。

## 確認
- 回帰テスト `tests/contract/test_T10_replace_decorated.py` を直す前の HEAD（使い捨ての作業ツリー）で走らせて Python の 10 件が赤（"new_code does not start with a definition"）になることを確かめ、直したあとは 22 件 green。
- httpx（`bench/.cache/httpx`）の装飾つき定義 239 件（うち複数行のデコレータ 30 件）の範囲の本文を `new_code` として `starts_with_definition` と `check_fragment` に通し、全件が「定義で始まる」かつ構文 ok（ticket の 13 件を含む）。
- `uv run python bench/run_edit_safety.py --fixtures F-PY --scale 1.0 --no-lsp --no-stdio`（1,000 件）: success=1000/1000、refused=0、EP-6 恒等 300/300、ジャーナル復元 50/50。ticket の再現手順（`--scale 0.3`）も 300/300・refused=0。
- `uv run python bench/run_edit_safety.py --scale 0.2 --no-stdio`（LSP つき F-PY + F-TS、200 件）: verdict=pass、200/200、refused=0、EP-6 60/60、ジャーナル 50/50。
- `tests/contract/test_T10_*.py`・`tests/unit/test_edit_*.py` を green（445 件 + 追加分）、`ruff check` は green。
