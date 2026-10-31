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
