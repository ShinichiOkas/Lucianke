# BUG-0028 14:51 の作業ツリーで、F-POLY に対して全ツールの最初の呼び出しが E_INTERNAL（query_project_db も）

見たことだけを書く（調査していない）。2026-10-29 14:51、bench/run_latency.py（18b）で観測。多数の役が並行編集中の作業ツリーの一時的な状態かもしれない。

## 症状
F-POLY を cwd+roots にして起動。`get_file_skeleton`・`search_workspace_symbols`・`get_symbol_bodies`・`find_references_and_implementations`・`query_call_hierarchy`・`get_diagnostics`・`get_project_db_schema`・`query_project_db` がすべて `E_INTERNAL`（`Internal error. Quote details.log_ref when reporting.`）。`resolve_symbols` は `E_TARGET_NOT_FOUND`（`file is not in the index: src/shop/models/receipt.py`）。
同じコマンドは 14:41 には成功していた。ログ行（log_ref）は取っていない。

## 再現
`uv run python bench/run_latency.py --fixtures poly`。結果 JSON の `runs[0].tools.<tool>.probe_errors` と `server_stderr_errors`。

## 原因
結論: 14:51 の作業ツリーが書きかけだった（一時的な状態）。push 済みのコミットでは再現しない。

- 14:51 の結果 JSON（`bench/results/20261029-145120-windows-latency.json`）は `lucianke.commit = 05aa3ac`・`dirty = true`・`dirty_paths = 43`、warm-up の 1 回目の `query_project_db` からすでに `E_INTERNAL`。コミット済みの内容ではなく、未コミットの編集が混ざった状態を測っていた。その時点の `log_ref` と標準エラーは残っておらず（標準エラーの記録は 54979b4 〈14:52〉で足された）、14:51 の例外そのものは復元できない。
- 同じ種類の状態を 15:04 に本体の作業ツリーで再現できた（`S:/work/develop/Lucianke` を cwd にして F-POLY を `bench/_stdio.py` で起動、`query_project_db` で files が 29 件になった後の `resolve_symbols`）。`E_INTERNAL` の元の例外（`<キャッシュ>/logs/lucianke-<日付>.log` の `tool_internal_error` の `exc`）は次のとおり。
  `TypeError: FileInfo.__init__() got an unexpected keyword argument 'pagerank'`（`index/queries.py` の `_file` が `pagerank=` を渡し、`core/model.py` の `FileInfo` にはまだその欄が無かった。別の役の編集の途中で、約 1 分後には `model.py` に欄が足されていた）。
- この形だと、索引が空のうちは行が 1 件も引けず `E_TARGET_NOT_FOUND`（`file is not in the index`）、files が入った後は索引の行を読む全ツールが `E_INTERNAL` になる。14:51 の「resolve_symbols だけ `E_TARGET_NOT_FOUND`、ほかは `E_INTERNAL`」の見え方と合う。ただし 14:51 の実際の例外が同じ `pagerank` だったとは確かめられない（その時刻の作業ツリーは戻せない）。確かなのは、共有の索引・モデルのコードが未コミットの編集の途中だと、全ツールの最初の呼び出しが `E_INTERNAL` になる、という点。
- コード側の欠陥ではない。ただし `E_INTERNAL` の応答に例外の型名も出ない（`log_ref` だけ）ため、標準エラーの 1 行（`internal error: TypeError`）では原因が追えず、JSONL ログを開く必要がある（直さない。設計書 §9.1 のとおり）。

## 直し方
直すコードは無い（一時的な状態）。回帰テストも足さない: 既存の `tests/e2e/test_wired_index_stdio.py`（実プロセスを stdio で起動し、索引と `query_project_db` が通ることを確かめる）が、この種の「結線した索引が読めない」状態を捕まえる。

## 確認
一時の作業ツリー（`git worktree`）で、push 済みのコミットに対して試した。
- `fccee0a`: `bench/run_latency.py --fixtures poly --calls 5` を 2 回。`E_INTERNAL` は 0 件。代わりに BUG-0020・BUG-0026 の無応答が出た（下の「別件」）。`tests/e2e/test_wired_index_stdio.py` は 2 件 green。
- `05aa3ac`（14:51 の結果が指していたコミット、クリーン）: warm-up の最初の呼び出しから `E_INTERNAL` なし。
- `b2d50a3`（最新）: 「サーバーを起動 → files が入るまで待つ → 9 ツール（`bench/run_latency.py` の台本の引数）を 1 回ずつ呼ぶ」を 10 回。90 回の最初の呼び出しすべて成功、サーバーの標準エラーの ERROR 行は 0。

### 別件（BUG-0020・BUG-0026 の原因。見たこと）
`fccee0a` では、起動の約 7 秒後に `resolve_symbols` が戻らなくなる（warm-up の 10 回目前後。サーバーは CPU を使い続け、stdin を閉じても 10 秒で終わらない）。`faulthandler` と `py-spy dump --native` で、イベントループのスレッドが `syntax/imports.py` の `_text`（`node.text`）の中の `tree_sitter` の `PyBytes_FromObject` で空回りしていた。最小の再現: `parse_source(src, "python", "x", deadline_ms=300)` の後に 0.6 秒待って `root_node.children[0].text` を読むと戻らない（`tree-sitter 0.26.0`。木は `read` コールバックを保持し、`Node.text` のたびに呼び直すが、`parsers._parse_with_deadline` の `read` は期限後に `b""` を返す）。この直しは本体の作業ツリーの `src/lucianke/syntax/parsers.py` に未コミットで入っている（`parsing` フラグで期限を解析中だけ効かせる。コメントに BUG-0020）。それを一時の作業ツリーに当てると `bench/run_latency.py --fixtures poly --calls 5` は無応答にならず、`E_INTERNAL` も出なかった（get_diagnostics の p95 2033 ms は別件）。
