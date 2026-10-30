# BUG-0077 名前だけの一致（BUG-0071）が、自前の同名メソッドを持つテスト・fixture 内の呼び出しを src のメソッドに結び付ける

- 報告: 2026-10-31 / 見た人: 親（dogfood。`main` = `105af18`、Lucianke 自身のリポジトリを stdio で索引、LSP は indexing 中）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

`find_references_and_implementations {"target": {"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer.pack"}}`:

```
! 7 call site(s) marked conf=heuristic are matched by name only: the receiver's type is unknown and Packer.pack is the only method named 'pack' in the workspace (besides 3 in test files)
references: 8 files, 45 sites (tests: 43; by name: 7)
src/lucianke/mcp/fit.py:102:23  [call] in pack_and_render  ...  conf=heuristic
src/lucianke/mcp/fit.py:142:23  [call] in pack_and_render  ...  conf=heuristic
tests/fixtures/untyped_calls/app/service.py:10:23  [call] in render  return packer.pack(items)  conf=heuristic
tests/fixtures/untyped_calls/app/service.py:15:18  [call] in render_untyped  out = packer.pack(items)  ...
```

- `tests/fixtures/untyped_calls/app/engine.py:4-9` に自前の `class Packer` / `def pack(self, items)` がある（`tests/fixtures/untyped_calls/tests/test_service.py:5` にも `def pack`）。
- その fixture の中の `packer.pack(...)`（`service.py:10`・`:15` ほか、計 5 件）が、`src/lucianke/mcp/budget.py#Packer.pack` の参照として数えられている。
- `query_call_hierarchy`（`Packer._fit` incoming depth 2）でも、`render @tests/fixtures/untyped_calls/app/service.py:10`・`Holder.flush`・`from_factory`・`test_pack` が `Packer.pack` の呼び元として出る。
- `search_workspace_symbols "Packer"` の 2 件目が `tests/fixtures/untyped_calls/app/engine.py#Packer`。

## 利用者（呼ぶ側の AI）として困ること

- 名前の一致による推定が、明らかに別のクラス（呼び出しと同じ fixture の中にある同名のクラス）への呼び出しを、対象への参照として返す。取りこぼしより誤りのほうが悪い（影響範囲を誤認する）。

## 原因・直し方・確認（2026-10-31）

### 原因

- BUG-0071 の名前の判定（`tools/_name_calls.py` の `NameIndex.verdict`）は、名前ごとに 1 回だけ候補を決めていた。ワークスペースの
  同名の `Method` のうち、テスト以外の定義が 1 つでもあればテストファイルの定義をすべて捨て、呼び出し箇所がどこにあるかは見なかった。
- `tests/fixtures/untyped_calls/app/engine.py` はワークスペースの分類ではテストファイル（`tests/` の配下）なので、その `Packer.pack` は
  「テストダブル」として捨てられ、同じフィクスチャの中の `packer.pack()` 5 件が `src/lucianke/mcp/budget.py#Packer.pack` に結ばれた。
- 同じ規則の裏側で、本番の定義が無い名前は「テストファイルの定義だけで数える」（`production or all`）に落ちていた。同じ測り方で、
  本番とテストの `conn.execute()`（sqlite）391 件がテストの偽物 `Slow.execute` に、`proc.kill()` 15 件が `RawSession.kill` に
  結ばれていた（T-06 の `by_name`）。

### 直し方（決めたこと）

- **候補を呼び出し箇所ごとに、近い段から選ぶ**（`NameIndex.verdict_for`。最初に定義のある段で決める）:
  1. 同じファイル（テストファイルの中のテストダブルも含む）。
  2. 呼び出し箇所を含む**テストフィクスチャ**。ルートは「テストディレクトリ（`test` / `tests` と、言語の `test_path_globs` のうち
     `**/<名前>/**` の形のもの＝JS / TS の `__tests__`）の配下にある `fixtures` / `fixture` / `__fixtures__` / `testdata` /
     `test_data` の直下のディレクトリ」（`workspace/languages.py` の `fixture_roots` と `dir_names_of_tests`。入れ子なら内側から外へ。
     `tests/fixtures/db.py` のような `fixtures` 直下のファイルはルートを作らない）。
  3. ワークスペース（どのフィクスチャにも入っていない定義）。
