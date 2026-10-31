# 引き継ぎ — 現在地と次の作業

この文書は、セッションが中断した（トークンクォータ等）ときに、**別の PC・別のエージェントが続きを拾うため**の現在地。
作業が進んだらこの文書を更新してコミットする（履歴は git に残る）。

最終更新: 2026-10-31 19 時台 / 対象: `main` / このセッションで BUG-0062〜0135 を処理し、exe 配布・能力測定の基準（Mnemo）・競合比較（Serena）・試しのリリース（`v0.0.1-rc1`）を入れた（§2・§6・§7）

---

## 0. まず流すもの（3 分）

```powershell
git pull
uv run pytest -q                     # 期待: 全 green（0 failed。2026-10-31 の `d3c676e` で 4402 passed / 30 skipped）。新しい .venv でもこれだけでよい（BUG-0062 で手順を不要にした）
uv run pytest tests/contract_fixtures/test_fx_db_T19_T20.py -q   # 期待: 6 passed（旧 BUG-0061 の確認）
```

- 手で `uv sync --extra langs` と `fetch_fixtures.py` を流す必要は**もう無い**（BUG-0062）。
  - langs の文法は dev 依存組に入れた（`pyproject.toml` の `[dependency-groups].dev` に `lucianke[langs]`）。`uv run` が既定で同期する。
  - F-PY(httpx)・F-TS(ky) は、`tests/contract_fixtures/` を走らせると conftest がセッションに 1 回 `bench/fetch_fixtures.py` で取得する。**取得に失敗したら赤（エラー）**になる（黙って skip しない）。初回は git と GitHub への通信が要る。
  - オフラインで走らせるときだけ `LUCIANKE_NO_FIXTURE_FETCH=1`。未取得分の契約テストは「NOT CHECKED」と明記した skip になる（**skip は pass ではない**。この状態で green と言わない）。
- **新しく赤が出たら、それは引き継いだ変更のせい**なので先に直す（今は全 green が基準）。
- lint は CI と同じ引数なしの `uv run ruff check`（clean）。**`ruff format` は使わない**（行末の日本語コメントなどを動かす。`ruff format --check` は 113 ファイルが未整形と出るが、それが正）。全体整形はしない（差分が膨らむだけ）。触ったファイルだけ手で揃える（BUG-0130）。

## 1. 状態

| 項目 | 値 |
|---|---|
| ブランチ | `main`（`origin/main` と同期） |
| スプリント | `m1-impl-20261029`（`.pair-agent/sprints/m1-impl-20261029.json`）。M1 実装 第 1 ラウンド |
| 全体テスト | `4402 passed, 30 skipped, 0 failed`（2026-10-31・`d3c676e` に固定した作業ツリー・13 分 47 秒。`13535ff` も同じ。その前の `a3df079` も 4334 passed・0 failed）。skip 30 件は環境由来（TS の LSP なし・POSIX のみ・symlink 権限・ctags なし・notices 検査の lxml）。**長い全体テストは固定の作業ツリー（`.claude/worktrees/` の下・パスが短い所）で流す**（実行中に同じ作業ツリーで pull すると偽の赤が出る） |
| 索引 DB のスキーマ | **v15**（v5 `_name_calls`=BUG-0071、v6 `_member_refs`=BUG-0075、v7 `via_super`=BUG-0081、v8=BUG-0085、v9 `_attr_defs`・`_var_types`=BUG-0093〜0095、v10 `_reexports`=BUG-0100、v11=BUG-0104/0105、v12 `_type_aliases`=BUG-0108/0109、v13=BUG-0113/0114、v14=BUG-0117、v15=BUG-0122）。古い索引は移行で再取り込みする |
| 配布 | exe（PyInstaller onedir）。`.\build.ps1`（§6） |

## 2. 2026-10-31 に入ったもの（要約。詳細は各票の「原因・直し方・確認」）

師匠の宣言（2026-10-31）: **Lucianke の主な利用者は呼ぶ側の AI。仕様の判断は AI にとっての有用さで AI が決めてよい**（ペア固有 Skill `the-tools-primary-user-decides-its-spec`）。以下はこの基準で決めて、仕様書も同じコミットで直した。

