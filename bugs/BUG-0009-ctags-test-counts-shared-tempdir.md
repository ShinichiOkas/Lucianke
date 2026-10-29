# BUG-0009 test_outline_uses_ctags_through_a_child_process が並列実行で 2〜3 割落ちる

- 報告: 2026-10-29 14:21 / 見た人: BUG-0001 の担当（並列での全体実行中）
- 見たこと:
  - `tests/unit/test_syntax_heuristic.py::test_outline_uses_ctags_through_a_child_process` が、並列実行（同時に複数の pytest）で 2〜3 割の確率で落ちる。
  - 担当の観察: テストは `tempfile.gettempdir()` 直下の `lucianke-ctags-*` を数えている。同時に走る別のプロセスの一時物を拾っている疑い。
- 再現手順: `uv run pytest -q tests/unit/test_syntax_heuristic.py` を 6 本同時に走らせる。
- 調査はしていない（直す役の仕事）。

## 原因
- 票の疑いどおり。`staged_dirs()` が OS の共有の一時ディレクトリ（`tempfile.gettempdir()`）直下の `lucianke-ctags-*` を数え、テストの前後の差で「一時ディレクトリが残った」を判定していた。実装（`_ctags_outline`）は `tempfile.mkdtemp(prefix="lucianke-ctags-")` で共有の一時ディレクトリに作る。
- 同時に走る別の pytest プロセスが、このテストの前後の間に自分の `lucianke-ctags-*` を作ると（まだ消えていない最中）、`staged_dirs() <= before` が偽になる。実測した失敗は `Extra items in the left set: 'lucianke-ctags-…'`（他プロセスの名前）で、残留物ではなく他プロセスの途中の一時物だった。
- 再現: `uv run pytest -q tests/unit/test_syntax_heuristic.py` を 6 本同時に 3 回（計 18 本）→ 18 本中 11 本が落ちた（落ちたのはすべてこのテスト 1 件）。

## 直し方
- 実装: `heuristic.outline()` / `_ctags_outline()` に `staging_root: Path | None = None` を足した。ctags に渡す一時ディレクトリは `tempfile.mkdtemp(prefix="lucianke-ctags-", dir=staging_root)`。既定（None）は従来どおり OS の一時ディレクトリで、`engine/indexer.py` の呼び出しは変えていない。
- テスト: 共有の一時ディレクトリを数えるのをやめた。`tmp_path / "stage"`（テストごとに一意）を `staging_root` に渡し、終わりに `list(stage.iterdir()) == []`（片付いた）を確かめる。さらに偽の ctags が「自分の作業ディレクトリの親が `--stage-root=` で渡された置き場と一致する」ことを確かめる（置き場を実装が使っていなければテストが落ちる。`dir=` を外す変異で落ちることを確認した）。他のプロセスの一時物は `tmp_path` に入らないので影響しない。未使用になった `tempfile` の import も除いた。

## 確認
- `tests/unit/test_syntax_heuristic.py` を 6 本同時に 10 回続けて実行（計 60 本）→ 60 本すべて green（直す前は 18 本中 11 本が落ちた）。
- `uv run pytest -q tests/unit/test_syntax_heuristic.py` 52 passed / 1 skipped、`uv run ruff check src tests` と `ruff format --check`（対象 2 ファイル）green。