- 2・3 の段では、その段のルートから見たテストファイルの定義（`files.is_test` と同じ規則を、フィクスチャならルートからの相対パスに
  当てる。`tests/fixtures/untyped_calls/tests/test_service.py#FakePacker` はフィクスチャの中のテストダブル）は数えない。段に同名の
  定義がテストダブルしか無ければ、そこで「候補なし」として止まる（外の段に結ばない）。呼び出し箇所を含まないフィクスチャ
  （兄弟のフィクスチャ）の定義は見えない。
- **呼び出しを囲むクラス自身のメソッドは、候補に数えるが唯一の候補でも結ばない**（`self.m()` は抽出器が結ぶので、型の分からない
  受け手は別のオブジェクト。`def kill(self): self.proc.kill()` のような委譲）。同じファイルの段を入れた最初の版で Lucianke 自身を
  測ると、同じファイルの段で結んだ 10 箇所のうち 9 箇所がこの委譲（`lock.acquire()`・`self._sampler.start()`・
  `super().print_help()`・`self.proc.kill()` など）で、すべて誤りだった。同じファイルの定義が囲みのクラス自身のものだけなら同じ
  ファイルの段は飛ばす（`WiredServices.ingest_ahead` の中の `rt.indexer.ingest_ahead()` は、ワークスペースの段で
  `Indexer.ingest_ahead` と `WiredServices.ingest_ahead` の曖昧のまま）。
- 本番の呼び出しは BUG-0071 のまま（テストの定義は数えない。ワークスペースの唯一の定義に結ぶ）。`fit.py:102` / `:142` →
  `Packer.pack` はそのまま。
- 依頼の「同じディレクトリの部分木（最も近い共通の祖先）」と「同じトップレベルのテストパッケージのテストファイルの定義も競う」は、
  **フィクスチャのルートだけを段にし、テストダブルは同じファイルの呼び出しにだけ結ぶ**形にした。理由（Lucianke 自身の索引で
  両案を試した）: 最も近い共通の祖先で順位を付けると、(a) 本番の同名メソッドの曖昧 127 箇所が近い方の推定に変わる（多態の受け手を
  誤認しうる）、(b) テストの呼び出しが隣のテストファイルの偽物に奪われる（テストの曖昧 186 箇所が推定に変わり、BUG-0071 で
  本番に結んでいた 114 箇所が別の先に変わった。見た先頭はどれも隣のファイルの偽物: `test_T08_diagnostics.py` の `wait_ready` →
  `test_T11_rename.py#StubPool.wait_ready`、`test_T13_two_servers.py` の `wait_job` → `test_T00_configure.py#Rig.wait_job`、
  `_t11_harness.py` の `resolve` → `test_T04_lsp.py#Env.resolve` など）。BUG-0071 の
  `tests/fixtures/untyped_calls/tests/test_pack.py:2` → `Packer.pack`（隣の `test_service.py#FakePacker` に奪わせない）も同じ理由。
- 代わりに落ちるもの: テストファイルが別のテストファイルの土台を import して呼ぶもの（`test_T06_T07_exact_marks.py` の
  `env.refs()` → `test_T06_T07_lsp.py#Env.refs` など 22 箇所）は結ばなくなった。テストの偽物への誤った結び付け（`Slow.execute`
  391・`Cur.fetchone` 105・`RawSession.kill` 15）を消す代わり。兄弟のテストモジュールの import が `lk://_ext/...` になっていて
  定義のファイルを辿れないため、import で近さを測ることもできなかった（票 **BUG-0082**）。
- 文言: `warnings` の根拠は段に応じて `… is the only method named 'x' in the workspace (besides N in test files)` /
  `in the test fixture <ルート>/ that holds the call` / `in the calling file`。同じ段に同名が複数なら
  `N methods share the name in the test fixture …`（ワークスペースの段は BUG-0071 の文言のまま）。`data.unattributed_calls` は
  対象が候補に入っている箇所だけ、理由と候補の組ごとに 1 要素。T-07 の `matched by method name only` の説明も段に合わせた。