- 予算・出力
  - BUG-0063/0064（`b1ce8ac`）: `max_tokens` の固定下限 200 を廃止。前置きだけで超えるときは必要な `max_tokens` を warnings で名指し
  - BUG-0067（`afc06fc`）: パッカーを予算に対して単調に（`max_tokens` を上げて件数・内容が減らない。縮退したレコードでページを閉じる。仕様 §5.6-5）
  - BUG-0078（`9f4ffa3`）: T-02 の縮退を 1 件ずつの段に（F-PY の json・6000 で 618 → 5,967 トークン。省いた量と丸ごとに要る `max_tokens` を書く）
  - BUG-0069（`a747105`）: compact で導ける `urn:` 行を出さない（`lk://<ws>/<path>#<修飾名>`）・imports をモジュールごとに・検索は名前の一致を先に・シグネチャ 160 文字（骨格 −37%、検索 −49%）
  - BUG-0065（`e021064`）・BUG-0066（`379d70c`）: json の記号は仕様 §6.2 の形（null・空のキーも出す）。json は形の安定、節約は compact
- 参照・呼び出し階層（構文木の層）
  - BUG-0071（`d5e8614`）: 型の分からない受け手の呼び出しを黙って落とさない。名前のメソッドが 1 つなら `conf=heuristic`、複数・組み込み名なら件数と位置を warnings と `data.unattributed_calls` に
  - BUG-0077（`c7f2b7e`）: 名前の一致の候補を呼び出し箇所に近い段（同じファイル → 呼び出しを含む fixture → 全体）から選ぶ（誤帰属 511 件を除去）
  - BUG-0075（`8a72e31`）: 基底クラスが別ファイルでも継承メソッドの呼び出し（`self.helper()`・`s: Sub`）を解決（C3 の MRO、`core/mro.py`、`index/inherited.py`）
  - BUG-0079/0081（`8c10a2a`）: 属性の連鎖（`a.b.m()`）を `type(a)` に結び付けない。`super().m()` を MRO の次のクラスへ
  - BUG-0082（`f5cf85b`）: Python の絶対 import を pytest が `sys.path` に載せる根（テストの隣・conftest・`pythonpath`）と fixture の根でも解決
  - BUG-0070（`94689c7`）: 初回索引の途中でも、未索引のファイルの対象をその場で索引して答える（not found にしない）。索引中の応答に進みの警告
- 試験・検証の穴
  - BUG-0062（`9d96cd2`）: 新 .venv でも `uv run pytest -q` だけで全体に届く（langs を dev 組、フィクスチャ自動取得・失敗は赤）
  - BUG-0068（`b034d64`）: 較正の標本を `tests/fixtures/tokens/` に凍結（skip をなくし、古くなったら赤）
  - BUG-0074（`4ca4684`）・BUG-0076（`0fee57d`）・BUG-0083（`bf74fb5`）・BUG-0089（`84d1b16`）: 負荷で揺れていた試験を決定的に（スレッドの漏れの見張り `tests/conftest.py`、条件待ち、watcher の `expect` の製品の直し）
  - BUG-0072/0073（`8536a5f`）: 依存方向の見張り・CI と同じ引数なしの `ruff check`
- その他: `.mcp.json` の `--directory` を「.」に（別 PC の絶対パスで MCP がつながらなかった。`cf4497c`）、exe 配布（§6）

### 2026-10-31 夕方に入ったもの（追記。BUG-0124〜0135）

- 速さ
  - BUG-0127（`e90ec54`・`ef5eb80`・`417ce34`）: 初回の索引の解析を spawn の子プロセスに並べる（`engine/analysis_pool.py`。`LUCIANKE_INDEX_WORKERS`、既定は論理 CPU の半分・最大 8・64 ファイル超の走査だけ）・書き手のページキャッシュ 128 MiB・取り込みの文をまとめ書き・200 ファイル以上なら型サーバーを走査と並べて起こす。Mnemo の準備 243 秒 → 62 秒（§7）。exe でも動く（`tools/smoke_exe.py` の 6 段目で確かめる。`8753573`）
  - BUG-0058（`7e66c64`〜`0965fec`）: T-06/T-07 は対象ごとに最初の診断を待たない・T-08 は届かない診断と同じ QuickFix を待たない。**仕様書 OD-23 を決定 (b)**（`3c015c9`・T-11 の dry_run も `d3c676e`。T-06・T-07・T-08・T-11 は冷 p95 < 1 秒〈深さ 2 以上の T-07 は < 2 秒〉と温 p95 < 50 ms に分けて判定。冷えた LSP の exact を 50 ms に入れるのは全部を先に計算するしかなく、ベンチの数字を作るだけになるため。`bench/run_latency.py` も冷・温で判定）
  - BUG-0128（`7925923`〜`4eb054b`）: T-19 の表ごとのスレッドをやめる・T-00 の Git の状態を覚える・T-01 は要る列だけ読む・tags.scm を 1 度だけ読む・T-11/T-10 の解析の覚え。BUG-0131（`90b20b4`・`48c4547`）: data_version はメモリの値（25 ms ごとに `PRAGMA data_version` で他プロセスの書き込みを検知）・編集の lsp_status は起動コマンドを探さない
  - BUG-0125（`b42429a`）: T-20 の `timeout_ms` は既定を索引の大きさで決める（1 MB あたり 60 ms・300 ms〜10 秒）・上限 30 秒・公開接続のページキャッシュ 64 MiB・打ち切りに `suggested_timeout_ms` などの手がかり。実物の Mnemo（250 MB）で集計 56〜225 ms
