# BUG-0070 初回の索引中、まだ索引していないファイルの対象が `E_TARGET_NOT_FOUND`「file is not in the index」になり、検索は 0 件になる

- 報告: 2026-10-31 / 見た人: 親（exe の試しと dogfood）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（2026-10-31、PyInstaller でビルドした exe、`tests/fixtures/poly` の写しを cwd にして stdio で起動）

`configure_workspace set_roots` の応答が `jobs: job_0001 reindex running 0%` のとき、直後の呼び出し:

```
--- search_workspace_symbols {"query": "process_charge"}  isError=False
query: "process_charge" match=fuzzy hits=0
--- query_call_hierarchy {"target": {"path": "src/shop/services/payment.py", "symbol_name": "PaymentProcessor.process_charge"}, ...}  isError=True
# query_call_hierarchy error E_TARGET_NOT_FOUND
! file is not in the index: src/shop/services/payment.py
--- find_references_and_implementations（同じ対象）  isError=True
# find_references_and_implementations error E_TARGET_NOT_FOUND
! file is not in the index: src/shop/services/payment.py
```

`get_repo_map` は `! ranking=alpha (graph not ready)` だけを返した。索引が終わってから同じ呼び出しをすると、検索は 1 件・T-07・T-06 は正しい答え。
Lucianke 自身のリポジトリでは、初回の索引に 84.5 秒かかった（dogfood）。

## 利用者（呼ぶ側の AI）として困ること

- ディスクにあるファイルを「無い」と言われる。呼ぶ側は対象の指定を誤ったと判断して、別の探し方へ移る（索引待ちだと分からない）。
- 検索の 0 件も「存在しない」と区別がつかない（索引の進み具合が応答に無い）。

## 原因・直し方・確認（2026-10-31）

### 原因

- 対象の解決（`tools/_nav.py` の `resolve_target`〈T-06・T-07〉、T-04 の `_resolve`）は索引の `files` 表だけを見て、行が無ければ `E_TARGET_NOT_FOUND`「file is not in the index」を返していた。起動時の差分索引（`Indexer.scan`。背景ジョブ）はファイルをパス順に取り込むので、走査がまだ届いていないファイルは、ディスクにあっても「無い」になった。T-04 は import をたどる先の判定（`exists`）も索引だけで見ていたので、たどった先がまだ索引に無いと「定義が無い」になった。
- 検索・リポジトリマップ・参照は索引にある分だけを返し、走査の途中かどうかを応答に載せていなかった（`Indexer` はジョブの百分率しか持たず、ツールからは見えなかった）。

### 直し方（2026-10-31 の方針: 呼ぶ AI への有用性で決める）

- **先取り**（`Indexer.ingest_ahead`、`src/lucianke/engine/indexer.py`）: 走査の途中で、ツールの対象のファイルがまだ索引に無ければ、解決の前にその 1 ファイルだけをその場で解析して取り込む（数 ms）。対象外（`.gitignore`・`exclude_globs`）・ディスクに無い・ファイルでないものは取り込まない（従来どおり `E_TARGET_NOT_FOUND`。それは正しい答え）。
  - 先取りは `ingest_file` だけで、関係の整え（`_settle_relations`）・import 元の解き直し・順位の予約はしない。整えると、まだ取り込まれていない定義元を指す行が消える（走査は最後に 1 回だけ整える作り）。
  - 走査は始めに読んだ `files` に無かったファイルを必ず取り込み直して最後に整える。列挙の途中（`files` を読む前）の先取りは読み取りから外す（空の索引からの走査を初回のまま保ち、全ファイルへの差分の解き直し `after_scan` を走らせない）。走査が取り込み直さなかった分（列挙の後に作られた等）は、走査の終わりに新しいファイルとして取り込み直す（`Indexer._settle_ahead`。関係を整え、import 元も解き直す）。走査が無いときの `ingest_ahead` は `index_file` と同じ。
- **呼ぶ場所**（共通の部品は新しい `src/lucianke/tools/_building.py`）: T-06・T-07（`ensure_target_indexed`。`urn` ならその URN のファイル）、T-04（項目ごと。import をたどった先のファイルも先取りし、走査の途中だけ import の先をディスクでも確かめる）、T-08（`paths` の各ファイル）。先取りに失敗したら `E_TIMEOUT`（`details.reason: "index_building"`、`retry_after_ms: 1000`、`path`、`files_done` / `files_total`。BUG-0046 の `index_opening` と同じ形）。`E_TARGET_NOT_FOUND` にはしない。
- **進みの警告**: `Indexer.building()` が走査の途中の進み（`ScanProgress`: 見たファイル数 / 全体。列挙の前は全体が `None`、投入した直後〈queued〉も含む）を返し、`WiredServices.index_building` で出す。索引を読むツール（新しい `ToolSpec.reads_index`: T-01・T-03・T-04・T-06・T-07・T-08・T-20）の応答に、パイプライン（`mcp/pipeline.py`）が `index building: 412/1300 files (31%); results may be incomplete - call again when done`（列挙の前は `listing files`）を添える。百分率は走査が終わるまで 99 で止める（全ファイルを見た後も関係の整えと順位の計算が残る）。構文木から答えるツール（T-02・T-05）には付けない。走査を終えた後は付けない（従来どおり）。
- **仕様書**: §4.3.1 に索引の途中の応答と進みの警告、§5.3 に規則 6（索引の途中の対象は `E_TARGET_NOT_FOUND` にしない・その場で索引する・できなければ `E_TIMEOUT` の `index_building`）、§5.8 の `E_TIMEOUT` に `index_opening` / `index_building` の再試行の指針を、理由つきで書いた。

