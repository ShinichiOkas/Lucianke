# BUG-0116 前置きの見積もりが、そのページに出ない降格の注意文まで数えて予算を余らせる（T-01 compact で 106 対 約 30）

- 報告: 2026-10-31 / 見た人: BUG-0110 の直す役（予算の掃引を測っていて見た）
- 見たことだけを書く（調査していない。直し方も考えていない。調査は直す役の仕事）

## 実測

`tests/contract/test_T01_repo_map.py` の F-POLY（`pipeline` の fixture）で `get_repo_map {"format": "compact", "max_tokens": 260}`:

- `mcp/fit.pack_and_render` が前置きとして数えた 0 件のページ（`reserved_tokens`）は 106 トークン。描かれた 0 件のページは:

  ```
  # get_repo_map ok conf=heuristic lsp=n/a rev=WORKING_TREE ws=poly ~260/260tok
  ! DEGRADED: confidence=heuristic engine=tree-sitter lang=python,typescript,cobol
  ! Warning: 言語の文法が無いため ctags とブロック推定で境界を求めました。終了行は推定であり、編集前に `get_symbol_bodies` で範囲を確かめてください。
  ```

- 実際に返った応答は `~182/260tok` で、ヘッダは `conf=structural`、`! DEGRADED` と `! Warning:` の行は無い（返ったのは
  `src/shop/services/limits.py` の 1 件だけ）。前置きは約 30 トークン。
- パッカーは予算 260 を使い切ったと見ている（`used_tokens_est` の見込み 260）。先頭のファイルは縮退版（シンボル 0 件・
  `rank` の行だけ）で、要約は `# summary: 2…` に切られている。実際には約 78 トークン余っている。
- `max_tokens=300` でも同じく、パッカーの見込み 297 に対して実際は `~220/300tok`。

## 再現

1. `tests/contract/test_T01_repo_map.py` の `pipeline` を使い、`repo_map(pipeline, "compact", max_tokens=260)` を呼ぶ。
2. ヘッダの `~used` と、`Packer.pack` に渡る `reserved_tokens`（106）を比べる。

## 利用者（呼ぶ側の AI）として困ること

- 予算の 3 割ほどが使われずに返り、先頭のファイルが必要以上に縮む（シンボルが消える）・要約が切れる。
- 後ろに精度の低いファイル（COBOL など）が 1 つでもあると、その注意文の分を毎ページ取り分けている。
