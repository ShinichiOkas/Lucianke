# ドッグフーディング報告 2 周目（2026-10-29 15:00〜15:26、問題出し担当 dogfood-2）

実クライアント（stdio、mcp 2.x）で M1 のツールを F-POLY のコピーとこのリポジトリ自身に当てた結果。台本は `run_dogfood2.py`（f82e41f・4600347）。生の出力は `out/obs2_*.json`（コミットしない）。

- 最初は b24f766 の使い捨て作業ツリーで走らせ、途中で 42640ac（15:20 の origin/main）に作り直して ro・edit・cfg・self を再実行した。出した票は HEAD でも再現するかを確かめて、各票に追記してある。
- 編集系は F-POLY のコピーにだけ当てた。
- **この環境では Python の型 LSP（pyright）が `init_failed` のまま 3 回再起動して `unavailable` になる（BUG-0037、HEAD でも再現）**。LSP が `ready` のときの `exact` 経路は確かめられていない。lint の ruff は `ready` になる。
- 報告書のファイルは担当が書けなかった（サブエージェントのファイル書き込みが止められた）ため、担当の報告の本文から親が書き起こした。

| ツール | 結果 | 実測 |
|---|---|---|
| T-00 configure_workspace | 一部不合格 | `workspace_id` は文字集合に一致。存在しないパスを含む `set_roots` は `E_INVALID_ARGUMENT` で登録不変。`reindex` は 1 ms で `job_id` を返し、直後に `running`。未知の `job_id`・`workspace_id` は `E_WORKSPACE_NOT_CONFIGURED`。`set_excludes` は置換（files 30→26、DB に除外対象 0 件）。`set_python_env` の `interpreter` は指定値と一致し、HEAD では `started_at` も入る。不合格: roots を宣言しないクライアントで最初の呼び出しが 8〜12 秒ブロックされる（BUG-0042） |
| T-01 get_repo_map | 未検査 | b24f766 では見えなかったが、T-01 の追加より前のコミットだったための誤報（BUG-0044）。HEAD での検査は次の周回 |
| T-03 search_workspace_symbols | 合格 | `exact` は全件 `name` = `query` で、`ast` の定義ファイルの集合と一致。`limit`・`symbol_kinds` を守る。`max_tokens: 300` の cursor 13 ページで重複 0・欠落 0（一括の 43 件と一致）。全ページ `~used ≤ max`。不正引数 5 種は `E_INVALID_ARGUMENT`。文法違反 0 |
| T-04 resolve_symbols | 合格（LSP 無し） | F-POLY の 16 シンボルで `definition.range` が `ast` と T-05 の `range_returned` に全件一致。`type_definitions` は全件解決。曖昧は `E_TARGET_AMBIGUOUS` と候補 3 件、他の項目は `ok: true`。不在は `E_TARGET_NOT_FOUND`。hover との一致は未検査 |
| T-06 find_references_and_implementations | 一部不合格 | F-POLY の 6 シンボルは `ast` の識別子の参照との差が 0。`include_tests: false` でテスト行 0。cursor の重複 0。外部 `lk://_ext/decimal#Decimal` は 21 件。このリポジトリ自身の `LuciankeError` は 473 件中 1 件の漏れ（関数内 import で束縛した名前の使用。BUG-0048） |
| T-07 query_call_hierarchy | 合格（LSP 無し） | 5 シンボル × 3 方向 × depth 1・3 で `depth` 超過 0。`cycle` の葉に children なし。外部ノードは葉。呼び出し箇所の行に相手の名前がある。`when:` は期待どおり |
| T-08 get_diagnostics | 不合格 | ruff が `ready` でも 0 件（BUG-0039）。型エラーは pyright が動かず未検査。BOM + CRLF の行 0 の列（観点 7）は未検査 |
| T-10 edit_symbol_body | 合格（HEAD） | 恒等の `replace_body`（LF 3 件・BOM + CRLF 2 件）はバイト一致。`dry_run` は不変。BOM と CRLF を保つ。`insert_before`・`insert_after`・`insert_member`・`delete_symbol` は HEAD で動き、`new_range` が `ast` の範囲と一致。エラー系 8 種は仕様どおりでファイル不変 |
| T-11 rename_symbol | 検査不能 | `E_LSP_UNAVAILABLE`（BUG-0037）。`new_name` の不正値が `E_INVALID_ARGUMENT` になることだけ確かめた |
| T-13 apply_code_action | 一部不合格 | ID 指定の適用は `sha256_after` が実ファイルと一致し、BOM を保ち、CRLF の混入なし。`dry_run` とエラー系でファイル不変。不合格: `quickfix` 指定の `E_TARGET_AMBIGUOUS` で同じ id・title の候補が重複し、件数も回ごとに変わる（BUG-0041） |
| T-19 get_project_db_schema | 合格 | ビュー 10 個、全 `row_count` = `COUNT(*)`、`columns` が実ビューと一致、`coverage` の和 = 1.0、`sample_queries` は全件 T-20 で `ok: true` |
| T-20 query_project_db | 合格 | 拒否すべき 16 文と `sqlite_master` などは全て `E_QUERY_REJECTED`。`limit` と `truncated` が一致。`params` の過不足は `E_INVALID_ARGUMENT`。無限再帰の CTE は `E_TIMEOUT`（207 ms）。ワークスペースのファイルは不変 |
| M2/M3 の 7 本 | 合格 | 全て `E_UNSUPPORTED` と `details.reason: not_in_milestone`・`details.milestone`。compact の文法違反 0 |
| 連続呼び出し（観点 6） | 合格 | 8 ツール混在の 400 回を 4 秒で完走。BUG-0020・0026 は再現せず |

## 出した票

| 票 | 内容 | HEAD |
|---|---|---|
| BUG-0037 | pyright が `init_failed` のまま `unavailable` | 再現 |
| BUG-0039 | ruff が `ready` でも `get_diagnostics` が 0 件 | 再現 |
| BUG-0041 | T-13 の `E_TARGET_AMBIGUOUS` で候補が重複し、件数が不安定 | 再現 |
| BUG-0042 | roots を宣言しないクライアントで最初の呼び出しが 8〜12 秒ブロック | 再現 |
| BUG-0043 | `started_at` が常に null | 再現せず（古いコミット。閉じた） |
| BUG-0044 | T-01 が `tools/list` に無い | 再現せず（誤報。閉じた） |
| BUG-0048 | T-06 が関数内 import で束縛した名前の使用を返さない | HEAD で確認 |

## 次の周回で検査するもの

- LSP が `ready` の `exact` 経路すべて（BUG-0037 が直ってから）: T-04 の hover との一致、T-06・T-07 の LSP との一致、T-08 の型エラーと `snippet`、T-11 の `edits_count`・`urn_map`、T-13 の `target_diagnostic_resolved`。
- T-08 で BOM + CRLF の行 0 の列（BUG-0039 が直ってから）。
- HEAD での T-01。
- 15:21 に 4 本を並列で走らせた self の実行で、T-03 の応答に `data` が無い一過性の失敗が 1 回あった。単独の再実行では再現せず、原因は未確認。
