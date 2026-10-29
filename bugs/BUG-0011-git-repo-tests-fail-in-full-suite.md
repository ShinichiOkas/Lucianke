# BUG-0011 全体実行のとき tests/unit/test_git_repo.py が実行ごとに別のテストで落ちる

- 報告: 2026-10-29 14:31 / 見た人: BUG-0005 の担当（`uv run pytest -q tests/unit` の全体実行中）
- 見たこと:
  - 最初の全体実行（2060 passed / 27 failed）で、`tests/unit/test_git_repo.py` のテストが落ちた。落ちるテストは実行ごとに違った。
  - 同じファイルを単独で実行すると 132 passed。
  - 全体実行は、同じ作業ツリーで 20 本ほどの役が並行して作業・テストしている最中だった。
- 再現手順: `uv run pytest -q tests/unit` を、別のプロセスで同時に pytest を走らせながら数回。
- 調査はしていない（直す役の仕事）。

## 原因
- 現在のツリーでは再現しない。`tests/unit/test_git_repo.py` 自体と、それが辿る import 先（`git/repo.py`・`git/vtree.py`・`workspace/paths.py`・`workspace/types.py`・`core/errors.py`・`core/model.py`）に、負荷や並行実行で結果が変わる作りは見つからなかった。
  - テストは `tmp_path` / `tmp_path_factory` 以外の共有パスを使わない。`_isolated_env`（`HOME`・`USERPROFILE`・`XDG_CONFIG_HOME`・`GIT_CONFIG_NOSYSTEM`）で利用者の gitconfig・グローバル ignore（実機の `~/.gitconfig` は他の役の `git config --global` で書き換わり続けている）を読まない。dulwich は設定を `Repo` ごとに読み直し、プロセス内キャッシュを持たない。コミット時刻・タグ時刻は固定で SHA は決定的。時刻や mtime に依存する assert は無い（`os.utime` は status が index を書かないことの確認のみ）。
  - pytest の tmp ディレクトリは、他セッションの終了時掃除では消えない（ロックが生きている間は対象外。pytest 9.2.0 の `ensure_deletable` を確認）。
- 報告時（14:31）に別のテストが落ちた時間帯は、他の役が `core/model.py`（14:28 `Kind.File` 追加、BUG-0005）など git_repo の依存先を編集中だった時間帯と重なる。全体実行は 2087 件 → 現在 2248 件とツリーが大きく動いており、報告時の失敗は他の役の書きかけ（と、同時刻の別テストの負荷揺れ。BUG-0006・BUG-0012・BUG-0022）が混ざったものと見るのが妥当。ただし、失敗したテスト名と assert が票に残っていないため、断定はできない。
- 唯一、Windows で本当に揺れうる箇所は `test_repo_handles_are_closed_so_files_can_be_renamed`（書いた直後に `.git/objects` を改名する）。ウイルス対策・インデクサが tmp 配下のハンドルを掴むと winerror=32 になりうる。再現はしていない。

## 直し方
- コードは変えていない。再現できないものを再試行で隠すのは禁止（揺れるテストは根を直す方針）なので、推測の修正は入れなかった。
- 再発したときは、落ちたテスト名・assert・`git status` を票に残すこと（今回はそれが無く、原因を断定できなかった）。再発したら、HEAD に固定した使い捨ての作業ツリー（`git worktree add`）で `pytest tests/unit/test_git_repo.py` を回すと、他の役の書きかけと切り分けられる。

## 確認
- `test_git_repo.py` 単独: 負荷なしで 12 並列 1 回、CPU を 40 本焼きながら 24 並列 × 3 回（計 72 回）、すべて 132 passed。
- 全体 `tests/unit` を 4 本同時に 1 回（各 2233〜2234 passed）: `test_git_repo.py` は 4 本とも green。赤は他の役の領域のみ（`test_index_ingest::test_external_symbols_get_a_minimal_row`、`test_lsp_features::test_real_code_action_for_a_type_error`、`test_workspace_watcher::test_burst_of_600_...`）。
- git_repo より前のファイル（`test_[a-g]*.py`、907 件）を 6 本同時に 1 回: すべて 907 passed（git_repo を含む）。
- 以上、git_repo の落ちは 0 件 / 約 94 回。票は「現在のツリーでは再現せず、原因未断定」で閉じる。再発時は上の手順で開き直す。
