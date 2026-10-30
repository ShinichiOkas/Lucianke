# BUG-0083 負荷の高いときに `test_workspace_watcher.py` の試験が落ちる（落ちる試験と落ち方が毎回変わる。揺れるテスト）

- 報告: 2026-10-31 / 見た人: BUG-0076 を直す役（負荷を掛けた試験の背景の負荷として `tests/unit` を回していて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `547a320` + BUG-0076 の作業中の変更。BUG-0076 の変更は `tests/unit/test_workspace_watcher.py` と `workspace/watcher` に触れていない）

同じ PC（14 コア）で pytest を 4 本並べて流していたとき（`tests/unit tests/invariants`・`tests/contract`・`tests/unit` の一部の繰り返し・
実 LSP の契約テスト）:

1. `uv run pytest -q -p no:cacheprovider tests/unit tests/invariants` → `2 failed, 3092 passed, 18 skipped in 538.43s`
   - `FAILED tests/unit/test_workspace_watcher.py::test_git_tmp_and_excluded_paths_never_reach_the_sink`
   - `FAILED tests/unit/test_workspace_watcher.py::test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes`
2. 続けて `uv run pytest -q -p no:cacheprovider tests/unit/test_workspace_watcher.py`（他の 3 本は流れたまま）→ `1 failed, 25 passed in 21.37s`
   - `test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes`: `tests\unit\test_workspace_watcher.py:77`
     `AssertionError: timed out waiting for the condition`（試験の `until`。上限は `WAIT_S = 5.0`）
3. もう一度同じコマンド → 2 件落ちた（どの試験かは控えていない。落ち方は 1・2 と違った）:
   - `:279` `assert "a.py" not in sink.of("changed")` → `AssertionError: assert 'a.py' not in ['a.py', 'b1.py']`
   - `assert sink.of("changed") == ["zz_barrier.py"]` → `['d0/f540.py', ... ]`（`Left contains 60 more items, first extra item: 'd0/f546.py'`）
4. もう一度 → `1 failed, 25 passed in 15.27s`（`test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes`）

負荷を外してから（他の pytest がすべて終わってから）同じコマンドを 3 回 → `26 passed in 22.92s` / `26 passed in 9.39s` / `26 passed in 11.48s`。
同じ負荷の下でも、`tests/unit tests/invariants` の 2 回目・3 回目は `3094 passed` / `3098 passed`（落ちない回もある）。

## 再現手順

- 何か CPU を飽和させる負荷（例: pytest を 3 本並べて `tests/unit` などを繰り返す）を掛けながら
  `uv run pytest -q -p no:cacheprovider tests/unit/test_workspace_watcher.py` を数回流す。

## 困ること

- 全体テストを並行で流すと、負荷しだいで赤になる（揺れるテストは無価値。BUG-0076 と同じ類）。

## 原因・直し方・確認（2026-10-31）

### 再現

- CPU を飽和させる負荷（14 コアの PC で忙しく回るだけのプロセスを 28 個）を掛けながら
  `uv run pytest -q -p no:cacheprovider tests/unit/test_workspace_watcher.py` を 8 回 → 8 回とも `1 failed, 25 passed`
  （`test_burst_of_600_files_triggers_one_rescan_and_no_individual_changes` が `:352` の `until(rescan)` で 5 秒の時間切れ。票の 2・4）。
  忙しいプロセス 20 個 + pytest 2 本の負荷で 3 回 → 3 回とも同じ。
- 窓を記録する診断（`Watcher` に渡る窓の時刻と件数を控える。28 個の負荷）: 600 件の作成に **6.21 秒**（軽いときは 1.3 秒）。
  awatch の窓は `176 件 @6.215 s`・`436 件 @7.111 s`・`3 件（d0, d3, d4）@7.914 s` → rescan 0 回・個別の changed 600 件。
  軽い回は `606 件 @1.617 s` の 1 窓で rescan 1 回。取りこぼしは無い（届いたパスの延べが 616 / 607）。
