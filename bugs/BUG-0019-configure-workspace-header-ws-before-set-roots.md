# BUG-0019 set_roots 応答のヘッダが ws=- のまま（解決がハンドラの前だけで、ハンドラが変えたアクティブが戻らない）

- 報告: 2026-10-29 / 見た人: T-00 を作る役（17a）
- 症状: ワークスペースが 1 つも無い状態で `configure_workspace` を `action=set_roots` で呼ぶと、成功応答（compact）のヘッダが `ws=-` になる。同じ応答の本文は `workspaces: 1 (active: poly)`。続く `status` のヘッダは `ws=poly`。
  - 実測（tests/contract/test_T00_configure.py の偽サービス）: `# configure_workspace ok conf=exact lsp=n/a rev=WORKING_TREE ws=- ~51/1500tok`
  - 期待: 仕様書 §11 T-00 の出力例は `ws=app`（登録後のアクティブ）。
  - 観察: ヘッダの `ws=` は `Pipeline` がハンドラの前に `Services.resolve_workspace_id` で解決した値（BUG-0004 の直し）。`set_roots` はハンドラの中でアクティブを作る・変えるので、その結果がヘッダに戻る道が無い。
- 再現手順: `tests/contract/test_T00_configure.py` の `make_rig` で空の `FakeHub` を作り、`rig.call(fmt="compact", action="set_roots", root_paths=[<存在するディレクトリ>])` の 1 行目を見る。
- 調査はしていない（直す役の仕事）。

## 原因
- ヘッダの `ws=` は `Pipeline` がハンドラの前に `Services.resolve_workspace_id` で解決した値（BUG-0004 の直し）。ワークスペースを作る・変えるのはハンドラの中（T-00 `set_roots`）で、その結果が `CallInfo` に戻る道が無かった。

## 直し方
- `ToolOutput.workspace_id: str | None = None` を足した（`core/records.py`）。ハンドラが確定した ID。`Pipeline` はハンドラの後、描画の前に、これがあって `CallInfo.workspace_id` と違えば差し替える（`mcp/pipeline.py`。ログの `workspace_id` も同じ値）。T-00 は応答に常にアクティブな ID を入れる。他のツールは `None` のままで挙動は変わらない。
- テスト: `tests/unit/test_mcp_pipeline.py`（ハンドラが返した ID がヘッダに出る、返さないツールは従来どおり）、`tests/contract/test_T00_configure.py`（`set_roots` の compact のヘッダが `ws=poly`）。

## 確認
- `tests/contract/test_T00_configure.py`・`tests/unit/test_mcp_pipeline.py` ほか 138 件を 3 回続けて green。
