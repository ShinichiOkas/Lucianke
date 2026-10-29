# BUG: T-10 挿入テスト（test_T10_insert.py）が共有作業ツリーでの 1 回の実行だけ 9 件落ちた

見たことだけ（原因は調べていない）。

- 症状: コミット 96866a5（T-10 の挿入 3 操作）の push 直後、共有作業ツリーで
  `uv run`（`.venv/Scripts/python.exe -m pytest -p no:cacheprovider -q tests/contract/test_T10_insert.py tests/contract/test_T10_edit_body.py tests/unit/test_import_graph.py`）を 1 回走らせたところ `9 failed, 104 passed`。
  末尾に見えた失敗は次の 3 件（残り 6 件の ID は取っていない。同じ 2 つのテスト群だった可能性が高いが未確認）:
  - `tests/contract/test_T10_insert.py::test_postcondition_3_target_urn_and_new_range_point_at_the_inserted_symbol[target7-insert_before-...PaymentClient.refund-end]`
  - `tests/contract/test_T10_insert.py::test_postcondition_3_target_urn_and_new_range_point_at_the_inserted_symbol[target8-insert_member-...PaymentClient.refund-start]`
  - `tests/contract/test_T10_insert.py::test_compact_output_follows_the_grammar[target2-insert_member-PaymentProcessor.refund]`
- そのときの作業ツリー: 他の役の未コミット変更が多数（`git status` で見えたもの: `src/lucianke/mcp/budget.py`、`mcp/pipeline.py`、`mcp/render/json_out.py`、
  `core/urn.py`、`syntax/symbols.py`、`syntax/parsers.py`、`syntax/skeleton.py`、`index/*`、`lsp/*`、`tools/t01・t06・t07` ほか）。
  同じ変更途中の状態で、これより前に `Packer.pack() got an unexpected keyword argument 'record_text'`（`mcp/fit.py` が `mcp/budget.py` より先に進んでいた）で 47 件落ちた観測もある。
- その後の再実行: 同じコマンドが 68 passed（test_T10_insert.py のみ）→ 113 passed → 113 passed。
- 隔離環境（`git archive HEAD` に自分の変更だけを重ねた木。他の役の未コミット変更なし）: 同じ 4 ファイル（insert・edit_body・delete・import_graph）が 3 回連続 146 passed。
- 再現手順: 現時点では再現していない。落ちた回の出力は取れていない（失敗名の末尾 3 件のみ）。
- 原因は断定しない。他の役の変更途中の状態（上記ファイル群）との相互作用か、テスト自体の揺れかは未調査。直す役が、失敗時の出力を取りながら共有作業ツリーで繰り返し走らせて確かめる。

## 原因
- HEAD（6f0a4a7、push 済み）では再現しない。9 件落ちた回は、共有作業ツリーに他の役の書きかけの未コミット変更（`mcp/budget.py`・`mcp/fit.py`・`core/urn.py`・`syntax/symbols.py` ほか）が載った一時的な状態だった。同じ票の中で、直前の回は `Packer.pack() got an unexpected keyword argument 'record_text'`（`mcp/fit.py` が `mcp/budget.py` より先に進んだ食い違い）で 47 件落ちており、木が整合していない時間帯があったことと符合する。
- 落ちた 9 件が触る道（挿入後の URN・範囲・compact の描画）は、当時書きかけだった `core/urn.py`（`parse` / `format` の `lru_cache`）と `syntax/symbols.py`（`extract_detailed` の内容キーのメモ）を通る。これらは BUG-0027（6f0a4a7）で確定してコミットされた。
- テスト自体の揺れではない根拠: 挿入・削除・本体編集のテストは `tmp_path` に `tests/fixtures/poly` を写して使い、LSP・監視・索引の時刻や並行に頼らない。BUG-0027 で足した内容キーのメモ（`syntax/memo.py`）はキーにワークスペース・パス・言語・クエリ・内容の指紋を全部含み、`Urn` は frozen・`compare=False` の欄が無いので `lru_cache` のキーとして安全であることをコードで確かめた。

## 直し方
- 直すものは無い（HEAD で再現せず、コードの変更なし）。「他の役の書きかけによる一時的な状態」として閉じる。
- 再発の見張り: 共有作業ツリーでテストが妙に落ちたら、まず `git status` で他の役の未コミット変更を見て、`git stash` ではなく使い捨ての作業ツリー（`git worktree add <dir> origin/main`）で同じコマンドを走らせて切り分ける。

## 確認
- push 済みの HEAD（6f0a4a7）の使い捨て作業ツリー（`git worktree add C:\Users\shini\AppData\Local\Temp\lucianke-bug0033 origin/main`、本体の `.venv` を `PYTHONPATH=<worktree>\src` で使用。`lucianke.__file__` が作業ツリーを指すことを確認）で次を走らせ、すべて green。
  - `tests/contract/test_T10_insert.py`・`test_T10_delete.py`・`test_T10_edit_body.py`（145 件）を 4 並行で 20 回: 20 / 20 が 145 passed（1 回 33〜44 秒）。
  - 負荷を上げた回: CPU を回し続けるプロセス 16 本を背負わせ、12 並行で 24 回。偶数回は `test_T10_edit_body` → `test_T10_delete` → `test_T10_insert` → `test_perf_reuse` → `test_perf_memo`（156 件）、奇数回は `test_perf_reuse` → `test_perf_memo` → insert → delete → edit_body → `test_T02_skeleton`（176 件）と順序を入れ替えて、メモ・`lru_cache` の持ち越しによる順序依存も見た: 24 / 24 が passed。
  - 合計 44 回、失敗 0。
  - 参考: 調査の最後に共有作業ツリー（そのときは別の役の未コミット変更あり）でも 1 回走らせ、145 passed。
