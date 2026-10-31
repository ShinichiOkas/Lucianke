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
