# BUG-0115 カーソル（`next_cursor`）1 つが約 70 トークンあり、件数の少ない答えでは compact で「1 件 + カーソル」のページが全体より大きくなる

- 報告: 2026-10-31 / 見た人: BUG-0110 の直す役（予算の掃引を測っていて見た）
- 見たことだけを書く（調査していない。直し方も考えていない。調査は直す役の仕事）

## 実測

`TokenEstimator`（`o200k_base` に較正済み。base64 の塊は 1 文字 0.63 トークン）で数えた値:

| 何 | 文字数 | トークン |
|---|---|---|
| `find_references_and_implementations` のカーソル（`CursorCodec.encode`、offset=1・data_version=12・ws=nm） | 112 | 71 |
| その `# more: cursor=… remaining≈4` 行（要約なし） | — | 82 |
| 同じ行に既定の要約を足した 2 行 | — | 112 |
| `probe_tool` のカーソル（試験用の短いツール名） | 79 | 50 |
| T-06 の compact の参照 1 行（`pkg/b.py:7:21  [call] in caller  return Packer().pack([1])`） | — | 約 22 |

- カーソルを出すページは、参照 1 行の約 3.7 行ぶんをカーソルに使う。
- `tests/contract/test_T06_T07_exact_name_supplement.py` の `FILES`（LSP なし・structural、4 箇所）を compact で呼ぶと、
  全体（4 件・トレーラなし）は 353 トークン、「先頭 1 件 + `# more:` 行」は約 360 トークンで、全体のほうが小さい。
  このため全体より小さいどの `max_tokens` でも 1 件も返らない（0 件と「raise max_tokens to ~353」の警告。BUG-0110 の直し後の値）。
- カーソルの中身は `base64url(json({"t": ツール名, "a": 引数の SHA-1 先頭 12 桁, "o": オフセット, "v": data_version, "w": workspace_id}))`
  （`mcp/budget.py` の `CursorCodec`）。ツール名が長いほどカーソルが長い。

## 再現

```python
from lucianke.core.tokens import TokenEstimator
from lucianke.mcp.budget import CursorCodec
c = CursorCodec().encode(tool="find_references_and_implementations",
                         args={"target": {"urn": "lk://nm/pkg/a.py#Packer.pack"}, "query_type": "references"},
                         offset=1, data_version=12, workspace_id="nm")
print(len(c), TokenEstimator().count(c))  # 112 71
```

## 利用者（呼ぶ側の AI）として困ること

- 予算を絞って少しずつ読むとき、ページごとに参照 3〜4 行ぶんがカーソルに消える。
- 件数の少ない答えは、全体より小さい予算では分けて読めない（0 件と警告だけが返る）。

## 原因・直し方・確認（2026-10-31）

直す役（BUG-0115 & BUG-0116 をまとめて）が調べて直した。

### 原因

`src/lucianke/mcp/budget.py` の `CursorCodec` が、カーソルの中身（ツール名・引数のハッシュ 12 桁・オフセット・`data_version`・
`workspace_id`）を `base64url(json(...))` にしてそのまま応答に出していた（設計書 ADR-8 の初版の決定）。ツール名と JSON の
鍵・記号まで base64 にするので、`find_references_and_implementations` で 112 文字・71 トークン（base64 の塊は 1 文字 0.63）、
`# more:` 行は約 82 トークンになった。パッカーはこの長さを正しく数えていた（見積もりの誤りではない）ので、件数の少ない答えでは
「1 件 + `# more:` 行」が「全部（トレーラなし）」より大きくなり、全体より小さいどの予算でも 0 件と警告だけになった（BUG-0110 の
直し後は目安が「全体」を正しく指していたが、分けて読む手段が無かった）。

### 直し方（案 (a): サーバー側の表）

- `CursorCodec` は中身 `CursorState` を自分の表（メモリの `OrderedDict`。`CURSOR_TABLE_MAX` = 4,096 件。発行と照合で新しい側へ
  移し、超えたら最も長く使われていないものから捨てる）に持ち、応答に出すのは短い鍵 `c` + 10 進 9 桁（例 `c482139071`）だけにした。
  - 鍵は中身の SHA-256 から決める（同じ中身なら同じ鍵、プロセスをまたいでも同じ。応答が決定的なまま）。表の別の中身とぶつかった
    ときだけ塩を足して引き直す（最大 32 回。空間 10 億に表 4,096 件なので事実上起きない）。
  - 先頭の `c` は、JSON の引数で数値と取り違えられない（`"cursor": 482139071` の型違反で往復しない）ため。10 進の 9 桁は
    o200k で必ず 3 トークン（3 桁ずつ）で、計数器（数字 1 文字 0.9）は 9 と多めに数える（安全側）。小文字の英字 6〜8 文字は
    o200k で 3〜4 トークンだが計数器が 1 と少なく数えるので採らなかった。
  - `peek` は表に入れずに鍵を返し、`encode` は表に入れる（発行）。パイプラインは `cursor_factory` に `peek` を渡し、描き終えて
    応答に出した `next_cursor` だけを `encode` する（パッカーは続きの形ごと・再開位置の上限などで 1 回に数個のカーソルを作るので、
    全部入れると表の上限を見積もりで使う）。
  - 照合（`decode`）の意味は保った: 鍵の形でない文字列 → `E_INVALID_ARGUMENT`（`details.param: "cursor"`。案内「next_cursor を
    そのまま渡すか cursor なしで呼び直す」）、表に無い鍵（再起動・捨てた・発行していない）→ `E_CURSOR_EXPIRED`
    （`details.reason: "unknown_cursor"`、「cursor なしで先頭から呼び直す」）、ツール・引数（`cursor`・`max_tokens`・`format` を
    除く）・ワークスペースの不一致 → `E_INVALID_ARGUMENT`（失効より先に見る）、`data_version` の不一致 → `E_CURSOR_EXPIRED`
    （`details.reason: "data_changed"`）。カーソルは今までどおり `max_tokens`・`format` を変えた呼び直しに使え、オフセットが進む。
  - 表はセッション限定の軽い対応表（設計書 ADR-16 決定 4 に元から書いてあった形）で、DB には書かない（schema は変えない）。
    再起動の後は失効して先頭から取り直せる（受入の「再起動耐性」と同じ）。
