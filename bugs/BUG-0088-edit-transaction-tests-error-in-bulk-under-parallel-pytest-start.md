# BUG-0088 負荷の高いときに `tests/unit/test_edit_transaction.py` の試験が 37 件まとめて ERROR になった（1 回だけ。揺れるテスト）

- 報告: 2026-10-31 / 見た人: BUG-0083 を直す役（watcher の変更の巻き添えが無いかを確かめる試験を流していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = origin/main `0fee57d` + BUG-0083 の作業中の変更。BUG-0083 は `workspace/watcher.py` だけを変え、
`test_edit_transaction.py` は偽の watcher（`FakeWatcher`）を使う）

同じ PC（14 コア）で、別の pytest を 3 本（`tests/unit tests/invariants`・`tests/contract`・`tests/unit` の索引/編集の重いファイルの
繰り返し）を**流し始めた直後**に:

- `uv run pytest -q -p no:cacheprovider tests/unit/test_tools_runtime.py tests/unit/test_edit_transaction.py tests/unit/test_core_limits.py`
  → `27 passed, 37 errors in 86.40s`
  - 出力の最後に見えた 2 行: `ERROR tests/unit/test_edit_transaction.py::test_zero_wait_answers_pending_without_waiting`、
    `ERROR tests/unit/test_edit_transaction.py::test_without_an_lsp_the_delta_is_the_tree_sitter_error_difference`
  - エラーの本文は控えていない（`tail -3` しか見ていなかった）。37 = `test_edit_transaction.py` の 41 件のほとんど（ERROR なので
    セットアップか後始末で落ちている）。
- 直後に同じ負荷の下で `test_edit_transaction.py` だけ → `41 passed`。同じ 3 ファイルを 4 回 → 4 回とも `64 passed`。

## 再現手順（未確認）

- 別の pytest を何本か同時に**起動しながら**、上の 3 ファイルを流す。

## 困ること

- 全体テストを並行で流すと、負荷しだいで赤になりうる（揺れるテストは無価値）。

## 原因・直し方・確認（2026-10-31）

**再現できなかった。原因は分かっていない（直したとは言わない）。** 次に起きたときに全文が残る見張りを置いた。

### 再現の試み（どれも赤にならなかった）

台本（`repro0088.py`。別の pytest を 3 本と対象を同時に起動し、対象が終わったら 3 本を木ごと止める）で、対象は票と同じ
`uv run pytest -q -p no:cacheprovider tests/unit/test_tools_runtime.py tests/unit/test_edit_transaction.py tests/unit/test_core_limits.py`。
3 本は票と同じ `tests/unit tests/invariants`・`tests/contract`・索引/編集の重いファイル。対象は 3 本の起動から 0 / 0.5 / 1 / 2 / 3 秒後。

| 組 | コード | 負荷 | 回数 | 結果（対象の時間） |
|---|---|---|---|---|
| A | origin/main `bf74fb5`（別の写し） | pytest 3 本 | 8 | 8 回とも `64 passed`（13〜15 秒） |
| B | 同上 | pytest 3 本 + 忙しいプロセス 20 個 | 6 | 6 回とも `64 passed`（45〜75 秒。票の 86 秒に近い重さ） |
| C | 同上 | 3 本目を「短い pytest を起動し続けるループ」に（セッションの開始・終了 = `tmp_path` の根の作成・掃除を数秒ごとに起こす）+ 忙しいプロセス 10 個 | 8 | 8 回とも `64 passed`（31〜49 秒） |
| D | この直し（BUG-0087 の試験 5 本を足して 69 件・見張りつき）を origin/main `525bd6e` に載せた作業ツリー | C と同じ | 6 | 6 回とも `69 passed`（34〜64 秒） |

### 票の数字から言えること

- `27 passed, 37 errors` の合計 64 は試験の数と同じ。後始末の ERROR なら同じ試験が passed と error の両方に数えられて 64 を
  超えるので、**37 件はセットアップの ERROR**。
- 37 は `test_edit_transaction.py` の非同期の試験の数ちょうど（当時の 41 件 − 同期の `test_verify_*` 4 件）。37 件はどれも
  `tmp_path` と非同期のフィクスチャ `index_writer` を使う。先に流れた `test_tools_runtime.py`（20 件。どれも `tmp_path` と非同期）と、
  後の `test_core_limits.py`（同期 3 件）と `test_verify_*` は通った。→ `test_tools_runtime.py` の後、ファイル 1 本の間ずっと、
  非同期で `tmp_path` を使う試験のセットアップに共通の何かが失敗していた。