- LSP の正しさ
  - BUG-0132（`50b917d`・`776b4fe`・`5c09fc0`）: ワークスペースの根に pyright の設定が無いと basedpyright が親の設定を採ってワークスペースを外していた → 根の `pyproject.toml` を `configFilePath` で明示。利用者の `include` の外のファイルは聞く前に開かせる（`lsp/pyright_scope.py`）。BUG-0129（`3f5a14b`・`c51f7bb`）: F-POLY に `pyrightconfig.json`・`__init__` の incoming はクラスの項目に聞く
  - BUG-0135（`3f8eba2`・`23c63a8`・`3ac4488`）: 根の設定の変更で解析の対象と明示を読み直し、変われば `didChangeConfiguration`。`.gitignore` の対象の設定ファイルも LSP に知らせる
  - BUG-0133（`ee7586d`・`d45d42d`）: T-07 の exact でモジュール直下の呼び元を捨てない・TS の incoming は references で別のプロジェクト（test/）の呼び出しを足す。BUG-0134（`d45d42d`）: T-06 は references を待つ間に分類を進める
- 出力・その他
  - BUG-0124（`c0cd165`）: 地図は祖先が非公開のメンバーを公開に数えない。BUG-0084（`d6c9c89`・`b1304d7`）: データファイルの骨格の縮退を 1 件ずつに（予算の 99% を使う）。BUG-0096（`31051ba`）: 監視が polling のとき要求の対象を sha256 で照合し、T-00 に `watcher` を出す
  - BUG-0126: `.mcp.json` の失敗は別 PC の絶対パスだった頃のもの（`cf4497c` で直っていた）。README に「clone 直後は先に `uv sync`」（`23def4a`）
  - BUG-0130: `ruff format` は使わない（親の指示の誤り）。見張り `tests/invariants/test_unique_test_module_names.py`（試験ファイルの名前の重なりで全体の収集が落ちる。`31232d9`）
  - リリース: `release.yml` は `v<版>-<接尾辞>` のタグを prerelease にする（`896c3a8`）。**`v0.0.1-rc1` で走らせて成功**（§6）

### 2026-10-31 午前〜午後に入ったもの（追記）

- 師匠のコーディングエージェント（ray）からのフィードバック: 全ツールに `openWorldHint: false`（read 系が純 read になる。`998e2ea`）
- CI は手動の起動だけ（師匠の判断「そもそもCIしなくていいよ」。`7dd9287`。検証は手元の全体テスト）
- 大きなリポジトリ: BUG-0112（索引中も呼び出しを待たせない `core/foreground.py`・長い行の位置の変換・入れ子の作業ツリーと圧縮 JS を飛ばす。`b124359`・`d733669`・`2d2f0a9`）、BUG-0111（`venv/` を見つける）、`.claude/worktrees/` を `.gitignore`
- 予算: BUG-0110（compact で全体より少し小さい予算だと 0 件だった）、BUG-0115/0116（カーソルを 10 文字・9 トークンに、前置きの見積もりの無駄）
- 参照の層（構文木）: BUG-0079/0081（属性の連鎖・super）、BUG-0082（pytest の import の根）、BUG-0085・0117（型の分からない受け手・Union を確定で結ばない）、BUG-0093〜0095（モジュール関数・import した変数・属性の読み書きを黙って落とさない）、BUG-0100/0101・0104/0105（再エクスポート・TS のバレル・star）、BUG-0107（exact の答えにも名前の一致の別欄）、BUG-0108/0109・0113/0114（TS の既定の import・型の別名・型リテラルのメンバー）、BUG-0122（インスタンス属性）
- LSP: BUG-0090（basedpyright は取り消された references の後に壊れる → 送らない・壊れたら再起動）、BUG-0091・0123（T-06/T-07 の exact は 1 問い合わせの予算・答えた分を保存・送り済みの要求を使い回す）
- 編集: BUG-0087（索引の更新に失敗したら watcher に expect しない）、BUG-0098/0099（編集の前の診断の基準が無いときは added にしない・待ち切れは E_TIMEOUT）
- 地図・検索: BUG-0120（地図の冒頭にパッケージの概観・地図 11.7 秒 → 1 秒）、BUG-0121（ドット無しの検索は名前で照合）
- 試験の揺れ: BUG-0074・0076・0080・0083・0086・0089・0092・0097・0103・0106

