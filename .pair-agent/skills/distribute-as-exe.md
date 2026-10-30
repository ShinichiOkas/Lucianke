---
skill_id: distribute-as-exe
category: domain
maturity: confirmed
domain_tags: [distribution, packaging, pyinstaller, windows, exe]
created_at: 2026-10-31
proposed_by: ai
approved_by: human
approved_at: 2026-10-31
source_sprint: m1-impl-20261029
---

# Lucianke の配布は exe 形式（PyInstaller・onedir）

## 師匠の言葉（原文 2026-10-31）

> これ、pyinstallerでexe化できる？
> はい。配布はexe型式にします。関連スクリプトを作成して下さい

## 内容

- 配布物は **PyInstaller の onedir**（`lucianke.exe` + 隣の `lucianke_internal\`）。onefile にしない:
  起動のたびに一時フォルダへ展開して 3.4〜4.2 秒かかり、MCP のホストはセッションごとに起動し直すので毎回待たされる
  （onedir は 0.7〜0.8 秒。2026-10-31 実測）。
- 入口は `build.ps1`（ビルド → `tools\smoke_exe.py` で確かめ → インストール。`-Zip` で配布用 zip と sha256）。
  定義は `packaging\lucianke.spec`、リリースは `.github/workflows/release.yml`（`v*` のタグで Release に zip を上げる）。
- インストール先は全プロジェクト共通の決定順（`-InstallDir` → `AGENT_INSTALL_DIR` → `~/.local/bin`）。
- LSP サーバーは exe に入れない（外部から探す。無ければ structural）。
- exe（`sys.frozen`）では `sys.executable` が Python ではない。**自分自身を `python -c` / `-m` で起動する作りは exe で壊れる**
  （`doctor` の自己診断が実例。隠しサブコマンド `_doctor-selftest` で exe 自身を起動するよう直した）。
- 動的 import（`importlib.import_module`・`pkgutil`）するモジュールとデータ（`.sql`・`.scm`）・metadata は spec で明示して同梱する。
  文法の一覧は `syntax/support.py` から spec が拾う（言語を足しても同梱が漏れない）。

## 適用

- 新しい依存・動的 import・データファイル・自己起動を足したら、`.\build.ps1 -NoInstall` を流して smoke が通ることを確かめる。
- 同梱した依存と Python 実行系のライセンス（`licenses\`・`THIRD_PARTY_NOTICES.md`・`licenses\python\LICENSE.txt`）を配布物に残す。
