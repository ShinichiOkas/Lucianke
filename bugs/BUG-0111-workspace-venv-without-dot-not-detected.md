# BUG-0111 ワークスペースの `venv/`（ドット無し）を Python 環境として見つけず、PATH の別の Python（Lucianke 自身の venv）を使う

- 報告: 2026-10-31 / 見た人: 親（能力測定の基準〈Mnemo の分析〉で Lucianke を Mnemo に向けたとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

- 対象のワークスペースの根に `venv/Scripts/python.exe` と `venv/pyvenv.cfg` がある（`.venv/` は無い）。
- `configure_workspace set_roots` の応答の表で、python の欄が `C:/Users/0000950207/work/Lucianke/.venv/Scripts/python.EXE (manager: path)`（Lucianke 自身の venv。ワークスペースの `venv/` ではない）。
- `src/lucianke/workspace/pyenv.py` の探索は `<root>/.venv` だけ（docstring の 2.、125 行 `venv = os.path.join(root, ".venv")`）。

## 利用者（呼ぶ側の AI）として困ること

- 型の LSP（basedpyright）がワークスペースの依存（例: FastAPI）を見つけられない環境で解析するので、exact の参照・定義・診断が劣化する（未解決 import の診断や、型の分からない受け手が増える）。
- `python -m venv venv` は Python の公式チュートリアルの既定の名前で、よく使われる。

## 原因・直し方・確認（2026-10-31）

- 原因: `workspace/pyenv._detect_interpreter` がワークスペースの仮想環境として `<root>/.venv` の名前しか試していなかった。`venv/` は見つからずに PATH の `python`（Lucianke を起動した venv）へ落ち、型の LSP（`python.pythonPath`）にそれが渡っていた。LSP の起動コマンドの探索（`lsp/servers.find_command`）も `<root>/.venv/Scripts|bin` だけを見ていた。
- 直し方（検出順。明示指定〈`set_python_env` / 設定〉の後）:
  1. `<root>/.venv` → `<root>/venv` → `<root>/env` → `<root>/.env`（`WORKSPACE_VENV_NAMES`・`workspace_venv_dirs`）。`.venv` 以外は `pyvenv.cfg` のあるディレクトリだけ（`env` は同名のソース、`.env` は普通 dotenv のファイルなので見分ける）。`.venv` は今までどおり `pyvenv.cfg` が無くても採る（既存の契約を崩さない）。インタプリタは Windows `Scripts/python.exe`、POSIX `bin/python`。
  2. 環境変数 `VIRTUAL_ENV` が **ワークスペースの根の中** の仮想環境（`pyvenv.cfg` とインタプリタあり）を指すとき。根の外（`uv run` で起動したときの Lucianke 自身の venv など）は見ない。
  3. `poetry.lock` だけ（`interpreter=None, manager="poetry"`）→ `CONDA_PREFIX` → PATH（今までどおり）。
  - 依頼の順（poetry/uv の規則 → `VIRTUAL_ENV`）から一つだけ変えた: `VIRTUAL_ENV` を「poetry.lock があるが仮想環境が無い」より先にした。その規則はインタプリタを返さずに探索を打ち切るので、後ろに置くと poetry のプロジェクトでは見つかったインタプリタを捨てて LSP に何も渡さなくなるため。
  - `manager` は仮想環境の隣（親ディレクトリ、無ければ根）の `uv.lock` → `uv`、`poetry.lock` → `poetry`、無ければ `venv`。
  - `find_command` も同じ候補（`workspace_venv_dirs`）の `Scripts|bin` を見る（ワークスペースが固定した basedpyright・ruff）。インタプリタは今までどおり `PythonEnv.interpreter` → `python.pythonPath`、仮想環境の根は `venv_root_of`（`pyvenv.cfg`）で `find_command` の `venv` に渡る。
  - 仕様書 T-00 処理規約 3・LSP の発見の行、設計書 §7.3.4・§7.5.1 の探索順・Windows の表を理由つきで直した。
- 確認:
  - 単体 14 件（`tests/unit/test_workspace_context.py`: `.venv` が `venv` に勝つ・`venv` を見つける・`venv`→`env`→`.env` の順・`pyvenv.cfg` の無い `venv`/`env`/`.env` は無視・dotenv のファイルは無視・lock による manager・Windows / POSIX の配置・`VIRTUAL_ENV` 根の中は採る／根の外と名前が前方一致するだけの隣は捨てる／`pyvenv.cfg` が要る・ワークスペースの仮想環境に負ける・poetry と conda より先・明示指定が勝つ。`tests/unit/test_lsp_client.py`: `find_command` が `pyvenv.cfg` のある `venv/` の ruff を PATH より先に、`.venv` があればそちらを拾う）。
  - 実機: 一時ワークスペースに今のインタープリタで `python -m venv venv` を作り、`configure_workspace set_roots` → `status` の表が `venv/Scripts/python.exe 3.12.10 (manager: venv)`（json の `python_env.interpreter` も同じファイル）。この処理の `VIRTUAL_ENV`（Lucianke の作業ツリーの `.venv`、根の外）は使われなかった。
  - `uv run ruff check` clean。`test_workspace_context`・`test_lsp_client`・`test_extra_paths`・`test_cli_doctor`・`test_lsp_pool`・`test_T00_configure` で 332 passed / 2 skipped。