## 3. 次にやること（優先順）

1. **能力測定の基準（§7）で点数を見ながら使い勝手を上げる。** 2026-10-31 の `8b39591` で Lucianke 15/15 合格・平均 0.993・19,068 トークン・cold 624 ms / warm 24 ms・**準備 62.4 秒**（`cdc9257` は 243 秒。Serena 13/15・0.946・準備 8.3 秒、grep 7/15・0.782）
2. M1-3（応答時間。OD-23 (b) の冷・温の判定）: F-POLY は全部合格、F-PY も合格（`d3c676e`）。F-TS は T-06 の冷だけ不合格（14 件中 1 件、tsserver が開かせたファイルでプロジェクトを組み直す回の references そのもの 1.1〜2.5 秒。node 22 でも同程度。BUG-0134）。受入の機材の値で判断する。この PC には node が無く、TS の LSP はベンチで bun 経由
3. 票の「残ったこと」: BUG-0135（`extends` の先の設定の変更は読み直さない・TS の tsconfig の変更は実機で未確認・M2 の Tier 3 の編集で TOML/JSON を書くときは編集から `notify_watched` が要る）、BUG-0131（T-00・T-08 の `status` は名前のために毎回 `find_command`）
4. 正式なリリース `v0.0.1` は師匠の判断で（試しの `v0.0.1-rc1` は成功・§6）。CI は手動の起動だけ（3 OS の確認が要るときに Actions から）。Actions の結果は公開 API（`api.github.com/repos/ShinichiOkas/Lucianke/actions/runs`）で読める。ログの本文は `gh auth login` が要る
4. 他 OS: WSL の Ubuntu は外へのネットワークがつながらない（VPN）。手元で Linux を回すには WSL の設定（例 `networkingMode=mirrored`）が要る
5. 票に状態欄は無い。処理済みかは `git log --oneline --grep=BUG-0xxx` で確認する

## 4. 触るときの注意

- **`Node.text` を書くと番人テストが落ちる**。`source[node.start_byte : node.end_byte]` で切る。ノードでない `.text`（自前の型・`re.Match` など）は `test_syntax_skeleton_realfiles.py` の `_NOT_A_NODE` に**受け手がノードでないことを確かめてから**足す
- 関数ノードの集合は `imports.JS_FUNCTION_NODES` が唯一の定義。`relations.py` 側に写しを作らない（ずれると使用が落ちるか解決できなくなる）
- `tests/contract_fixtures/` は `bench/.cache/` の F-PY（httpx）・F-TS（ky）で本物の索引を組んで走る。無ければ conftest が自動で取得し、取得できなければ赤にする（BUG-0062）。`LUCIANKE_NO_FIXTURE_FETCH=1` のときだけ NOT CHECKED の skip（**skip は pass ではない**）。`bench/run_exit_criteria.py` は `network = "none"` を守るためこれを立てて走る
- テストが無条件に import する文法パッケージは dev 依存組（`lucianke[langs]`）に入れる。利用者向けの optional extra だけに置くと、新しい `.venv` で収集エラーになる（BUG-0062）
- `.pair-agent/current/` のポインタの鍵はホスト名 + リポジトリのパスで決まる。**別 PC では別のポインタになる**ので、現在地はこの文書と `.pair-agent/sprints/m1-impl-20261029.json` を読む
- 区切りごとにコミット・push（常に sync）。作業の前に `git pull`

## 5. この文書を書いた時点で確かめたこと

### 初版（`7aa42e2` 時点）
- `git stash` で `src/` とテストの未コミット変更を退避して HEAD（`7aa42e2`）のまま `tests/contract_fixtures/test_fx_db_T19_T20.py` を走らせ、**同じ 2 件が落ちる**ことを確認 → BUG-0061 は引き継いだ変更が原因ではない
- 全体テスト 1 回（544 秒）と、影響範囲のテスト 4 ファイル（89 件 green）
- `ruff check src tests` clean

