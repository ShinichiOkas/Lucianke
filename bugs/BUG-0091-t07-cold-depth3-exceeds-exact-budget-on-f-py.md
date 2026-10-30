# BUG-0091 冷えた LSP での F-PY の T-07（incoming、depth 3）は、負荷をかけなくても 1 回目が 1 問い合わせの予算 `EXACT_BUDGET_S`（10 秒）や要求の期限（5 秒）に届いて降格する（5 回中 4 回）。降格の警告は使い切った予算ではなく「5000 ms」と書く

- 報告: 2026-10-31 / 見た人: BUG-0080 を直す役
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `c7f2b7e` + BUG-0080 の直し、basedpyright 1.40.2、Windows 11）

F-PY の rig（`tests/contract_fixtures/_rig.open_rig`。本番と同じ結線。索引の完了と basedpyright の ready を待つ）を開き、
`warm_up(rig, ["httpx/_urls.py"])`（最初の診断まで待つ）の後に、T-07 `query_call_hierarchy`
（`URL.copy_with`、`direction=incoming`、`depth=3`、`max_tokens=6000`）を聞いた。rig は毎回新しい（basedpyright も新しいプロセス）。

### 1 回目の depth 3（冷えた LSP）の所要と水準

自分で負荷はかけていない（同じ PC で他の役の試験が走っていたかもしれない）。

| # | 前に聞いたもの | 所要 | 水準 | 降格の理由（所要からの見立て。警告はどれも「5000 ms」） |
|---|---|---|---|---|
| 1 | なし | 10.08 秒 | `heuristic`（降格） | 予算 10 秒 |
| 2 | なし | 8.5 秒 | `exact` | — |
| 3 | なし | 5.2 秒 | `heuristic`（降格） | 要求の期限 5 秒（`LSP_TIMEOUT_CALL_HIERARCHY_MS`） |
| 4 | なし | 10.12 秒 | `heuristic`（降格） | 予算 10 秒 |
| 5 | なし | 5.19 秒 | `heuristic`（降格） | 要求の期限 5 秒 |
| 6 | T-07 depth 1（3.3 秒・`exact`） | 6.94 秒 | `exact` | — |
| 7 | T-06 references（2.1 秒・`exact`） | 8.11 秒 | `exact` | — |

- 何も聞いていない冷えた LSP への 1 回目は、5 回中 4 回が降格した（予算 10 秒で 2 回、callHierarchy の要求 1 つの期限 5 秒で 2 回）。
  要求の期限で切れた分は、`LspHandle` の連続時間切れ（3 回で `unavailable`）に数えられる。
- `tests/contract_fixtures` の T-07 の試験（`warm_up` の後に depth 3）でも、負荷の高い全体テストで同じ降格が起きている（BUG-0074・BUG-0080）。

### 内訳（exact で答えた 8.5 秒の 1 回。BUG-0058 の遅い呼び出しのログ）

| ノード | 合計 | 内訳 |
|---|---|---|
| `URL.copy_with` | 2844 ms | prepare 63・calls 2000・sites 781 |
| `BaseClient._enforce_trailing_slash` | 1328 ms | settle 1281（そのファイルの最初の診断を待つ） |
| `BaseClient.__init__` | 1297 ms | calls 1297 |
| `Proxy.__init__` | 688 ms | settle 344・calls 297 |
| `test_url_set_param_manipulation` | 828 ms | settle 797 |

- 2 回目以降（同じ rig）は `lsp-cache` で 0.05 秒・`exact`。depth 1 は冷えた状態でも 3.3 秒で `exact`。

### 降格したときの警告

- 予算（10 秒）を使い切って降格した応答の `accuracy_warning` は「LSP の応答が 5000 ms 内に得られなかったため構文木で解決しました。…」。
  5000 ms は callHierarchy の要求 1 つの期限（`LSP_TIMEOUT_CALL_HIERARCHY_MS`）で、使い切ったのは 1 問い合わせの予算 10 秒。予算を
  0.3 秒・0 秒にして降格させたときも、同じ「5000 ms」の文言だった。

## 再現手順

1. F-PY の rig を新しく開き、`warm_up(rig, ["httpx/_urls.py"])`。
2. 上の T-07（depth 3）を 1 回聞き、所要と `_resolution_meta.confidence_level` を見る。

## 困ること

- 呼ぶ側の AI がセッションの最初に聞く「このメソッドを誰が呼ぶか（depth 3）」が、60 ファイルほどのリポジトリでも、負荷を
  かけなくても 5 回中 4 回 `structural`（同名メソッドの区別が落ちる）になる。聞き直せば `exact` になるが、AI には聞き直す
  理由が分からない（警告は「5000 ms 内に得られなかった」と言い、使い切った予算も、聞き直せば exact になることも書かない）。
