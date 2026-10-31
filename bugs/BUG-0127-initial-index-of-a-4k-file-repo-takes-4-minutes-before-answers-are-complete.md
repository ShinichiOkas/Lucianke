# BUG-0127 4,000 ファイル級のリポジトリで、初回の索引が終わって答えがそろうまで 4 分前後かかる（Serena は最初の答えまで 8 秒）

- 報告: 2026-10-31 / 見た人: 親（能力ベンチ〈Mnemo の `bench/lucianke/`〉の準備の時間と、外部のコーディングエージェント ray からのフィードバックで）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

能力ベンチ（`bench/capability/run.py`、Mnemo を `d419cf0b` に固定した写し〈`git archive d419cf0b` を展開〉、Lucianke `cdc9257`）の準備の内訳
（Mnemo の `bench/lucianke/results/2026-10-31.md` の「Setup details」）:

| | Lucianke | Serena 2.0.0 |
|---|---|---|
| initialize | 1.7 s | 3.1 s |
| 索引のジョブの完了（`index_ready_ms`） | 213.0 s | （索引を先に作らない） |
| Python の型サーバーの準備（`lsp_ready_ms`） | 243.2 s | 最初の記号のツールの答えまで 8.3 s |
| 索引の規模 | 4,221 ファイル / 65,824 シンボル / 396,690 relations | — |

- 他の直す役が並行で走っていた 14:17 の回（BUG-0120 の直しの後の計測）は `index_ready_ms` 348,597 / `lsp_ready_ms` 369,268。
- ベンチの台本は、索引のジョブの完了と型サーバーの準備を待ってから課題を流している（`status` を 30 秒ごとに見る）。
- 外部のコーディングエージェント ray のフィードバック（2026-10-31・師匠経由）: `configure_workspace` の索引が、ray のサーバーごとの時間の上限 120 秒を超える。

## 利用者（呼ぶ側の AI）として困ること

- 中くらいのリポジトリを開いて最初の 4 分、参照・呼び出し階層などの構造の答えがそろっているか分からない（待つか、grep に戻るか）。
- 呼び出し側の時間の上限（ray は 120 秒）に引っかかる。

## 再現

Mnemo の README（`bench/lucianke/README.md`）の手順で、`d419cf0b` の写しに対して `uv run python bench/capability/run.py --tasks <Mnemo>/bench/lucianke/tasks.toml --workspace <写し> --engines lucianke` を流し、出力の `setup` の行を見る。