### 追試（2026-10-29、別 PC・新 .venv・`6ade29e` まで）
- 新 .venv で `uv run pytest -q` が `tree_sitter_rust` の収集エラーで止まることを確認 → `uv sync --extra langs` で解消（BUG-0062）
- langs 同期後・フィクスチャ未取得だと `3734 passed / 71 skipped / 0 failed`。契約テスト単独は `6 skipped`（= 未検査）。`fetch_fixtures.py` で F-PY・F-TS を取得すると `2 failed, 4 passed`（BUG-0061 が見える）→ BUG-0062
- BUG-0061 を直す役が `default_max_tokens` を 2500→6000 に（`2a42a50`）。`tests/contract_fixtures/test_fx_db_T19_T20.py` が **6 passed** になることを自分の環境で確認。仕様 5.19（全 10 テーブルを 1 回で返す）と照らして妥当と判断

## 6. exe 配布（2026-10-31・師匠の判断「配布は exe 形式」）

- 入口は `.\build.ps1`（Windows PowerShell 5.1 で動く。UTF-8 BOM）。ビルド（`packaging\lucianke.spec`、PyInstaller 6.22.3 固定、onedir）→
  `tools\smoke_exe.py`（`--version`・`doctor`・`tools/list` がソースと同じ・`tests/fixtures/poly` で T-00/T-02/T-03/T-07・stdout の純度・
  stdin EOF で終了コード 0）→ インストール（`-InstallDir` → `AGENT_INSTALL_DIR` → `~/.local/bin`）。`-Zip` で `dist\lucianke-<版>-windows-<arch>.zip` と sha256。
- onefile にしない（起動 3.4〜4.2 秒。onedir は 0.7〜0.8 秒）。実行時の中身は `lucianke_internal\`（PATH の bin に直接置いても他の PyInstaller 製 exe とぶつからない）
- インストール先の exe が動いている（MCP のホストが使っている）と置き換えられない。`build.ps1` は止めずにエラーにし、`-Force` で止めて置き換える
- exe では `sys.executable` が Python ではない。`doctor` の自己診断は隠しサブコマンド `_doctor-selftest`（本体は `bootstrap_io.run_stdout_selftest`）で exe 自身を起動する
- リリース: `v*` のタグを push すると `.github/workflows/release.yml` が Windows でビルド・smoke・Release に zip を上げる（python.org の Python に固定して、その LICENSE.txt を同梱）
- 確かめたこと（2026-10-31、このPC）: `build.ps1 -Zip -InstallDir <一時>` が smoke 13 項目すべて ok、zip は 394 項目・区切りは `/`・展開した exe が動く。
  動いている exe があると exit 1 で止まり、`-Force` で止めて置き換え、`-Uninstall` で消える。
- **試しのリリース（2026-10-31、師匠の承認）**: `v0.0.1-rc1`（`896c3a8`）で `release.yml` が全段成功（1 分 40 秒）。Release（prerelease）に
  `lucianke-0.0.1-windows-x64.zip`（30.9 MB）と `.sha256`。落とした zip の sha256 一致・展開した exe で `smoke_exe.py` が PASSED（子プロセスの段を含む）。
  タグが `v<__version__>` なら正式な Release、`v<__version__>-<接尾辞>` なら prerelease

## 7. 能力測定の基準（Mnemo の分析）と競合比較（2026-10-31・師匠の判断）

- Lucianke の能力は、実プロジェクト Mnemo（`../Mnemo`、固定 `d419cf0b`）を分析させた結果で測る（Skill `mnemo-is-the-capability-benchmark`）。
- **置き場所**: Mnemo の中身（課題・正解・結果）は Mnemo の `bench/lucianke/`。Lucianke には汎用の実行器 `bench/capability/`（`run.py`・adapters: lucianke / serena / grep）だけ（Mnemo の中身を OSS に持ち込まない）。
- 走らせ方: `uv run python bench/capability/run.py --tasks <Mnemo>/bench/lucianke/tasks.toml --workspace <固定の写し> --engines lucianke --date <日付> --out <Mnemo の一時作業ツリー>/bench/lucianke/results --work-dir <dir>`。
  Mnemo の作業ツリーには他の作業の未コミットの変更があることが多いので、結果は `git -C <Mnemo> worktree add --detach <scratch> origin/main` の一時作業ツリーで commit・push する。
- 競合: Serena 2.0.0（師匠の承認で導入。MCP・pyright）。Serena は準備が速く誤検出が無いが、型の分からない受け手を落とし、outgoing と名前変更の dry-run が無い。
- Mnemo の `PROJECT_STRUCTURE.md` §10 に Lucianke による構造分析（規模・パッケージの依存・被参照の多いモジュール・気づいた点）を書いた。
- 結果: 1 回目 `bench/lucianke/results/2026-10-31.md`（Lucianke `cdc9257`・Serena・grep）、2 回目 `2026-10-31b.md`（Lucianke `8b39591` だけ。BUG-0124〜0132 の後。15/15・0.993・準備 62.4 秒）。
