# BUG-0118 ワークスペースの走査が、入れ子の Git の作業ツリー（`.claude/worktrees/*`）にも降りて、同じリポジトリの写しを何十も索引する

- 報告: 2026-10-31 / 見た人: BUG-0112 の直す役（Lucianke 自身の索引の遅さの報告〈外部のクライアント ray が Lucianke のリポジトリを索引して 120 秒で打ち切られた〉を見たとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`C:\Users\0000950207\work\Lucianke` = Lucianke の主の作業ツリー）

- `.claude/worktrees/` の下に作業ツリー 33 個（並行の役のもの。各々が `.git` ファイルを持つ Git の作業ツリー）。
- `.gitignore` に `.claude/` も `.claude/worktrees/` も無い（`.venv/`・`bench/.cache/` などはある）。
- `.py` の数: `.claude/` の外 454 本、`.claude/worktrees/` の中 15,048 本（`.venv`・`__pycache__` を除く。`find` で数えた）。
- `engine/indexer.py` の `_walk` が飛ばす名前は `.git` だけ（`if name == ".git": continue`）。`exclude_globs` の既定は空（`workspace/settings_file.py` の `out.setdefault("exclude_globs", ())`）。

## 利用者（呼ぶ側の AI）として困ること

- 「800 ファイルほどのリポジトリ」のつもりで索引させると、実際には 30 倍以上のファイルを取り込む（初回の索引が長くなり、検索・参照の答えに同じ定義の写しが何十も混ざりうる）。
- 並行の役の作業ツリーを置くのは、このユーザーの普段の使い方（Claude Code の worktree）。

## 原因・直し方・確認（2026-10-31）

- 原因: `engine/indexer._walk` が飛ばす名前は `.git` だけで、`.git` を持つ入れ子のディレクトリ（Git の作業ツリー・別のリポジトリ）にも降りていた。1 ファイルの取り込みの対象外の判定（`_is_skipped`）も同じで、監視が並行の作業ツリーの編集のたびに取り込みうる状態だった。
- 直し方: 列挙は、根以外で `.git`（ファイル・ディレクトリのどちらでも）を持つディレクトリに降りない（`_nested_repo`）。`_is_skipped` も祖先のディレクトリにそれがあれば対象外（監視・先取りも取り込まない）。根の `.gitmodules` の `path` に挙がるサブモジュールは今までどおり辿る（`_submodule_paths`。プロジェクトのソースとして取り込んでいたもの）。Git もこれらを親の作業ツリーの一部として扱わない。仕様書 §12.4 項 1、設計書 §7.6.2 に理由つきで書いた。なお origin/main の `043ccc0` で Lucianke 自身の `.gitignore` に `.claude/worktrees/` が入ったが、この直しは `.gitignore` に依らない（ほかのリポジトリ・`.gitignore` の無い作業ツリーでも同じ）。
- 確認: 単体 `tests/unit/test_engine_indexer_nested_repos.py`（作業ツリー〈`.git` はファイル〉・中に置いたリポジトリ〈`.git` はディレクトリ〉のファイルは走査で `files` に入らず、`index_file` は `None`・`ingest_ahead` は `skipped`、`.gitmodules` のサブモジュールは取り込む）。索引器・監視・T-00 の試験と合わせて 113 件が通る。`uv run ruff check` はきれい。
