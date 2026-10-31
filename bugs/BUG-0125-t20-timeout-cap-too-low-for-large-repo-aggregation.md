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

## 原因・直し方・確認（2026-10-31）

### 原因

1. 上限（引数の最大 5 秒・既定 300 ms）が固定で、索引の大きさと無関係だった。
2. 実測（合成の索引: 4,000 ファイル・65,000 シンボル・40 万行の関係・88 MB）で、シンボルを主キー（`urn`）で引く結合つきの集計
   （`relations` ⨝ `symbols` ⨝ `files` を `GROUP BY`）が **9〜12 秒**かかった。実行計画は妥当（`SCAN r` → `SEARCH s/t USING INDEX
   sqlite_autoindex_symbols_1`）で、遅さの正体は **SQLite の既定のページキャッシュ 2 MiB**。公開接続は 1 要求 1 接続で、結合の
   ランダムな引き当てがキャッシュに乗らず、同じページを読み直していた（`cache_size=-65536` で 1.7 秒、`mmap_size` も付けて 1.4〜1.5
   秒。同じ SQLite を素で開いて測った）。`relations` だけの `GROUP BY target_urn`（fan-in の上位）は 0.08 秒で、索引
   `idx_rel_target` が効く。票の 5 秒超の fan-in は結合つきの形と見られる。
3. 打ち切りの応答は `details.timeout_ms` と `elapsed_ms` だけで、どこまで延ばせば返るかも、どう書き直せば速いかも分からなかった。

### 直し方

- **引数の上限を 30 秒に**（`QUERY_TIMEOUT_MAX_MS`。呼ぶ側の MCP の上限 60〜120 秒より短い）。
- **既定を索引の大きさで決める**（`timeout_ms` を渡さない。索引ファイル 1 MB あたり 60 ms・下限 300 ms・上限 10 秒。88 MB で約 5 秒）。
  小さな索引は今までどおり 300 ms で暴走を早く止める。実際に使った期限は `data.timeout_ms` に返す。スキーマは `int | None`（既定 null）。
- **公開接続のページキャッシュを 64 MiB・mmap を 256 MiB に**（接続ごと・閉じれば返る）。上の結合つきの集計が 9.8 秒 → 0.9〜1.7 秒。
  スキーマ（索引）側は変えない（索引は足りていた。表の変更・移行も要らない）。
- **打ち切りの手がかり**: `E_TIMEOUT` の `details` に `max_timeout_ms`・`can_extend`・`suggested_timeout_ms`（4 倍・上限まで）・
  `full_scans[]`（`EXPLAIN QUERY PLAN` で全件を読む表）・`hint`（大きな表を単独で集計してから結合する・索引のある列で絞る）を足した。
  部分結果（取得済みの行）は今までどおり `data` に載る。
- 仕様書 T-20（入力表・処理規約 5・5a・5b・data・エラー・事後条件 4）と設計書 §7.6.4 を直した。

### 確認

- 合成の索引（88 MB）で、結合つきの集計は既定（5,019 ms）で 0.9 秒で返り、300 ms では `E_TIMEOUT` と手がかり
  （`suggested_timeout_ms: 1200`・`hint`）が返る。`fan-in`（結合あり）は 0.17 秒。
- 新しい試験: `tests/unit/test_index_public.py` に 6 件（上限 30,000 まで通る・既定が大きさで延びる・キャッシュと mmap・打ち切りの
  手がかり・上限での打ち切りは延ばせない）、`tests/contract/test_T19_T20.py` に 1 件（`data.timeout_ms`・`details`）。既存の
  範囲外の試験（5001）は 30001 に、`tests/unit/test_core_limits.py` の定数表は新しい値に直した。
- 残り: 実機の Mnemo の索引での再測は親に託す（合成の索引は URN をランダムに引くので、実物より引き当てが散る側）。
