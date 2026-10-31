# BUG-0130 `uv run ruff format --check <変えた .py>` が、変えていない既存の行で落ちる（リポジトリ全体では 113 ファイル）

- 報告: 2026-10-31 / 見た人: BUG-0127 を直した役（直す役の規約の `uv run ruff format --check <変えた .py>` を流したとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 症状

`origin/main` = `8b9b2c9`、ruff 0.17.3、`pyproject.toml` の `line-length = 110`。

```
> uv run ruff format --check src tests
113 files would be reformatted, 281 files already formatted
```

BUG-0127 で触ったファイルの、自分の変更と関係のない既存の行の差分（`ruff format --diff` の塊の数）:

| ファイル | 塊 |
|---|---|
| `src/lucianke/engine/indexer.py` | 7（変更前の `ee3b5c6` の版でも 8） |
| `src/lucianke/tools/_runtime.py` | 8 |
| `src/lucianke/__main__.py` | 1（30 行目 `if args[:1] == [_SELFTEST_SUBCOMMAND]:  # doctor が exe 自身を…`） |
| `tests/unit/test_tools_runtime.py` | 15 |

差分の例（`indexer.py`、変更前の版でも同じ）:

```
-_JS_LANGUAGES: Final = frozenset({"typescript", "tsx", "javascript", "jsx"})  # バレルの公開する名前を書く言語（BUG-0104）
+_JS_LANGUAGES: Final = frozenset(
+    {"typescript", "tsx", "javascript", "jsx"}
+)  # バレルの公開する名前を書く言語（BUG-0104）
```

見た塊はどれも、行末の日本語のコメントか日本語の文字列を含む行だった。

## 困ること

- 直す役の規約の `ruff format --check <変えた .py>` が、変えたファイルが既存の行を含むだけで落ちる。直す役は「自分の行だけ整えた」
  ことを差分の塊の数の比較で確かめるしかない（BUG-0127 ではそうした）。
- ファイル全体に `ruff format` をかけると、変更と関係のない行が大量に動き、並行する他の直す役の変更とぶつかる。

## 再現

`origin/main` で `uv run ruff format --check src tests`、または上のファイルに `uv run ruff format --diff <ファイル>`。
