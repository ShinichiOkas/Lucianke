# BUG-0002 stdout 純度の不変条件テストが、ツールの追加で落ちる

- 報告: 2026-10-29 14:00 / 見た人: グラフ出力の担当、骨格の担当（全体 pytest を実行した）
- 症状: `tests/invariants/test_stdout_purity.py` の 2 件が落ちる。サーバーに `get_file_skeleton` が登録され、`tools/list` の結果が `["ping"]` だけではなくなったため（骨格の担当の観察）。
- 再現手順: 縦切りの結線（T-02 の登録）が入った作業ツリーで `uv run pytest -q tests/invariants/test_stdout_purity.py`。
- 調査はしていない（直す役の仕事）。

## 原因
`tests/invariants/test_stdout_purity.py` が `tools/list` の結果を `["ping"]` と決め打ちしていた（dev_tools 無効の構成では `== []` とも決め打ち）。製品のツール（`get_file_skeleton`、`get_symbol_bodies`）が登録されると、不変条件の本質（stdout の純度・noise の隔離・EOF での終了）と無関係な部分で落ちた。

## 直し方
- `tools/list` は「`ping` を含む」ことだけを確かめ、スキーマの検査は一覧の先頭ではなく `ping` のエントリに対して行う。
- dev_tools 無効の構成は「`ping` が一覧に無い」ことだけを確かめる（製品のツールは並ぶので空は求めない）。
- 純度（全行 JSON-RPC・LF 終端）、noise の隔離、行数 5、stderr に noise が 2 回以上（print と os.write(1, …) の両方が出た歯の確認）、EOF 後 2 秒以内の終了コード 0 は弱めていない。陽性対照は元々このファイルには無く（stderr 側の noise 計数が「noise が実際に出た」ことの確認を兼ねる）、それが引き続き効いている。

## 確認
`uv run pytest -q tests/invariants/test_stdout_purity.py` を 3 回続けて 4 passed。
