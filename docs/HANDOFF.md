# 引き継ぎ — 現在地と次の作業

この文書は、セッションが中断した（トークンクォータ等）ときに、**別の PC・別のエージェントが続きを拾うため**の現在地。
作業が進んだらこの文書を更新してコミットする（履歴は git に残る）。

最終更新: 2026-10-31（夜〜朝。師匠は就寝中で「おまかせ」） / 対象: `main` / このセッションで BUG-0062〜0089 を処理し、exe 配布を入れた（§2・§6）

---

## 0. まず流すもの（3 分）

```powershell
git pull
uv run pytest -q                     # 期待: 全 green（0 failed。2026-10-31 の統合で 3925 passed / 29 skipped。1 件の揺れは BUG-0089 で直した）。新しい .venv でもこれだけでよい（BUG-0062 で手順を不要にした）
uv run pytest tests/contract_fixtures/test_fx_db_T19_T20.py -q   # 期待: 6 passed（旧 BUG-0061 の確認）
```

- 手で `uv sync --extra langs` と `fetch_fixtures.py` を流す必要は**もう無い**（BUG-0062）。
  - langs の文法は dev 依存組に入れた（`pyproject.toml` の `[dependency-groups].dev` に `lucianke[langs]`）。`uv run` が既定で同期する。
  - F-PY(httpx)・F-TS(ky) は、`tests/contract_fixtures/` を走らせると conftest がセッションに 1 回 `bench/fetch_fixtures.py` で取得する。**取得に失敗したら赤（エラー）**になる（黙って skip しない）。初回は git と GitHub への通信が要る。
  - オフラインで走らせるときだけ `LUCIANKE_NO_FIXTURE_FETCH=1`。未取得分の契約テストは「NOT CHECKED」と明記した skip になる（**skip は pass ではない**。この状態で green と言わない）。
- **新しく赤が出たら、それは引き継いだ変更のせい**なので先に直す（今は全 green が基準）。
- `uv run ruff check src tests` は clean。`ruff format --check` は 56 ファイルが未整形になるが、**これはインストール済み ruff とリポジトリの版のずれ**。全体整形はしない（差分が膨らむだけ）。触ったファイルだけ手で揃える。

## 1. 状態

| 項目 | 値 |
|---|---|
| ブランチ | `main`（`origin/main` と同期） |
| スプリント | `m1-impl-20261029`（`.pair-agent/sprints/m1-impl-20261029.json`）。M1 実装 第 1 ラウンド |
| 全体テスト | 統合（`9f4ffa3`、直す役 4 つが同時に pytest を流す高負荷・37 分）で `1 failed, 3925 passed, 29 skipped`。落ちた 1 件は実時間 1 秒の試験の揺れで、BUG-0089（`84d1b16`）で直した。skip 29 件は環境由来（TS の LSP なし・POSIX のみ・symlink 権限・ctags なし・notices 検査の lxml） |
| 索引 DB のスキーマ | **v7**（v5 `_name_calls` = BUG-0071、v6 `_member_refs` = BUG-0075、v7 `_member_refs.via_super` = BUG-0081）。古い索引は移行で Python/TS/JS を再取り込みする |
| 配布 | exe（PyInstaller onedir）。`.uild.ps1`（§6） |

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

## 3. 次にやること（優先順）

1. 作業中だった票（このセッションの終わりに直す役が走っていたもの。push 済みかは `git log --oneline --grep=BUG-00xx` で見る）:
   BUG-0080（F-PY の exact の事後条件が降格時に誤った理由で skip）・BUG-0086（F-PY T-06 事後条件 3 の揺れ）・BUG-0085（型の分からない素の名前の受け手を同じファイルの唯一の同名メソッドに確定で結ぶ誤り）・BUG-0087（索引の更新に失敗しても watcher に expect を登録して索引が古いまま）・BUG-0088（並行起動で `test_edit_transaction.py` が一斉にエラー）
2. **BUG-0084** — データファイル（JSON など）の骨格の縮退が深さ単位で粗い（ky `package.json` で予算の 66%）。影響は小さいので後回しにした
3. **BUG-0058**（レイテンシ）— 師匠判断（2026-10-30）で優先度を下げた。exact 経路の初回が 10 秒の予算を超えて structural に落ちることがある（BUG-0074・0080 の票にも実測）
4. リリースのワークフロー（`.github/workflows/release.yml`）は未実行。CI（GitHub Actions）の結果もこの PC から確かめていない（`gh` が無い）
5. 票に状態欄は無い。処理済みかは `git log --oneline --grep=BUG-00xx` で確認する

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
  動いている exe があると exit 1 で止まり、`-Force` で止めて置き換え、`-Uninstall` で消える。リリースのワークフローはまだ一度も走らせていない（初回のタグで確かめる）
