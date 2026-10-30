# BUG-0072 全体テストで `test_import_graph.py::test_dependency_directions` が落ちる（`cli/selftest.py` が `bootstrap_io` を import）

- 報告: 2026-10-31 / 見た人: BUG-0066 を直した役（全体テストを回したときに見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 症状（実測）

- `uv run pytest -q -p no:cacheprovider -rfs`（作業ツリー = `c149e0d` + BUG-0066 の直し）→ **1 failed, 3786 passed, 32 skipped**（867 秒）。
- 落ちたのは `tests/unit/test_import_graph.py::test_dependency_directions` の 1 件:

```
E       AssertionError: cli\selftest.py imports composition root 'bootstrap_io'
E       assert not ["cli\\selftest.py imports composition root 'bootstrap_io'"]
```

- 単独でも落ちる: `uv run pytest -q -p no:cacheprovider tests/unit/test_import_graph.py` → 1 failed。
- BUG-0066 の変更（T-06・T-07 とテスト）は `cli/selftest.py` に触れていない。

## 手がかり（見たことだけ）

- `src/lucianke/cli/selftest.py` は `4ba7c0a`（exe 配布）で入った。21 行目 `from lucianke.bootstrap_io import bootstrap_io, open_transport_streams`。モジュールの説明に「import の時点で ``bootstrap_io`` 以外を読まない」とある。
- BUG-0065 の票の全体テスト（3783 passed・0 failed）は `9d96cd2` の上で回したもので、`4ba7c0a` より前。

## 再現

```
uv run pytest -q -p no:cacheprovider tests/unit/test_import_graph.py
```

## 原因・直し方・確認（2026-10-31）

- 原因: exe 配布のコミット `4ba7c0a`（親が入れた）で足した `cli/selftest.py` が、合成ルートの `bootstrap_io` を import していた（設計書 §4.2: サブパッケージから合成ルートを import しない）。親は変更範囲の単体だけ流して全体テストを流さずに push した。
- 直し方: 自己診断の本体を合成ルートの `bootstrap_io.run_stdout_selftest(marker)` に移し、`cli/selftest.py` を消した。`cli/doctor.py` は綴り `SELFTEST_SUBCOMMAND = "_doctor-selftest"` を持ち、`__main__` が同じ綴りで振り分ける（cli から合成ルートを import できないので定数は共有しない）。綴りの一致は `test_hidden_selftest_subcommand_keeps_stdout_pure` が `doctor.selftest_argv(frozen=True)` の argv で `python -m lucianke` を起動して確かめる。
- 確認: `tests/unit/test_import_graph.py`・`test_cli_doctor.py`・`test_server_launch_guard.py`・`test_smoke.py` → 34 passed。`build.ps1 -NoInstall` の smoke（exe の `doctor` の stdout isolation が隠しサブコマンドを通る）→ PASSED。`uv run ruff check` clean。