- BUG-0071 の副作用も消えた: フィクスチャの `Packer.pack` を対象にした T-06 が `fit.py` の 2 件を「理由が空の未帰属」
  （`are not listed: ; e.g. src/lucianke/mcp/fit.py:102`）と書いていた。
- 仕様書: T-06 処理規約 7（段・フィクスチャのルートの定義・テストダブル・囲みのクラス・理由 1 行）、T-07 処理規約 9、§8.3 の
  `heuristic`、T-06 の json の `unattributed_calls`。

### 確認

Lucianke 自身（`fa82dc7` のスナップショットを旧コードで、作業ツリーのスナップショットを新コードで、同じ手順で in-process に索引。
LSP なしの `structural`。`src/lucianke/mcp/budget.py#Packer.pack`）:

修正前:

```
=== T-06 src Packer.pack references (compact)
! 7 call site(s) marked conf=heuristic are matched by name only: the receiver's type is unknown and Packer.pack is the only method named 'pack' in the workspace (besides 3 in test files)
references: 8 files, 45 sites (tests: 43; by name: 7)
src/lucianke/mcp/fit.py:102:23  [call] in pack_and_render  page = packer.pack(out, max_tokens=call.max_tokens, offset=offset, cursor_factory=cursor_factory)  conf=heuristic
src/lucianke/mcp/fit.py:142:23  [call] in pack_and_render  page = packer.pack(  conf=heuristic
tests/fixtures/untyped_calls/app/service.py:10:23  [call] in render  return packer.pack(items)  conf=heuristic
tests/fixtures/untyped_calls/app/service.py:15:18  [call] in render_untyped  out = packer.pack(items)  conf=heuristic
tests/fixtures/untyped_calls/app/service.py:24:27  [call] in Holder.flush  return self.inner.pack(["z"])  conf=heuristic
tests/fixtures/untyped_calls/app/service.py:28:19  [call] in from_factory  return make().pack(["y"])  conf=heuristic
tests/fixtures/untyped_calls/tests/test_pack.py:2:19  [call] in test_pack  assert packer.pack(["b"]) is not None  conf=heuristic
=== T-07 Packer._fit incoming depth 2（名前で結んだ呼び元）
  <- pack_and_render @src/lucianke/mcp/fit.py:102  when: fmt not in FITTED_FORMATS  conf=heuristic
  <- render @tests/fixtures/untyped_calls/app/service.py:10  when: items  conf=heuristic
  <- render_untyped @tests/fixtures/untyped_calls/app/service.py:15  conf=heuristic
  <- Holder.flush @tests/fixtures/untyped_calls/app/service.py:24  conf=heuristic
  <- from_factory @tests/fixtures/untyped_calls/app/service.py:28  conf=heuristic
  <- test_pack @tests/fixtures/untyped_calls/tests/test_pack.py:2  conf=heuristic
=== T-06 tests/fixtures/untyped_calls/app/engine.py#Packer.pack
! 7 call site(s) of .pack() with a receiver of unknown type are not listed: ; e.g. src/lucianke/mcp/fit.py:102, src/lucianke/mcp/fit.py:142, tests/fixtures/untyped_calls/app/service.py:10, …
references: 1 files, 1 sites (tests: 1)
```

修正後:

```
=== T-06 src Packer.pack references (compact)
! 2 call site(s) marked conf=heuristic are matched by name only: the receiver's type is unknown and Packer.pack is the only method named 'pack' in the workspace (besides 7 in test files)
references: 6 files, 40 sites (tests: 38; by name: 2)
src/lucianke/mcp/fit.py:102:23  [call] in pack_and_render  page = packer.pack(out, max_tokens=call.max_tokens, offset=offset, cursor_factory=cursor_factory)  conf=heuristic
src/lucianke/mcp/fit.py:142:23  [call] in pack_and_render  page = packer.pack(  conf=heuristic
json totals.by_name: 2 ['src/lucianke/mcp/fit.py:102', 'src/lucianke/mcp/fit.py:142']
=== T-07 Packer._fit incoming depth 2（名前で結んだ呼び元）
  <- pack_and_render @src/lucianke/mcp/fit.py:102  when: fmt not in FITTED_FORMATS  conf=heuristic
=== T-06 tests/fixtures/untyped_calls/app/engine.py#Packer.pack
! 5 call site(s) marked conf=heuristic are matched by name only: the receiver's type is unknown and Packer.pack is the only method named 'pack' in the test fixture tests/fixtures/untyped_calls/ that holds the call (besides 1 in test files)
references: 3 files, 6 sites (tests: 6; by name: 5)
（service.py:10・:15・:24・:28、tests/test_pack.py:2）
```

