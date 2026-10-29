# BUG-0035 set_python_env で Python の LSP が二重に再起動されうる

- 報告: 2026-10-29 15:10 / 見た人: 結線の役（16b）
- 見たこと（コードの読みから。実機での再現はしていない）:
  - T-00 の `_set_python_env`（`src/lucianke/tools/t00_configure_workspace.py`）は、`hub.workspace_changed(ws)` を呼んだあと、背景ジョブで `handle.ws = new` → `pool.reset` → `ensure_started` を行う。
  - `hub.workspace_changed(ws)`（`tools/_runtime.py`、34fcb14）は `LspPool.rebind(ws)` を await する。`LspPool.rebind`（16c、8a6f40f / c84b7a0）は、Python のインタープリタが変わると Python 系のハンドルを自分で止めて起動し直す。
  - そのため 1 回の `set_python_env` で、Python 系の LSP が 2 回再起動される可能性がある。
- 再現手順: 実プロセスで Python のワークスペースを開き、basedpyright を ready にしてから `configure_workspace` の `set_python_env` を呼び、LSP の起動ログ（`started_at` の変化）を数える。
- 調査はしていない（直す役の仕事）。

## 原因

票のとおり。`set_python_env` の流れが、再起動を 2 か所で行っていた。

1. `_set_python_env` → `_rebind` → `hub.workspace_changed(new_ws)` → `LspPool.rebind` → `LspHandle.rebind`。Python のインタープリタが変わり、ハンドルが起動中・起動済みなら、ここで `_stop` → `_begin` する（standby → starting。`started_at` も新しくなる）。
2. そのあと T-00 が背景ジョブ `restart_python_lsp` を投げる。ジョブは `pool.reset(python)`（= `_stop`）→ `pool.ensure_started(python)`（= `_begin`）を無条件に行った。1 で起動を始めたばかりのものを止めて、もう一度起動した。

実測（直す前の木に、実物の `RuntimeHub` + `LspPool` + 偽 LSP サーバーで書いたテストを載せた）: 動いている Python 系（types と lint の 2 役）に `set_python_env`（自動検出と別のインタープリタ）を 1 回呼ぶと、各役の起動の試み（standby → starting）が 初回 1 + 3 = 3 回。直したあとは 1 + 1 = 2 回。

## 直し方

再起動の責任を `LspPool.rebind` に一本化した。

- `RuntimeHub.workspace_changed` が、再起動したハンドルの一覧を返す（`LspPool.rebind` の戻り。プール単位の `rebind` が無いときは、ハンドルごとの `rebind` が `True` を返したもの。失敗・プールなしは空）。
- T-00 `_rebind` がそれを受け取る（`workspace_changed` を持たない・`None` を返すハブは空）。`_set_python_env` のジョブは、rebind が Python 系を再起動していたら `reset` をしない（`ensure_started` だけ。starting 以降は何もしないので、まだ起動していない standby の分だけ起動する）。
- rebind が再起動しなかったとき（rebind が起こさない場合）だけ、ジョブが従来どおり `reset` + `ensure_started` する: (a) 同じインタープリタを指定し直した場合（パッケージを入れ直したあとの取り直し。仕様書 T-00 事後条件 3: `started_at` が呼び出し時刻より後）。(b) 動いていなかった・ハンドルが無かった場合（standby は reset が何もしないので、起動の 1 回だけ）。
- どの場合も、1 回の `set_python_env` で各役の起動は高々 1 回。応答の形・`job_id` の返り方・`lsp_daemons[].started_at` は変えていない。

## 確認

- `tests/unit/test_t00_set_python_env_restart.py`（新規 3 本。実物の `RuntimeHub` + `LspPool` + `test_lsp_pool.FAKE_SERVER`、types と lint の 2 役。起動の試み〔`LspHandle.history` の STARTING の数〕とプロセスの起動回数を数える）: インタープリタ変更で各役 1 回だけ起こし直す／同じインタープリタの指定し直しも 1 回／使っていない Python 系は 1 回だけ起動する。直す前の木では 1 本目と 2 本目が落ちる（3 回起動）ことを確かめた。
- `tests/contract/test_T00_configure.py`（32 件）・`tests/unit/test_tools_runtime.py`・`tests/unit/test_lsp_pool.py` は変更なしで通る。

## コミット
- `hub.workspace_changed` が再起動したハンドルを返す変更: 6e28093（別の役のコミットが同じファイルの変更として一緒に入れた）。
- T-00 のジョブ・テスト 3 本・この票: c613fb4（このコミットには、別の役が index に載せていた docs〔仕様書・設計書・docs/spikes/SYNC-5.md〕の変更も入ってしまった。共有の index を `git commit` にパス指定なしで使ったため。内容はその役の変更のまま、履歴は書き換えていない）。
- 回帰テスト 1 本の追加（BUG-0043）: ecc9632。
