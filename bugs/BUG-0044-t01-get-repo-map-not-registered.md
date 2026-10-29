# BUG-0044 T-01 get_repo_map が tools/list に無い（M1 の 21 本のうち 20 本）

- 報告: 2026-10-29 / 見た人: dogfood-2（実 stdio クライアント、使い捨て作業ツリー b24f766）
- 症状: `tools/list` に `get_repo_map`（T-01。仕様書 §11.0 の表で M1）が無い。サーバーのログは `lucianke 0.0.1 starting (tools=20)`。載っているのは T-00・T-02〜T-20 の 20 本。`get_repo_map` を呼ぶ口が無いので、`files[]` の `rank` の降順・`max_symbols_per_file`・`.gitignore`/`exclude_globs`・`page.total_estimate`（仕様書 T-01 の事後条件）が確かめられない。
  - 設計書 §2201 の表は `tools/t01_get_repo_map.py` を指すが、`src/lucianke/tools/` に `t01_*.py` は無い（`t00`・`t02`〜`t20` と `dev_ping.py` のみ）。
- 再現手順: `python -m lucianke` を起こして `tools/list`（SDK の `Client.list_tools()`）。
- 調査はしていない（直す役の仕事）。作業中の変更で追加済みなら、この票は閉じてよい。

## 原因
- 見た人の使い捨て作業ツリーのコミット b24f766（2026-10-29 14:59:55）は、T-01 `get_repo_map` を入れたコミット 458a4cb（15:00:31）より前だった。製品の不具合ではない。

## 直し方
- 直すものは無い。

## 確認
- 2026-10-29 15:20、HEAD（20fc5f0 以後）で `register_tools` を呼び、登録されたツールは 21 本、`get_repo_map` を含むことを確かめた（親）。
