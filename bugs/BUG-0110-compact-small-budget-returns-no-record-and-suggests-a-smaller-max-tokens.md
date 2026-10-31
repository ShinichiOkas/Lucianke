# BUG-0110 compact で全体より少し小さい予算だと 1 件も返らず、「今より小さい max_tokens に上げよ」と言う

- 報告: 2026-10-31 / 見た人: BUG-0107 の直す役（補足の欄のページネーションの試験を書いていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

`tests/contract/test_T06_T07_exact_name_supplement.py` の `FILES`（`pkg/a.py#Packer.pack` と、その呼び出し 4 箇所）を
ワークスペース `nm` にし、LSP を起動しない（`structural` の答え。BUG-0107 の変更の影響を受けない経路）で、
`find_references_and_implementations {"target": {"urn": "lk://nm/pkg/a.py#Packer.pack"}, "query_type": "references", "format": "compact", "max_tokens": N}`
を N = 150〜258 で 6 刻みに呼んだ（LSP ありの `exact` の答えでも同じ形）:

```
N=150..246  ヘッダ ~167/Ntok  レコード 0 件
            ! no record fits in max_tokens=N: the next record needs ~22 tokens; raise max_tokens to ~154 or more and call again with the same arguments
N=252       ヘッダ ~251/252tok レコード全部
```

- 全体（前置き + 全レコード）は 251 トークン。1 件目は ~22 トークン。N = 246 でも 1 件も入らない。
- 警告の「raise max_tokens to ~154」は、呼んだ `max_tokens`（例 212・246）より小さい。言われたとおりにしても同じ応答になる。
- 同じ問い合わせの `format: "json"` は、全体より 40 トークン小さい予算で 2 ページに分かれて全部返った（`next_cursor` あり）。

## 再現

1. 上のファイルを任意のディレクトリに置き、`Indexer` で取り込む（`test_T06_T07_exact_marks.Fake(...).start(None)` で作れる）。
2. 上の引数で `max_tokens` を 200 にして T-06 を compact で呼ぶ。

## 利用者（呼ぶ側の AI）として困ること

- 予算を絞って少しずつ読もうとすると、何も返らない。警告どおりに `max_tokens` を変えても何も変わらず、ページを進める手段が無い。
- BUG-0107 の補足の欄の compact のページネーションの試験は、これのために json だけで確かめている。
