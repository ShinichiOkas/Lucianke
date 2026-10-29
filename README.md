# Project Lucianke
**Lucid + Ananke: 透明な支配の美学**

## コンセプト
Project Luciankeは、コーディングエージェントにおけるトークン消費の浪費と作業時間の損失を根本的に解消するために設計された、高効率LSP-MCPサーバーである。Tree-sitterによるASTベースの骨格抽出とLSPによる精密なシンボル解決を統合することで、カオスなコードベースを厳格な構造へと閉じ込め、決定論的な解析と編集を実現する。

## 核心的価値：「透明な支配（Transparent Domination）」
- **Lucid（透明性）**: 巨大なコードの森に潜むノイズを排し、システムの真の構造を透視する知的な快楽。
- **Ananke（必然性）**: 解析結果を偶然に委ねず、構造的整合性に基づいた「逃れられない正解」を導き出す決定論的な支配力。

## 技術的柱
- **ASTベースの骨格抽出**: ファイル全体の読み込みを避け、構造的なバックボーンのみを抽出することでコンテキスト窓を最適化する。
- **決定論的編集プロトコル**: 編集時の整合性を厳格に担保し、エージェントによる不確定な破壊的変更を排除する。
- **高効率トークン戦略**: 転送データを最小限に抑制し、推論リソースを本質的な問題解決に集中させる。

## 開発メモ
- `.mcp.json` の起動は `uv run --directory <repo> python -P -m lucianke`（コンソールスクリプトの `lucianke.exe` を使わない）。Lucianke を MCP でつないだままでも `.venv\Scripts\lucianke.exe` を握らないので、`pyproject.toml` を変えて `uv run` / `uv sync` が `lucianke.exe` を入れ直しても `os error 32` で失敗しない（Windows では動いている exe を消せない。BUG-0030）。握るのは `python.exe` で、同期は python のバージョンが変わらない限り触らない。テスト・ベンチも同じ形（`python -P -m lucianke`。`tests/_server_cmd.py`、`bench/_stdio.py` の `server_argv`）で起こす。`-P` は cwd（解析対象のワークスペース）を `sys.path` に足さないため。ホストの `uv run ... lucianke` に戻さないこと（`tests/unit/test_server_launch_guard.py` が見張る）。

---
*論理的整合性と構造的なエレガンスの追求。*
