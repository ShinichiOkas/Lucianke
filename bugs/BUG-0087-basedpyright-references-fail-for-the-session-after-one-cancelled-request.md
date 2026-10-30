# BUG-0087 basedpyright の references の要求が 1 度取り消される（製品の期限 3 秒で切れる）と、同じ LSP のプロセスでその後の T-06 がすべて `structural` に降格する

- 報告: 2026-10-31 / 見た人: BUG-0080 を直す役
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `c7f2b7e` + BUG-0080 の直し、basedpyright 1.40.2、Windows 11）

F-PY の rig（`tests/contract_fixtures/_rig.open_rig`。本番と同じ結線）で、`warm_up(httpx/_urls.py)` の後に T-06
`find_references_and_implementations`（`URL.copy_with`、`query_type=references`、`include_tests=true`）を聞いた。

1. 普通に聞く → `exact`（29 件、1.7 秒、`engine_used=basedpyright+tree-sitter`）。続けてもう 1 回 → `lsp-cache` で `exact`。
2. 新しい rig で、`lucianke.lsp.features.LSP_TIMEOUT_REFERENCES_MS` を 1 にして 1 回聞く（負荷で 3 秒を超えたときと同じ: 要求を送り、
   時間切れで `$/cancelRequest` を送る）→ `structural`、警告「LSP の応答が 3000 ms 内に得られなかったため構文木で解決しました。…」。
3. 期限を 3000 に戻して聞く → 0.03 秒で `structural`、`lsp_status=ready`、警告「LSP が利用できないため構文木のみで解決しました。…
   `configure_workspace status` で LSP の状態を確認してください。…」。ログ（`lucianke.tools.t06`）:
   `LSP references failed; answering from the index: LuciankeError('LSP basedpyright failed textDocument/references: Request textDocument/references failed with message: this._token.cancel is not a function')`
4. 20 秒待ってから聞く → 同じエラーで `structural`。3 つの rig で、取り消しの後の references は 4 回とも同じエラーだった。
   同じプロセスの callHierarchy（T-07 depth 3）は、その後も `exact`（4.4 秒）で答えた。
- 比べ: T-07 の callHierarchy の要求を途中で取り消した後（`EXACT_BUDGET_S` を 0.3 秒にして 1 回聞く → `$/cancelRequest`）は、予算を
  戻した次の問い合わせが `exact`（1.0 秒）。T-04 の hover / definition を 1 ms で切った後も、次の問い合わせは `exact` になった。
- このエラーの応答は `lsp_status=ready` のまま（降格の理由は `lsp_error` に見える）。連続時間切れの数（3 回で `unavailable`）は
  時間切れのときしか増えないので、プロセスは止まらず、そのまま使われ続ける。

## 再現手順

1. F-PY の rig を開き、`warm_up(rig, ["httpx/_urls.py"])`。
2. `lucianke.lsp.features.LSP_TIMEOUT_REFERENCES_MS = 1` にして、上の T-06 を 1 回聞く。
3. `LSP_TIMEOUT_REFERENCES_MS = 3000` に戻して、同じ T-06 を聞く → `structural`（上のエラー）。何度聞いても同じ。

## 困ること

- 負荷の高いときに T-06 の LSP の要求が 1 度でも 3 秒を超えると、その LSP のプロセスが続く間（セッションの間ずっと）、T-06 は
  `exact` で答えられない。呼ぶ側の AI は参照の型の区別を失い、警告は「LSP が利用できない」と言うのに、`configure_workspace status` は `ready`。
- 契約テスト（BUG-0080 の直し）は、このため T-06 だけ最初から延ばした期限で聞いている（`_rig.ask_exact(..., product_first=False)`）。