### 確認

- **再現（直す前）**: Lucianke 自身の作業ツリー（530 ファイル、初回の索引は数十秒）を cwd に実プロセスを stdio で起こし、空のキャッシュで `set_roots` の直後に呼んだ（`jobs: job_0001 reindex running 0%`）。

```
--- search_workspace_symbols
query: "process_charge" match=fuzzy hits=0
--- query_call_hierarchy isError=True
# query_call_hierarchy error E_TARGET_NOT_FOUND
! file is not in the index: tests/fixtures/poly/src/shop/services/payment.py
--- find_references_and_implementations isError=True
# find_references_and_implementations error E_TARGET_NOT_FOUND
! file is not in the index: tests/fixtures/poly/src/shop/services/payment.py
--- get_repo_map
! ranking=alpha (graph not ready)
```

- **直した後**（同じ手順）:

```
--- search_workspace_symbols isError=False
! index building: listing files; results may be incomplete - call again when done
query: "process_charge" match=fuzzy hits=0
--- query_call_hierarchy isError=False
! index building: 1/530 files (0%); results may be incomplete - call again when done
root: [Method] lk://.../tests/fixtures/poly/src/shop/services/payment.py#PaymentProcessor.process_charge direction=incoming depth=1
--- find_references_and_implementations isError=False
! index building: 5/530 files (0%); results may be incomplete - call again when done
target: [Method] lk://.../tests/fixtures/poly/src/shop/services/payment.py#PaymentProcessor.process_charge
references: 0 files, 0 sites (tests: 0)
--- get_repo_map isError=False
! index building: 7/530 files (1%); results may be incomplete - call again when done
! ranking=alpha (graph not ready)
```

- **決定的なテスト**（時間に頼らず、門で走査を止める。`_walk` を `threading.Event`、2 つ目のバッチの書き込みを `asyncio.Event` で止める）:
  - `tests/unit/test_engine_indexer_building.py`（9 件）: 進み（走査の前後は `None`、列挙の途中は `ScanProgress(0, None)`、走査の途中は `0 < 済み < 全体`、百分率は 99 で止まる）。先取りの後の走査の索引（`files` / `symbols` / `relations`。目標のファイルまで）が、先取りしない普通の走査と一致する（列挙の途中・走査の途中・列挙の後に作られたファイルの 3 通り）。走査の途中の先取りは、まだ索引に無い定義元への行を消さない。対象外・ディスクに無い・ディレクトリは `skipped`。走査が無いときは `index_file` と同じ。
  - `tests/unit/test_tools_index_building.py`（2 件）: 本番と同じ結線（`RuntimeHub` + `WiredServices` + `Pipeline` + 全ツール）で、走査を列挙で止めたまま T-03（0 件 + 警告）・T-07・T-06・T-04（import をたどった先も）・T-01 が成功し警告が付く。ディスクに無いファイルは `E_TARGET_NOT_FOUND`、T-02 には警告なし。先取りが失敗すると T-07 は `E_TIMEOUT`（`index_building`、`retry_after_ms=1000`）、T-04 は項目単位の失敗。走査を終えると警告は消え、呼び出し元（`create_order`・`retry_order`）が揃う。
  - 変異で効きを確かめた: 先取りを無効にすると T-07・T-04 が `E_TARGET_NOT_FOUND` に戻って落ちる。列挙の途中の先取りを `files` の読み取りから外さないと、初回の走査が差分扱いになって落ちる。走査の終わりの取り込み直しを外すと、列挙の後に作られたファイルの関係が整わずに落ちる。
- `uv run ruff check src tests` green。全体テスト: 基 08b946e の上で 3,795 passed・2 failed・32 skipped（1,401 秒）。落ちた 2 件はこの変更と無関係: `test_import_graph.py`（BUG-0072。origin/main の 8536a5f で直っている）と、T-07 の契約テスト（F-PY）の `when` の検査（単独では通る揺れるテスト。票 BUG-0074 に書いた。契約の土台は走査の完了を待ってから呼ぶので、この変更の経路は通らない）。
