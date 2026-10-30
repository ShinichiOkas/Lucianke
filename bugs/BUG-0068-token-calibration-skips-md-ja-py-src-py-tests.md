# BUG-0068 トークン計数の較正テストが md_ja・py_src・py_tests の 3 種で skip（記録と一致する標本が少ない）＝ §5.7 の誤差 ±10% を検証していない

- 報告: 2026-10-31 / 見た人: BUG-0065 を直した役（全体テストを流したとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 症状

`uv run pytest -q -p no:cacheprovider -rfs`（`9d96cd2` + BUG-0065 の直し、2026-10-31）: `3783 passed, 32 skipped`。skip の 32 件のうち 3 件が次のもの。

```
SKIPPED [1] tests\unit\test_core_tokens_calibration.py:142: md_ja: 記録と一致する標本が少ない（書き換わった）。calibrate.py で result.json を更新する
SKIPPED [1] tests\unit\test_core_tokens_calibration.py:142: py_src: 記録と一致する標本が少ない（書き換わった）。calibrate.py で result.json を更新する
SKIPPED [1] tests\unit\test_core_tokens_calibration.py:142: py_tests: 記録と一致する標本が少ない（書き換わった）。calibrate.py で result.json を更新する
```

- skip しているのは `test_total_error_within_ten_percent_per_kind`（種類ごとの推定誤差が ±10% 以内か）。日本語の Markdown・`src` の Python・`tests` の Python の 3 種で、仕様書 §5.7 の「誤差 ±10% 以内」を確かめていない。
- skip の理由の文は「calibrate.py で result.json を更新する」と手順を書いている。
- BUG-0065 を直す前の全体テスト（BUG-0065 の票。`32 skipped`）でも skip の件数は同じ 32 件だった（内訳は見ていない）。

## 再現

```powershell
uv run pytest -q -rs tests/unit/test_core_tokens_calibration.py
```
