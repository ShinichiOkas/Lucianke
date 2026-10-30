# BUG-0073 CI と同じ `uv run ruff check`（引数なし＝リポジトリ全体）が bench の 2 本の台本で 4 件落ちる

- 報告: 2026-10-31 / 見た人: BUG-0068 を直した役（自分の変更を CI と同じ形で lint したとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 症状

`origin/main` = `c149e0d`（BUG-0068 の変更は `bench/tokens/` だけで、下の 2 ファイルは触っていない）、ruff 0.17.3:

```
> uv run ruff check --output-format concise
bench\_budget_probe.py:7:1: I001 [*] Import block is un-sorted or un-formatted
bench\_bug0026_repro.py:17:8: F401 [*] `tempfile` imported but unused
bench\_bug0026_repro.py:196:11: F541 [*] f-string without any placeholders
bench\_bug0026_repro.py:213:19: F541 [*] f-string without any placeholders
Found 4 errors.
[*] 4 fixable with `--fix`.
```

- `.github/workflows/ci.yml` の lint の段は `uv run ruff check`（引数なし）。票や HANDOFF の確認手順は `uv run ruff check src tests` で、こちらは All checks passed（bench を見ていない）。
- 2 ファイルの最後のコミット: `bench/_bug0026_repro.py` は `bc53333`（BUG-0026）、`bench/_budget_probe.py` は `87d9451`。

## 再現

```powershell
uv run ruff check
```