- セットアップの段（共有のもの）と見たこと:
  1. `tmp_path`: 根 `%TEMP%\pytest-of-<user>` は、この PC の全部の作業ツリー・別のプロジェクトの pytest と共有。各セッションの
     basetemp は `.lock` で守られ、掃除（`cleanup_numbered_dir`）は生きている `.lock` を 3 日間消さない（pytest 9.2.0 の
     `_pytest/pathlib.py` の `ensure_deletable` を読んで確かめた）。basetemp が消えれば、以降の `tmp_path` はすべて
     `could not create numbered dir ...` になり、票の形に合う。組 C で掃除を数秒ごとに起こしても消えなかった。
  2. pytest-asyncio のイベントループの作成（Windows の Proactor はループバックの TCP の socketpair を作る。OS 全体の資源）。
     失敗すれば非同期の試験だけがセットアップで落ち、票の形に合う。調べた時点で `TIME_WAIT` は 319、動的ポートは 16384。
  3. `tests/fixtures/poly` の `copytree`（同じ作業ツリーの並行の pytest と共有）: 書き込む試験は無い（grep）。全作業ツリー 22 本と
     本体の `tests/fixtures/poly` はどれも 46 件のまま（`__pycache__` などの残りなし）。
  4. `IndexWriter` の起動: 試験ごとの DB（`tmp_path` の中）。起動の上限は 60 秒。
  どれが当たりかは本文が無いので決められない。
- 本文が残らなかった理由: pytest の短い要約の `ERROR …` 行は端末の幅で切られ、パイプでは 80 桁なので、試験名が長いと
  ` - <理由>` が丸ごと消える（票の 2 行に理由が無いのはこのため）。全文は上の `ERRORS` の節に出ていたが `tail -3` で落ちた。

### 置いた見張り（`tests/conftest.py`）

- 赤い報告（セットアップ・本体・後始末）の全文を `<一時ディレクトリ>\lucianke-test-errors\<時刻>-<pid>.log` に追記する
  （新しいものから 50 本を残す）。頭に pid・argv・cwd・`rootpath`・**basetemp**・一時ディレクトリ・`PYTEST_DEBUG_TEMPROOT` を書く
  （共有のパスが原因かを見分けるため）。
- 最後の要約の行の**後**に `N red report(s) written in full to <ファイル> (first: <試験> [setup]: <例外の 1 行目>)` を 1 行出す
  （`| tail -3` でも理由とファイルが見える）。
- 試験 `tests/unit/test_red_report_log.py`（`tests/conftest.py` を写した入れ子の pytest。長い名前の試験のセットアップの ERROR で、
  最後の行に理由とパスが出て、ファイルに全文と basetemp があること。緑のときは何も書かないこと）。`test_leak_guard.py` の入れ子の
  pytest は一時ディレクトリを `tmp_path` の中に向けた（本物の記録を汚さない）。
- 試験の分離そのもの（`test_edit_transaction.py`）は、ワークスペースの写し・DB・ジャーナルが試験ごとの `tmp_path`、ロックは
  プロセスの中、LSP・監視・索引は偽物で、プロセスをまたいで共有するのは上の 1〜3 だけ。`tmp_path` の根を作業ツリーごとに分ける案は、
  pytest の `.lock` が既に守っていて効く根拠が無く、作業ツリーごとに大きな残りが溜まるので採らなかった。
- 見張りが働くことは、この作業の中の別の赤（BUG-0097）で確かめた: 最後の行が
  `1 red report(s) written in full to C:\Users\000095~1\AppData\Local\Temp\lucianke-test-errors\20261031-075330-4820.log (first: tests/contract/test_T13_two_servers.py::… [call]: AssertionError: …)`
  になり、ファイルに basetemp と全文が残った。

### 確認

- `uv run ruff check` → `All checks passed!`。`tests/conftest.py`・`tests/unit/test_red_report_log.py`・`tests/unit/test_leak_guard.py` の
  `ruff format --check` と `basedpyright` → 0 件。
- `tests/unit/test_red_report_log.py tests/unit/test_leak_guard.py` → `3 passed`（載せる前と後）。
- 並べて起動する再現: 上の表のとおり 28 回（直す前のコード 22 回・この変更 6 回）。どれも赤にならなかった。
- 状態: **未再現のまま**。次に起きたら、最後の行に理由とファイルが出て、ファイルに全文と basetemp が残るので、それで原因を決める。
