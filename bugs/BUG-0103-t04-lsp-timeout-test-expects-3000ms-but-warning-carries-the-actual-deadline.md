# BUG-0103 `test_T04_lsp.py::test_fake_timeout_degrades_to_structural_and_skips_the_rest_of_the_call` が `origin/main` で落ちる（警告の期限の値が 300 ms で、試験は 3000 ms を期待している）

- 報告: 2026-10-31 / 見た人: BUG-0100・BUG-0101 を直す役（T-04 の契約テストを流していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

- `bc63ffa` の `src` だけで（BUG-0100・BUG-0101 の変更を載せずに。`PYTHONPATH` で `bc63ffa` の `src` を先にして）
  `uv run pytest -q -p no:cacheprovider tests/contract/test_T04_lsp.py -k fake_timeout` → `1 failed`。
- `5af9c49` の `tests/contract/test_T04_lsp.py` の 490 行目は今も `selector.warning_text("lsp_timeout", timeout_ms=3000)` を期待している。

```
E           AssertionError: assert 'LSP の応答が 300...ため構文木で解決しました。' == 'LSP の応答が 300...ため構文木で解決しました。'
E             - LSP の応答が 3000 ms 内に得られなかったため構文木で解決しました。
E             ?             -
E             + LSP の応答が 300 ms 内に得られなかったため構文木で解決しました。
```

- 試験は `monkeypatch.setattr(features, "LSP_TIMEOUT_DEFINITION_MS", 300)` で期限を 300 ms にしている。
  `d2ac0fc`（BUG-0091）のコミット文には「セレクターの lsp_timeout の雛形もその値（実際に切れた期限）を書く」とある。

## 困ること

- T-04 の契約テストがいつも 1 件赤い（全体テストで毎回落ちる。他の役が自分の変更で落ちたのかを見分けにくい）。