- 自分の書き込みの試験（票の 3 の `:279`）: 書き手が切り詰めと書き込みの間で 50 ms 止まる書き方（負荷で書き手が止まるのと同じ）に
  すると 5 回中 5 回、窓が `{a.py} @0.080 s`・`{a.py, b1.py} @0.103 s` に割れ、`changed` が票と同じ `['a.py', 'b1.py']` になる。

### 原因

1. **600 件の試験の時間切れ（票の 2・4）。** 窓の割れ方は書く側の速さで決まる。負荷で 600 件の作成が debounce（1 秒）より長く
   かかると、awatch は debounce で窓を切る。この試験は BUG-0006 で `step_ms=400` にしていたので、次の窓は早くても 2 step
   （800 ms）後に届き、合算の静穏 `WATCH_BURST_QUIET_S`（0.3 秒）を過ぎる → 窓ごとに処理され、どの窓も `WATCH_BURST_FILES`（500）
   以下なので個別の changed になる（S-06 の規則どおりで、索引は正しい）。試験が「600 件が 1 つの窓（か 0.3 秒以内の窓）で届く」
   ことを前提にしていた。
2. **60 件の余分な changed（票の 3 の 2 件目）。** 同じ仕組みで窓が 540 件と 60 件に割れた。540 件は rescan、後から届いた
   `f540`〜`f599` の 60 件は小さい窓として個別に処理された。あわせて、偽の sink の rescan が索引にファイルを載せない作りだったので、
   後から届くディレクトリだけの窓（上の `3 件（d0, d3, d4）`）はディレクトリごと展開されて個別の changed になりえた（潜在）。
3. **`'a.py' in changed`（票の 3 の `:279`）。** 試験の「自分の書き込み」は `write_bytes`（切り詰め + 書き込み）で、書き手が 1 step
   （10 ms）以上止まると 2 つの窓に割れる。期待（`expect`）は最初の窓で消費され、2 つ目の窓が届いた。**製品の不具合でもある:**
   編集トランザクションの書き込み（一時名 + `os.replace`）も deleted と added の 2 つのイベントになり（S-06）、負荷で別々の窓に
   割れうる。期待を 1 回で消費するので 2 つ目が二重取り込みになり、`expect` の目的（二重取り込みの抑止）を果たしていなかった。
4. **`test_git_tmp_and_excluded_paths_never_reach_the_sink`（票の 1）。** 票に落ち方が残っておらず、こちらの負荷つき 21 回（直す前
   8 回・直した後 13 回）では落ちなかった。この試験は `sorted(changed) == ["real.py", "zz_barrier.py"]` と**回数まで**比べており、
   `put("real.py")`（作成 + 書き込み）が 3 と同じく 2 つの窓に割れると `real.py` が 2 回届いて落ちる。上限 5 秒の待ちもあった。
5. **同じ型の潜在の揺れ。** 回数を数える主張（`count("a.py") == 1`・`sorted(...) == [...]`・`finished == ["a.py"]`・
   `removed == ["a.py"]`）、見張りのファイルを `put`（2 つのイベント）で書いていたこと、1 回きりの計測で見る時間の予算
   （反映 `INDEX_REFLECT_MS` = 500 ms・停止 1 秒）、5 秒の上限。

### 直し方

- **製品（`workspace/watcher.py`）:** `expect` の期待を、ディスクの内容が `sha256_after` と**一致する間は残す**（`WATCH_EXPECT_TTL_S`
  = 10 秒で失効）。一致しない内容を見たら消費してそのイベントを渡す（その後の別の変更を取りこぼさない、は従来どおり）。
  削除の期待（`""`）も不在の間は捨てる。設計書 §7.3.6（API の注記・手順 8・§8.5 の流れの 3）を合わせた。決定的な試験
  `test_expectation_drops_every_echo_while_the_content_matches`（割れて届いたこだまを `_consume` に直接流す。直す前は 2 件届いて
  落ちることを確かめた）と `test_expectation_expires_after_its_ttl` を足した。監視の定数（step・debounce・静穏・バースト）は
  変えていない（窓の割れ方で個別処理になるのは S-06 の規則どおり）。
