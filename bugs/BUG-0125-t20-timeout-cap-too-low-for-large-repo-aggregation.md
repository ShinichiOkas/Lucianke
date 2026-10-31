# BUG-0125 T-20 `query_graph` の時間の上限（既定 300 ms・最大 5 秒）では、大きなリポジトリの集計が打ち切られる

- 報告: 2026-10-31 / 見た人: 親（Mnemo の構造分析〈Mnemo の `PROJECT_STRUCTURE.md` §10〉を T-20 で集計していて）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（Mnemo の固定の写し・索引 4,221 ファイル / 65,824 シンボル / 約 38〜43 万行の relations）

- 既定（`timeout_ms` を渡さない = 300 ms）では、パッケージ間の依存を数える集計（relations と symbols を結合して `GROUP BY`）が `E_TIMEOUT` で打ち切られた。
- `timeout_ms: 5000`（引数の上限。`QUERY_TIMEOUT_MAX_MS`）にすると、同じパッケージ間の集計は 0.7 秒で返った。
- 全関係をシンボルごとに数える集計（被参照の多いモジュール・シンボル〈fan-in〉の上位）は、5 秒でも打ち切られた。これ以上は引数で延ばせない。
- 定数は `QUERY_TIMEOUT_DEFAULT_MS = 300`（`timeout_ms` の `Field(default=..., ge=QUERY_TIMEOUT_MIN_MS, le=QUERY_TIMEOUT_MAX_MS)`）。

## 利用者（呼ぶ側の AI）として困ること

- 大きなリポジトリの構造を調べる集計（依存の向き・被参照の多いものの上位）が、T-20 では出せない。結局 SQLite を直接開くか、grep で数え直すことになった。
- 既定の 300 ms で打ち切られた応答から、どこまで延ばせば返るかが分からない（5 秒を指定して、初めて足りるか分かった）。

## 再現

1. Mnemo（4,000 ファイル級）を `configure_workspace` で索引し、索引の完了を待つ。
2. T-20 に、relations 全体をシンボルごとに `GROUP BY` して件数の上位 20 を出す問い合わせを、`timeout_ms: 5000` で渡す → `E_TIMEOUT`。