- パッカーはカーソルの実際の文字列を `cursor_factory` から受けて数えるので、見積もりは自動で新しい長さになる（`# more:` 行は
  約 82 → 17。o200k では約 11）。
- 仕様書 §5.6-3（カーソルの形・表・失効）・§5.8 の `E_CURSOR_EXPIRED`（`details.reason`）・各例の `cursor=`、設計書 ADR-8・
  §7.2.2 の擬似コード・§7.2.3・§8.6 に理由つきで書いた。
- 試験の側: 1 呼び出しごとに新しい `Pipeline` を作ってカーソルを辿っていた `tests/unit/test_mcp_response_budget.py` の
  `_pipeline` を 1 つにした（サーバーと同じ。毎回作ると再起動と同じで失効する）。`FakeCodec` に `peek` を足した。

### 確認

前後（`TokenEstimator`。o200k は tiktoken で測った値）:

| 何 | 直す前 | 直した後 |
|---|---|---|
| `find_references_and_implementations` のカーソル | 112 文字・71 トークン（o200k 71） | 10 文字・9 トークン（o200k 3） |
| その `# more: cursor=… remaining≈3` 行 | 82 | 17（o200k 約 11） |

BUG-0110 の掃引（1 刻み。「1 件 + カーソル」が返り始める予算）:

| 答え | 全体 | 直す前 | 直した後 |
|---|---|---|---|
| T-06 `FILES`・exact・compact | 252 | 235（全体の 17 手前） | **171**（2 件 + カーソルは 235） |
| T-06 `FILES`・structural・compact（票の形） | 353 | **無し**（352 まで 0 件・「~353」） | **289**（2 件 + カーソルは 336） |
| T-06 `FILES`・exact・json | 677 | 470 | 410 |
| T-06 `FILES`・structural・json | 785 | 581 | 517 |
| 合成 `probe_tool`（5 件 × 29）・compact | 170 | 114 | **69**（2 件 124・3 件 155） |
| 合成 `probe_tool`・json | 870 | 337 | 297 |

試験（足したもの）:

- 単体 `tests/unit/test_mcp_budget.py`: 鍵の形と長さの上限（`c` + 9 桁・計数器で 10 以下・`# more:` 行で 20 以下、長いツール名でも
  同じ）、同じ中身は同じ鍵で `peek` は発行しない、表に無い鍵・捨てた鍵は `E_CURSOR_EXPIRED`（`unknown_cursor`・案内つき）、
  使った鍵は表の新しい側に残る、鍵がぶつかったら引き直す（空間を 2 に潰して）、形の崩れた文字列・以前の base64 の形は
  `E_INVALID_ARGUMENT`、`data_version` の失効は `data_changed`。引数・ツール・ワークスペースの不一致は今までの試験のまま通る。
- 単体（パイプライン）`tests/unit/test_mcp_response_budget.py`: 応答に出したカーソルだけが表に入る（1 呼び出しで 1 件）、
  `max_tokens`・`format` を変えた呼び直しに使え、別の引数は `E_INVALID_ARGUMENT`、新しいパイプライン（再起動）では
  `E_CURSOR_EXPIRED`、以前の形の文字列は `E_INVALID_ARGUMENT`。
- 性質 `tests/unit/test_mcp_budget_fit.py`: 件数の少ない答え（3〜6 件）が compact で全体より参照 1 行ぶん以上小さい予算から
  分けて読め、辿ると全件が 1 度ずつ返る。票の形の試験に「1 件 + カーソル」が全体より 40 以上小さいことを足した。
- 契約 `tests/contract/test_T06_T07_exact_name_supplement.py`: 票の形（structural・compact）の掃引で、全体より 40 以上小さい
  予算から短い鍵つきのページが返る。
- `uv run ruff check` は通る。`tests/unit/test_mcp_*`・`tests/unit/test_compact*`・compact の文法（`tests/contract/test_compact_grammar.py`）・T-01〜T-08 の契約・`tests/contract_fixtures`・e2e の縦切り: 762 件が通る（12 件は環境で飛ばす）。全体の試験は親が main でまとめて回す。