- **試験（`tests/unit/test_workspace_watcher.py`）:**
  - 条件待ちの上限 `WAIT_S` を 60 秒に（失敗したときだけ使い切る）。
  - `running` は監視を始めたら見張りが届くのを待ち、記録を空にしてから渡す（監視が動いている・準備のイベントを出し切った状態から
    始める）。見張りは中身の無い新しいファイル（作成のイベントだけ）で、主張からは除く（`FakeSink.got`）。
  - 主張は「届いた／届かない」をパスの集合で書き、回数は数えない（sink は冪等で、1 回の書き込みが何回届くかは決まっていない）。
  - 600 件の試験は `HeldWindows`（本物の `awatch` を包み、600 件すべての作成のイベントが watchfiles から届くまで窓を貯めて 1 つにして
    渡す）で「1 つの窓で届いた」状況を決まった形で作り、`sink.root` を設定して本物の rescan と同じく索引にファイルを載せる。主張は
    「rescan 1 回」「600 件は 1 つの窓で渡した」「個別の changed は束ねた窓の後に別に届いたイベントのパスだけ」。窓が割れたときの
    合算の規則は既存の決定的な試験（`test_windows_split_by_awatch_are_merged_into_one_rescan`）が確かめる。名前を
    `test_burst_of_600_files_in_one_window_triggers_one_rescan` に変えた。
  - 自分の書き込みの試験は編集トランザクションと同じ書き方（一時名 + `os.replace`）にした（名前を `test_expected_self_write_is_dropped` に）。
  - 時間の予算（反映 500 ms・停止 1 秒）は、外の負荷で止まった回を除くため、予算に収まるまで最大 5 回くり返した最良の回で見る
    （設定や実装が予算を満たせないなら全部の回で超える）。`test_single_write_changed_once_within_budget` は回数を見なくなったので
    `test_single_write_is_reflected_within_budget` に名前を変えた。
  - `test_drain_waits_for_the_sink_to_finish` は gate を監視が動いてから掛け、`a.py` は中身の無い新しいファイルで作る。
    `test_drain_cuts_short_the_burst_merge_wait` の静穏は 3600 秒に（上限を 60 秒にしたので、30 秒のままだと drain が打ち切らなくても通る）。

### 確認

- **CPU を飽和させる負荷**（忙しいプロセス 28 個）の下で `tests/unit/test_workspace_watcher.py` を 5 回 → 5 回とも `28 passed`
  （72 / 87 / 61 / 72 / 70 秒）。直す前は同じ負荷で 8 回とも赤。
- **pytest を 3 本並べた負荷**（`tests/unit tests/invariants`・`tests/contract`・`tests/unit` の索引/編集の重いファイルの繰り返し）の下で
  5 回 → 5 回とも `28 passed`（112 / 84 / 34 / 16 / 16 秒）。
- **両方を重ねた負荷**（28 個 + pytest 3 本）で 3 回 → 3 回とも `28 passed`（98 / 47 / 52 秒）。
- 負荷なし 3 回 → `28 passed`（10 / 10 / 8 秒）。
- 実物の `Watcher` を使う `tests/unit/test_tools_runtime.py` と `test_edit_transaction.py`・`test_core_limits.py` → 負荷の下で 4 回 `64 passed`（その前の 1 回は `test_edit_transaction.py` が
  37 件 ERROR。`FakeWatcher` を使う試験で、この変更とは別。票 BUG-0088）。
- `uv run ruff check` → `All checks passed!`。変えた 2 ファイルの `ruff format --check`・`basedpyright` も通る。全体テストは流していない
  （親が main で 1 本流す）。
- 見つけた別の不具合（見たことだけの票）: BUG-0087（編集の再索引が失敗しても `expect` を登録するので、自分の書き込みのイベントが
  捨てられ索引が古いまま残りうる）、BUG-0088（負荷の高いときに `test_edit_transaction.py` が 37 件まとめて ERROR になった。1 回だけ）。
