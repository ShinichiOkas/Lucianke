# BUG-0121 search_workspace_symbols が修飾名の部分一致で、一致したクラスのメンバーまで返す（能力ベンチの T02 で適合率 0.5）

- 報告: 2026-10-31 / 見た人: 親（能力測定の基準〈Mnemo の能力ベンチ〉の課題 T02）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（`main` = `b124359`、Mnemo の固定の写し）

- T02「名前に 'InstanceLock' を含むクラス・関数をすべて」（`search_workspace_symbols {"query": "InstanceLock"}`）: 再現率 1.0、適合率 0.5。
- 誤検出 4 件は `companion_sdk/src/mnemo_companion/_instance.py` の `InstanceLock` クラスのメンバー（28 行 `InstanceLock.info_path`、32 行 `InstanceLock.acquire` など）。メンバーの名前自体は 'InstanceLock' を含まず、修飾名（`InstanceLock.acquire`）が含む。

## 利用者（呼ぶ側の AI）として困ること

- 名前で探すと、一致したクラスの中身が並んで、探している定義が埋もれる（トークンも使う）。メンバーが欲しいときは `InstanceLock.` のようにドット付きで探せる。
