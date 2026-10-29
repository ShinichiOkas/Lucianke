# BUG-0030 実行中の lucianke.exe があると uv run の同期が失敗し、テストが走らない（Windows）

- 報告: 2026-10-29 14:55 / 見た人: 親
- 見たこと:
  - 作業ツリーの `pyproject.toml` が変更された状態（`license-files` と `[tool.hatch.build.targets.sdist]` の追加。別の役の作業中）で `uv run pytest -q tests/unit/test_syntax_heuristic.py -k ctags` を実行すると、テストが走る前に次で失敗した:
    `error: failed to remove file S:\work\develop\Lucianke\.venv\Lib\site-packages\../../Scripts/lucianke.exe: プロセスはファイルにアクセスできません。別のプロセスが使用中です。 (os error 32)`
  - そのとき `lucianke.exe` が 2 つ動いていた。PID 38660（14:15:01 起動、親は `uv run --quiet --directory S:\work\develop\Lucianke lucianke`。起こした役は 14:38 に終わっている）と PID 80096（別の役の一時作業ツリーの実行）。
  - `uv run --no-sync pytest ...` なら同じテストが通った（14 passed, 1 skipped）。
- 影響の見込み: テスト・ベンチ・e2e は `uv run ... lucianke`（コンソールスクリプトの `lucianke.exe`）でサーバーを起動するので、誰かがサーバーを動かしている間に `pyproject.toml` が変わると、全員の `uv run` が失敗しうる。`.mcp.json` も `uv run --directory ... lucianke` なので、Claude Code がつないでいる間の開発でも起きうる。
- 再現手順: `lucianke.exe` を 1 つ起動したまま `pyproject.toml` の `[project]` を変えて `uv run pytest -q tests/unit/test_core_model.py`。
- 調査はしていない（直す役の仕事）。
