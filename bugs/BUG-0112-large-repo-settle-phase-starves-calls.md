# BUG-0112 大きなワークスペースの初回索引で、取り込みの後に十数分 DB に書かないまま CPU を使い続け、その間の呼び出しが 2 分以上待たされる

- 報告: 2026-10-31 / 見た人: 親（能力測定の基準〈Mnemo の分析〉で Lucianke を Mnemo に向けたとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`main` = `8505b8d` 相当のソースから `uv run --project <lucianke> python -P -m lucianke`、cwd = 対象のワークスペース〈Python 約 2,000 本・.gitignore 適用後〉、`LUCIANKE_CACHE_DIR` は空から）

- 10:47 頃 `configure_workspace set_roots` → `jobs: job_0001 reindex running 0%`。以後 5 秒ごとに `configure_workspace status` を呼んだ。
- `paad.sqlite` の最終書き込み 10:50:15（225.5 MB、`-wal` 21.4 MB）。以後 11:04 まで書き込みなし（13 分以上）。
- サーバーのプロセス（`python -P -m lucianke`）: 起動から 17 分で CPU 1,024 秒（ほぼ 1 コアを使い切り）、作業セット 1,930 MB。
- サーバーのログ（`tool_call` の `timing_ms`）: `configure_workspace`（status）が 10:50 までは 1,411〜3,659 ms、10:52 以降は **119,180 / 152,739 / 130,175 / 132,641 / 139,299 / 137,721 ms**。
- 比較: 同じ版で Django 5.1（Python 2,788 本）は索引 337 秒で終わり、この症状は見えなかった。

## 利用者（呼ぶ側の AI）として困ること

- 大きなリポジトリでは、索引の途中で数分間どの呼び出しにも答えが返らない（BUG-0070 の「索引中でも答える」が効かない）。
- 索引がいつ終わるかが分からない（進みの表示が止まって見える）。
