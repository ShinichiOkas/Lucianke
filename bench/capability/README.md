# 能力ベンチ（capability benchmark）

同じ「AI エージェントが実際にやる仕事」を、複数のコード知能エンジンに答えさせて、正しさ・応答トークン・待ち時間・
立ち上がりの時間を並べる汎用の台本。**この台本はプロジェクトの中身を知らない**: 課題と正解（ground truth）は
解析対象のプロジェクトの側に置き（`--tasks`）、ワークスペースの場所は引数か環境変数で渡す。

| エンジン | 何を測るか |
|---|---|
| `lucianke` | Lucianke（stdio MCP。既定はソースから `python -P -m lucianke`、`--lucianke-exe` で配布物） |
| `serena` | [Serena](https://github.com/oraios/serena)（stdio MCP。LSP を使う記号の道具） |
| `grep` | 意味の道具を持たないエージェントの基準線（Grep と Read だけ） |

## 走らせ方

```
uv run python bench/capability/run.py --tasks <プロジェクト>/bench/lucianke/tasks.toml \
    --workspace <解析する木> --engines lucianke,serena,grep --out <結果の置き場> \
    --serena-cmd "<serena の実行ファイル>" --work-dir <キャッシュ・ログ・生の応答の置き場>
uv run python bench/capability/run.py --report --tasks <tasks.toml> --out <結果の置き場> --notes <考察.md>
```

* `--workspace` を省くと `LUCIANKE_BENCH_MNEMO`、それも無ければ `../Mnemo`。測るのは **固定した写し**
  （`git archive <SHA>` を展開したもの）にする。作業中の木を解析すると、走っている間の編集で結果がぶれる。
* `--serena-cmd` の既定は `uvx --from serena-agent==2.0.0 serena`。uv の隔離環境に入れた実行ファイルを渡してもよい。
  Serena の設定・ログ・メモリは `--work-dir/serena/serena-home`（`SERENA_HOME`）に閉じる。ウェブのダッシュボードと
  GUI のログ窓は切る。資格情報は渡さない。
* Lucianke は `LUCIANKE_CACHE_DIR` を `--work-dir/lucianke/cache`（毎回空）にして、毎回ゼロから索引する。
  立ち上がりの状態は `configure_workspace status` を `--poll-s` 秒（既定 30 秒）ごとに見る（遅い呼び出しを積まない）。
* `--only T06,T07` で課題を絞る。`--keep-text` で応答の全文を JSON に残す（既定は先頭 400 文字。全文は
  `--work-dir/<エンジン>/<課題>.txt`）。
* 出力: `<out>/<日付>-<エンジン>.json`（課題ごとの呼び出し・時間・トークン・正規化した答え・採点）と
  `<out>/<日付>.md`（課題 × エンジンの表と合計）。

## 課題ファイル（TOML）

`[[task]]` の `kind` ごとの欄（すべて作業根からの相対パス・1 始まりの行）:

| kind | 入力 | 正解（`[task.expected]`） |
|---|---|---|
| `orientation` | — | `mentions`（地図に出てほしいパス）、`pass_recall` |
| `symbol_search` | `query`、`match = "substring"` | `prod` / `tests`（定義の `path:line`） |
| `skeleton` | `path`、`pass_recall` | `symbols`（`Class.method:line` の並び） |
| `definition` | `name`（パスは渡さない） | `location`、`signature_contains` |
| `body` | `[task.target] path, symbol` | `path`、`start`、`end` |
| `references` | `[task.target] path, symbol, name, line` | `prod`、`tests`、`neutral`、`traps` |
| `call_hierarchy` | `direction`、`depth`、`[task.target]` | `keys`（`path::Qualified.name`）、任意で `depth1` |
| `diagnostics` | `path` | `required`、`secondary`（行番号） |
| `rename` | `new_name`、`[task.target]` | `sites`、`neutral` |

## 測るもの

* **時間**: 課題ごとに冷えた 1 回目（その問いを初めて投げたとき）と、同じ呼び出しをもう一度（温まった 2 回目）。
  複数の呼び出しに分かれる課題（ページ送り・連鎖）は合計。
* **応答トークン**: エージェントが読む文字列（MCP の text、grep の出力と読んだ行）を、**全エンジンとも Lucianke の
  `TokenEstimator`** で数える（同じ物差し）。
* **正しさ**: `scoring.py` の冒頭に規則を書いた。要点:
  * 位置の集合（参照・改名・記号検索）は `path:line` の適合率 / 再現率 / F1。`neutral`（import・文字列で引く読み・
    文書中の言及）と対象自身の定義行は両側から外す。本番とテストを別にも採点する。`traps` は同名の別物
    （ローカル変数・別の関数の引数など）で、報告すれば偽陽性。
  * 呼び出し階層は、報告された位置（呼び出し箇所でも定義でも）を作業根のファイルの AST で「いちばん内側の def」に
    写して `path::Qualified.name` にそろえ、その集合で F1。呼び出し先を名前だけで返すエンジンは `::Qualified.name`。
  * 骨格は期待する名前の再現率（多重集合）。定義は位置（±3 行）とシグネチャの断片の完全一致。本体は期待する本体の
    空でない行がどれだけ答えに含まれるか。地図は期待するパスの言及率。診断は `required` をすべて拾えば合格。
  * 合格: 位置の集合と階層は F1 ≥ 0.9、骨格・地図は `pass_recall`、定義は完全一致、本体は 0.95 以上。
* **立ち上がり**: Lucianke は起動 → 索引ジョブの完了 → Python の型サーバーが ready まで。Serena は起動（プロジェクトは
  起動時に有効化）→ 最初の記号の道具が答えるまで（言語サーバーの起動）。grep は 0。

## 公平さのための決めごと

* Lucianke の課題は既定の `compact` 形式で呼ぶ（エージェントが普段読む形）。`# more: cursor=` が出たらページを
  追う（各ページが 1 呼び出し）。`name_matched`（`conf=heuristic` の補足の欄）の位置も「報告した」として数え、
  JSON の `extra.heuristic_sites` に分けて残す。
* Serena は課題ごとにいちばん近い道具に写す（`adapters/serena.py` の冒頭）。同じ働きの道具が無い課題（呼び出し先の
  問い合わせ）は `unsupported` にして、作り話で埋めない。深さ 2 の呼び出し元は `find_referencing_symbols` を
  呼び出し元の関数ごとに連ねる（呼び出し数・時間・トークンは合計）。`rename_symbol` に下見（dry-run）は無いので、
  写しに適用してファイルの差分から編集箇所を取り、バイト単位で元に戻す（温まった 2 回目はしない）。
* grep の基準線は `rg -n -w -t py` を Python で再現したもの（外部のバイナリが要らない）。検索の出力は 250 行で切る
  （エージェントの Grep の既定 `head_limit` と同じ）。読み込みは必要な行だけ（本体、シグネチャ、ヒットから外側の
  def まで最大 40 行）。診断は Grep/Read ではできないので `unsupported`。
* エンジンは 1 つずつ順に走らせる（同時に走らせると時間が互いに乱れる）。

## エンジンを足す

`adapters/base.py` の `Adapter` を継ぎ、`kind_<kind>` のコルーチンで `(calls, Answer)` を返し、`adapters/__init__.py`
の `ADAPTERS` に登録する。`Answer` は正規化した答え（作業根からの POSIX パス・1 始まりの行）だけを持つ。
