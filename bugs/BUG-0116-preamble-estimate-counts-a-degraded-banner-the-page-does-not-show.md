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

## 原因・直し方・確認（2026-10-31）

直す役（BUG-0115 & BUG-0116 をまとめて）が調べて直した。

### 原因

`src/lucianke/mcp/fit.py` の `pack_and_render` の 1 の段（前置きを数える）が、0 件のページを `build_meta(out, None)`（**全レコード**の
精度の集計）で描いて数え、その値を `reserved_tokens` としてパッカーに渡していた。降格の注意文（`! DEGRADED` と `! Warning:`）・
ヘッダの `conf=`・json の `_resolution_meta` は、実際には **ページに入ったレコード** の集計（`build_meta(out, page)`）で出る。
後ろに精度の低いレコード（F-POLY の COBOL の heuristic）が 1 つでもあると、先頭の structural のファイルだけのページにも注意文の
分（約 76 トークン）を取り分けた。全レコードの集計は「入ったレコードの集計より長いことはあっても短いことはない」ので予算は
守られる（そのための設計だった）が、余った分は使われずに返った。詰めたあとの数え直し（3 の段）は超えたときに足すだけで、
余りを返す道が無かった。T-01 の compact で前置きは約 30 なのに 106 を取り分け、先頭のファイルは縮退版（シグネチャ・シンボルを
削った形）で、要約も切れていた。

1 つの値のまま「入りうるレコードの集計」にすると、予算を上げて heuristic のレコードが入りうる側に来た途端に前置きが約 76 増え、
返る件数が減る（BUG-0067 の単調性を壊す）ので、形ごとに前置きを変える必要があった。

### 直し方

- `Packer.pack` に `reserved_for(m)`（先頭から m 件のページの前置き。m について減らず、予算に依らない）を足した。鎖の各形
  （m 件目の縮退版・丸ごと）は自分の件数の前置きで「収まる」を判定し、要約の取り分（BUG-0067・BUG-0110 の `min(avail // 4,
  avail − F)`）も形ごとの `avail(m) = max_tokens − reserved_for(m)` で求める。各形の判定は予算について単調（予算 1 につき前置きは
  動かず、取り分は高々 1 増える）で、ページはその中で最も先の形なので、単調性はそのまま。`reserved_tokens` は 0 件のページ
  （1 件も入らない・詰めるものが無い）の前置き。1 件も入らないときの目安は `min(reserved_for(1) + 先頭 + # more:, reserved_for(全部)
  + 全部)`。省略すれば今までどおり全部の形で `reserved_tokens`。
- `pack_and_render` は、先頭から m 件の集計の水準が変わる位置で段に分け（ふつう 1〜2 段、最大で精度の水準の数）、段ごとに
  「その段の最後の形（先頭から段の終わりまで）の集計」で 0 件のページを描いて数え、前の段までの最大を `reserved_for` にする
  （段の最後の形は json の `confidence_breakdown` の件数も段の中で最大）。同じ集計は 1 度だけ描く。0 件のページの前置きは
  レコード 0 件の集計（`out.engine.confidence`）で数える（実際にそう描かれる）。3 の段の不足分は全部の形に足す。
- 仕様書 §5.7（前置きはページに出る形で数える。理由つき）、設計書 §7.2.2（擬似コードの `avail(m)`・応答全体の予算の項）に書いた。

### 確認

T-01（F-POLY・compact。先頭のページは structural のファイルだけ）:

| max_tokens | 直す前 | 直した後 |
|---|---|---|
| 260 | `~179/260`（69%）。`limits.py` は `retry @L20-24`・`schedule @L27-28` だけ（シグネチャなし） | `~244/260`（94%）。`limits.py` の 5 シンボルをシグネチャつきで丸ごと + ディレクトリの行 |
| 300 | `~215/300`（72%）。`limits.py` はシンボル 0 件 | `~293/300`（98%）。`limits.py` 丸ごと + `payment.py` の縮退版 |
| 400 | `~318/400`（80%）。2 ファイル | `~388/400`（97%）。3 ファイル |

どれも `! DEGRADED` は出ず（ページに heuristic のファイルが無い）、予算内。以前は 200 で 3 シンボル、260 で 2 シンボルと、予算を
上げてシンボルが減っていた（200 では T-01 がディレクトリの行を出さず、それで前置きの見積もりも違った）が、直した後は 200 で
2 シンボル、260 で 5 シンボル。

試験（足したもの）:

- 性質 `tests/unit/test_mcp_budget_fit.py`: exact 12 件の後ろに heuristic 1 件の答えを compact と json で掃引し、exact だけの
  ページには注意文が無く、余りが次のレコード 1 件ほどより小さい（以前は注意文の分が毎ページ余った）、heuristic が入るページには
  注意文があり予算内。既存の性質テスト（予算内・申告＝数え直し・カーソルの有無・単調・1 件以上・目安）は精度の混在を含む乱数の
  レコード列でそのまま通る。
- 契約 `tests/contract/test_T01_repo_map.py`: compact 260 / 300 / 400 で `conf=structural`・`! DEGRADED` なし・予算の 9 割以上を使い、
  先頭のファイルはシグネチャつき。
- 直す前の `fit.py` に戻すと上の 4 件が落ちる。直す前の `budget.py` / `fit.py` / `pipeline.py` に戻すと BUG-0115 の新しい性質・
  契約の試験 3 件が落ちる。
- 既存の試験の直し: `tests/contract/test_T02_data_files.py` の「先頭の 1 ファイルが深さ 1 に縮む最小の予算」は、カーソルが短く
  なって 230 では丸ごと入るので 160（縮む帯は 140〜193）にした。
- `uv run ruff check` は通る。`tests/unit/test_mcp_*`・`tests/unit/test_compact*`・compact の文法（`tests/contract/test_compact_grammar.py`）・T-01〜T-08 の契約・`tests/contract_fixtures`・e2e の縦切り: 762 件が通る（12 件は環境で飛ばす）。全体の試験は親が main でまとめて回す。
