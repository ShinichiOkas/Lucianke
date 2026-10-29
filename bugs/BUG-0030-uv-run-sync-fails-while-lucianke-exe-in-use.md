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

## 原因
- `uv run`（`uv sync`）は、`pyproject.toml` が変わっていると editable の `lucianke` を入れ直すため `.venv\Scripts\lucianke.exe`（コンソールスクリプト）を消して作り直す。**動いている exe は Windows では消せない**（os error 32）。
- 票のとき `lucianke.exe` を握っていたのは、テスト・ベンチ・e2e が `uv run ... lucianke` でサーバーを起こしていたため（`tests/e2e/*`、`bench/_stdio.py` の `uv_argv`〈`run_latency` / `run_reaction`〉、`bench/run_tokens.py`、`bench/dogfood/run_dogfood.py`）。誰かのサーバーが動いている間に `pyproject.toml` が変わると、**それとは関係のない人の `uv run pytest` も、テストが走る前の環境同期で落ちる**（再現: 一時作業ツリーで `lucianke.exe` を 1 つ起動したまま classifiers に 1 行足して `uv run pytest -q tests/e2e/test_vertical_slice_stdio.py` → 票と同じ `failed to remove file ...Scripts/lucianke.exe ... (os error 32)`）。
- 外側の `uv run` の同期は、exe が握られている限り直しようが無い（テストの中身では防げない）。直せるのは「誰が exe を握るか」だけ。

## 直し方
- 実プロセスのサーバーは `sys.executable -P -m lucianke` で起こす（`uv` を挟まない。`lucianke.exe` を握らない）。握るのは `python.exe` で、同期は python のバージョンが変わらない限り触らない。子は親（`uv run pytest` / `uv run python bench/...`）と同じ環境で動くので、環境は親が同期した直後のもの（`--no-sync` を付ける・付けない問題が出ない）。`-P` は `python -m` が cwd（ワークスペース。F-POLY など任意の Python ファイルを含む）を `sys.path` に足さないため（コンソールスクリプトは元々足さない）。
- 1 か所にまとめた: テスト側は `tests/_server_cmd.py` の `server_argv()`、bench 側は `bench/_stdio.py` の `server_argv()`（旧 `uv_argv`）。使う側: `tests/e2e/test_vertical_slice_stdio.py`・`test_wired_index_stdio.py`、`bench/run_latency.py`・`run_reaction.py`・`dogfood/run_dogfood.py`（`bench/run_tokens.py` は別の役の作業中の版が同じ形の `server_command()` を持つ）。`tests/invariants/test_stdout_purity.py` と `tests/contract/test_unsupported_tools.py` は元から `sys.executable -m lucianke`。
- 見張り: `tests/unit/test_server_launch_guard.py` — `server_argv` の形、tests/ と bench/ に `uv run ... lucianke` / `command="uv"` / `uv_argv` が戻ってきたら落とす、bench 側と tests 側が同じ argv であること、`.mcp.json` が `lucianke` のエントリーポイント（`[project.scripts] lucianke = "lucianke.__main__:main"`）を指し続けること。
- `.mcp.json` は変えない（ホストが `uv run ... lucianke` で exe を握るのは開発者が Lucianke をつないで使っている間だけ。変えるとホストの再承認が要りうる）。README.md に注意を 1 行足した: 「MCP でつないだまま `pyproject.toml` を変えたら、`uv sync` の前にホストのサーバーを止める」。**（後日: 下の「残りの直し」で `.mcp.json` も変えた。この段落は当時の判断）**
- 残る握り手（意図どおり）: M1-6 の台本（`bench/e2e/run_session.py`、`tests/e2e/test_m1_6_session.py`）は仕様書 §15.2 のとおり **`.mcp.json` の command をそのまま使う**ので、その実行中は `lucianke.exe` を握る。その間は `pyproject.toml` を変えない（または `uv run --no-sync` を使う）。

## 確認
- 一時作業ツリー（`git archive HEAD` を `uv sync`。本体の `pyproject.toml` には触れない）で:
  - 直す前: `lucianke.exe` を起動したまま classifiers に 1 行足して `uv run pytest -q tests/e2e/test_vertical_slice_stdio.py` → `failed to remove file ... Scripts/lucianke.exe ... (os error 32)`（票を再現）。
  - 直した後: テストが起こすのと同じ `python -P -m lucianke` を起動したまま classifiers に 1 行足し、`uv run pytest -q tests/e2e/test_vertical_slice_stdio.py tests/unit/test_server_launch_guard.py` → 同期が `Uninstalled 1 package / Installed 1 package`（lucianke の入れ直し）で通り、5 passed。続けて `tests/e2e tests/invariants` 197 passed。
  - bench 側: `bench/_stdio.py` の `McpStdioClient(server_argv())` で `tools/list` 21 本・正常終了、`bench/dogfood/run_dogfood.py` の `params()` で `get_file_skeleton` が返ることを確認。
- 本体の作業ツリー: `uv run --no-sync pytest -q tests/e2e/test_wired_index_stdio.py tests/invariants tests/unit/test_server_launch_guard.py` green。

## 残りの直し（ホストの `.mcp.json`。2026-10-29）
- 師匠はこのサーバーをまだ `/mcp` で承認していない（再承認の手間が増えない）ので、`.mcp.json` も exe を握らない形にした: `["run", "--directory", "S:/work/develop/Lucianke", "python", "-P", "-m", "lucianke"]`（`uv run` は環境の同期のために残す。握るのは `.venv\Scripts\python.exe` で `lucianke.exe` ではない。`-P` はテスト・ベンチの `server_argv` と同じ理由）。
- M1-6 の台本（`bench/e2e/run_session.py`）は `.mcp.json` の command と args をそのまま使うので、台本の実行中も `lucianke.exe` を握らなくなった（上の「残る握り手」は解消）。
- 見張り: `tests/unit/test_server_launch_guard.py::test_host_config_launches_python_dash_m_not_the_console_script`（`.mcp.json` が `uv run --directory <repo> python -P -m lucianke` で、`python` 以降が `server_argv()` と同じ形。コンソールスクリプトの `lucianke` を直接起こす形に戻ったら落ちる）。README.md の注意書きも新しい形に直した。
- 確認: `tests/e2e/test_m1_6_session.py` 2 passed（台本 9 段が pass、`leftover_processes_after_exit == []`、20.7 s）。`.mcp.json` の command と args でサーバーを起こして子孫の実行ファイルを列挙: `uv.exe` → `.venv\Scripts\python.exe` → 基の `python.exe` で、`lucianke.exe` は無い。`tests/unit/test_server_launch_guard.py` 4 passed。
