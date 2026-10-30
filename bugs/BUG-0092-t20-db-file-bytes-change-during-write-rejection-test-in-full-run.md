# BUG-0092 F-PY の T-20 の契約テスト（書き込み文を断り、DB ファイルが変わらない）が、`tests/contract_fixtures` の全体で流すと DB ファイルの SHA-256 が変わって落ちる

- 報告: 2026-10-31 / 見た人: BUG-0080・BUG-0086 を直す役
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測

- 落ちた試験: `tests/contract_fixtures/test_fx_db_T19_T20.py::test_t20_postcondition_1_2_writes_are_rejected_and_the_db_is_untouched[F-PY]`
  の 87 行（`assert sha(path.read_bytes()) == before_hash and counts(path, views) == before_counts`）。左の SHA-256 の比較で落ちた
  （行数の比較は評価されていない）:

  ```
  E       AssertionError: assert ('fb3627bbff56...78ef894363368' == '9c9257c07c9d...941bc6c6eddbe'
  ```

  もう 1 回は `'d0408db0ea2a...df4ac67e2b34c' == '306edf194606...6ed2e5c51d6fe'`。
- 作業ツリー = `8c10a2a`（BUG-0079 & BUG-0081、スキーマ v7）+ BUG-0080・BUG-0086 の直し（`tests/contract_fixtures/_rig.py`・
  `test_fx_read_T00_T08.py`・`test_fx_edit_T10_T13.py` と `_lsp_exact.CallEdgeSource` の予算の既定の読み方。T-19・T-20 の試験と索引・DB の
  コードには触れていない）。`uv run pytest -q -p no:cacheprovider -rfs tests/contract_fixtures` を:
  - 単独で 1 回 → `1 failed, 44 passed, 6 skipped`（377 秒）。落ちたのは上の 1 件。
  - 2 本同時に → 1 本は `45 passed, 6 skipped`、もう 1 本は `1 failed, 44 passed, 6 skipped`（同じ 1 件）。
- 同じ作業ツリーで上の試験だけを 3 回流すと、3 回とも通った（各 8〜11 秒）。
- 載せ直す前（`c7f2b7e`・`9f4ffa3` + 同じ直し）の `tests/contract_fixtures` 全体 5 回では、この試験は毎回通っていた。

## 困ること

- 全体テストで T-20 の契約テストが揺れる。試験は書き込み文を 10 個断ったあと `SELECT count(*)` を 1 回流すだけなので、その間に
  DB ファイルのバイト列を変えたのが何かが分からない（事後条件 T-20#2「DB ファイルの SHA-256 は変わらない」が守られていないのか、
  試験の前提〈索引が落ち着いている〉が崩れているのかを、この出力からは区別できない）。

## 原因・直し方・確認（2026-10-31）

### 原因

- **試験の前提（索引が落ち着いた = DB のバイト列が止まった）が崩れていた（試験の側の誤り）。製品は DB に書いていない。**
  PaaD は WAL。唯一の書き手 `IndexWriter._loop`（`src/lucianke/index/writer.py`）は、書き込みが `IDLE_CHECKPOINT_S`（2.0 秒）途切れると
  `wal_checkpoint(PASSIVE)` で WAL の頁を本体へ写す。`_rig.open_rig` は索引ジョブの完了と LSP の ready までしか待たないので、
  LSP の準備が「最後のコミットから 2 秒」より早く済むと、T-20 の試験は WAL に写していない頁が残ったまま本体のハッシュを取る。
  その 2 秒目が試験の最中（書き込み文 10 個 + `SELECT` 1 個、約 40〜80 ms）に来ると、本体のバイト列が変わって落ちる。
- 実測（書き手のコミット・チェックポイントと試験の開始・終了に時刻を付けて `tests/contract_fixtures` 全体を 2 本同時に流した）:
  F-PY の T-20 の本体は、最後のコミット（`pagerank`）から **1.84 秒後** に始まり（2 本とも 17.686 → 19.525 s、17.767 → 19.611 s）、
  その間に PASSIVE は走っていない。窓まで 0.16 秒しかなく、負荷で少し遅れれば写しが試験の中に入る。F-TS は LSP が無いので
  0.05 秒後に始まって 40 ms で終わり、たまたま窓に入らない。
- 決定的な再現: 索引完了後の暇なときの PASSIVE を試験本体の開始直後まで遅らせる pytest プラグイン（全体テストで LSP が早く ready に
  なったときの時刻の並びを作る）で、直す前の試験は F-PY で 3 回とも票と同じ assert（87 行の SHA-256 の比較）で落ちる。
- 製品の側の確認: `query_project_db` は `open_public`（`src/lucianke/index/public.py`）で `mode=ro` の URI + `query_only=ON` +
  authorizer で開き、ジャーナルの設定も変えない。本体・`-wal`・`-journal` のハッシュと一覧を前後で比べる製品の試験は既に
  `tests/invariants/test_public_db_readonly.py` にある（delete と wal の両方）。

### 直し方（試験だけ。製品は変えていない）

- `tests/contract_fixtures/test_fx_db_T19_T20.py`:
  - `settle_db(rig)`: 書き手に何もしないジョブを積んで待つ（キューは先入れ先出しなので、それより前のジョブはコミット済み）→ 別の接続で
    `wal_checkpoint(TRUNCATE)` を流し、`(0, 0, 0)`（busy なし・WAL 空）を確かめる。写し切れないなら比べる前提が無いので落とす。
  - 比べるのは本体だけでなく本体・`-wal`・`-journal` の SHA-256（`file_state`）。前で `-wal` が空なことも確かめる（以後の暇なときの
    チェックポイントには写す頁が無い）。行数の比較も別の assert にした（どちらで落ちたかが出る）。
- 空の WAL の上の空コミット + PASSIVE が本体・`-wal` のバイト列を変えないことは、単体の SQLite の試しで確かめた（`truncate (0,0,0)`
  → 空の `BEGIN IMMEDIATE; COMMIT` → `passive (0,0,0)` → `mode=ro` で読む → 前後のハッシュが一致）。

### 確認

- 上の再現プラグインの下で、直した試験は 3 回とも通る（F-PY・F-TS とも）。直す前の試験は同じ条件で F-PY が 3 回とも落ちる。
- `tests/contract_fixtures` 全体（`9d6ef2e` + この直し）を 3 本: 単独 1 本 `45 passed, 6 skipped`（620 秒）、全コアを回す CPU の負荷
  （900 秒）の下で 2 本同時 `45 passed, 6 skipped`（920 秒）・`45 passed, 6 skipped`（959 秒）。T-20 の試験は 3 本とも F-PY・F-TS で通った。
- `uv run ruff check` は通る。
