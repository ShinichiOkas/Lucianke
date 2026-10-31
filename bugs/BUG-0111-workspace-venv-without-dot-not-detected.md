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