（`besides 7` は今回足したフィクスチャ `tests/fixtures/name_scopes` の `pack` 4 つを含む。T-06 の `Slow.execute` の `by_name` は
391 → 0、`RawSession.kill` は 15 → 0。）

索引全体（同じ `fa82dc7` の索引に旧コードと新コードの判定を当てた。型不明の呼び出し 9,875 箇所）: 結んだ 1,373 → 826、
曖昧 834 → 833、組み込み型と同名 2,217 → 2,185、候補なし 5,451 → 6,031。減った結び付けは、テストの偽物への誤り（上の 3 つで
511）・囲みのクラスへの委譲・フィクスチャの呼び出しの付け替えと、上の 22 箇所。

テスト（決定的。新しいフィクスチャ `tests/fixtures/name_scopes`、バイト列のまま `-text`）:

- `tests/contract/test_T06_T07_name_scopes.py`（10 件。旧コードでは 9 件が落ちる）: 同名のクラスを持つフィクスチャの中の呼び出しは
  `app/budget.py#Packer.pack` に結ばず、フィクスチャ自身の `Packer.pack` に結ぶ（フィクスチャの中の偽物 `FakePacker` は数えない）、
  兄弟のフィクスチャは見えない、本番の呼び出しとより近い定義の無いテストの呼び出しはワークスペースの唯一の定義に結ぶ、フィクスチャの
  中の同じ近さの 2 つ（`Left.settle` / `Right.settle`）は曖昧として要約し外の `Packer.settle` には結ばない、本番の同名 2 つは
  BUG-0071 のまま曖昧、テストダブルは同じファイルの `self.conn.execute()` にだけ結ぶ（本番・別のテストファイルの `conn.execute()`
  は結ばない）、フィクスチャに偽物しか無い名前は外に落ちない・名前の無いものはワークスペースに落ちる、囲みのクラスのメソッドへの
  委譲は結ばない（`Session.terminate`）・他の候補と曖昧のまま（`Calc.reset`）、T-07 の incoming / outgoing、compact の文法。
- `tests/unit/test_workspace_languages.py`: `fixture_roots`（12 通り）と `dir_names_of_tests`。
- 既存の `tests/contract/test_T06_T07_name_calls.py`（13 件）・`tests/unit/test_name_calls.py`（11 件）は変更なしで通る。
- `uv run ruff check`: クリーン。T-06 / T-07 の契約（`test_T06_T07*.py`・`test_T06_ts_outside_tsconfig.py`・
  `test_T07_builtins_hidden.py`・`test_T07_ts_outside_tsconfig.py`）と上の単体: `310 passed, 6 skipped, 2 errors`。2 件の error は
  実 LSP のテスト（`test_T06_T07_lsp.py::test_t07_editing_a_file_makes_the_cache_stale`、
  `test_T06_T07_lsp_postconditions.py::test_t06_postcondition_1_exact_references_equal_the_lsp_result[False]`）の setup の
  `TimeoutError`（他の役が並行に走る PC。票 BUG-0076 の症状）で、その 2 つだけを流すと `3 passed`。`test_import_graph.py`・
  `test_tools_autoregister.py`: 通過。全体テストは親が main でまとめて回す。
- 見つけた別の穴: **BUG-0082**（テストの兄弟モジュールの import とフィクスチャの中の import が `lk://_ext/...` になり、`Env`・`Rig`・
  フィクスチャの `Packer` を import して使うファイルが参照に出ない。見たことだけ）。
