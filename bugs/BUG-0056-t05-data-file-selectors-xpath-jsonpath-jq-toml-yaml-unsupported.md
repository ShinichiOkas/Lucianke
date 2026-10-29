# BUG-0056 get_symbol_bodies が `xpath:` `jsonpath:` `jq:` `toml:` `yaml:`（Tier 3 のデータファイル）のセレクタを返せない（E_UNSUPPORTED）。仕様書 §11 T-05 処理規約 3

- 報告: 2026-10-29 / 見た人: BUG-0049 を直す役（`cell:` の結線のとき）/ 見たことだけ（調査・修正はしていない）
- 見たこと（F-POLY `tests/fixtures/poly` をルートに、パイプライン経由で `get_symbol_bodies` を `format: json` で呼んだ）:
  - `{"path": "conf/settings.json", "symbol_name": "jsonpath:$.a"}` -> `E_UNSUPPORTED`、message `selector jsonpath: is not supported in this build (code symbols only)`、`details.reason = selector_not_supported`。
  - `{"path": "conf/app.yaml", "symbol_name": "yaml:a"}` と `{"path": "reports/junit.xml", "symbol_name": "xpath:/testsuite"}` も同じ。
  - 部品は在る: `src/lucianke/syntax/adapters/{json_,yaml_,toml_,xml_}.py` の `select`（`FormatAdapter.select`。`syntax/support.py`）。`tools/t05_get_symbol_bodies.py` の `_plan` は `cell:` 以外のセレクタを `_resolve` に回し、`_resolve` が一律に断る。
- 仕様書: §11 T-05 処理規約 3（`xpath:` は XML / HTML / SVG、`jsonpath:` と `jq:` は JSON / JSONL、`toml:` は TOML、`yaml:` は YAML。ファイル種別に合わないセレクタは `E_UNSUPPORTED`、構文誤りは `E_INVALID_ARGUMENT`、一致が複数なら最初の 20 件を 1 項目内に並べ総数を `match_count` に書く）。§8.2 の表で Tier 3 の T-05 は ○（セレクタ）。
- 影響: Tier 3 のデータファイルの部分取得（FR-NAV-2）ができない。事後条件の番号付きの試験は無い（`tests/contract/test_T05_bodies.py` は `.py` への `xpath:` が `E_UNSUPPORTED` になることだけ見ている）。
- 再現: 上の呼び出しを `tests/contract/test_T05_bodies.py` の `pipeline` フィクスチャ経由で送る。
