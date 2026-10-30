# 引き継ぎ — 現在地と次の作業

この文書は、セッションが中断した（トークンクォータ等）ときに、**別の PC・別のエージェントが続きを拾うため**の現在地。
作業が進んだらこの文書を更新してコミットする（履歴は git に残る）。

最終更新: 2026-10-31 / 対象: `main`（この文書を含むコミット） / 直前のコミット: `87d9451`。このコミットで BUG-0063（`max_tokens` の固定下限 200 の廃止）・BUG-0064（前置きだけで予算超過のとき必要な `max_tokens` を名指し）を解消

---

## 0. まず流すもの（3 分）

```powershell
git pull
uv run pytest -q                     # 期待: BUG-0065 の 7 件以外 green（§3）。新しい .venv でもこれだけでよい（BUG-0062 で手順を不要にした）
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
| 全体テスト | `3783 passed, 32 skipped, 0 failed`（2026-10-31・645 秒・BUG-0065 の直しのあと）。skip は環境由来（TS の LSP なし・POSIX のみ など）と較正の 3 件（BUG-0068）。新 .venv でも `uv run pytest -q` だけで届く（BUG-0062 で langs を dev 組に・フィクスチャを自動取得） |
| 索引 DB のスキーマ | v4（`_exact_marks`、`index/exact_marks.py`、ADR-16） |

## 2. このコミットで入ったもの

テーマは「`max_tokens` の下限を固定値から呼び出しごとの実測値へ」（師匠の判断 2026-10-30）。

### (a) 固定下限 200 の廃止（BUG-0063）

前置き（ヘッダ・accuracy_warning・`head_kv`・json の封筒）だけで 200 を超える応答があり、仕様 §5.2（最小 200）と §5.7（`used_tokens_est <= max_tokens`）が衝突していた。

- `core/limits.py`: `MIN_MAX_TOKENS` を廃止。`REGISTRY_MIN_DEFAULT_MAX_TOKENS = 1`（`ToolSpec.default_max_tokens` の登録時の床。作者のミス検出用で公開下限ではない）と、テスト用の `BUDGET_FLOOR_FOR_PAGING = 200`
- `tools/_base.py`: `max_tokens: int | None = Field(default=None, gt=0)`。非正だけ `E_INVALID_ARGUMENT`
- `mcp/pipeline.py`: 200 未満を弾く検査を削除。`mcp/registry.py`: `tools/list` のスキーマは `exclusiveMinimum: 0`
- 仕様書 §5.2 の表・§5.6-4、設計書の検査一覧を更新

### (b) 前置きだけで超過したときに必要な `max_tokens` を名指しする（BUG-0064）

レコードが元から 0 件で前置きが予算を超えると、`warnings` が空で「いくつに上げればよいか」が伝わらなかった。

- `mcp/budget.py` の `Packer.pack`（`start >= n` の経路）: `reserved_tokens > max_tokens` なら
  `the response preamble alone needs ~N tokens, over max_tokens=M; raise max_tokens to ~N or more ...` を `warnings` に入れる
  （`_fit` の `m == 0` の警告と同じ形）。dot・mermaid もこの経路を通る
- 実測（`tests/fixtures/poly`、`bench/_budget_probe2.py`）: T-07 json は max_tokens=200 で警告 `~365`、365 で呼び直すと used=365・警告なし。T-06 json は `~384` → 384 で used=384・警告なし

### (c) その他

- `mcp/render/graph.py`: ruff の 2 件（`Final` の import 漏れ・未使用変数）を修正。`ruff check src tests` clean
- `tests/contract/test_unsupported_tools.py`: スキーマの `minimum == 200` を `exclusiveMinimum == 0` に
- `bench/_budget_probe2.py`: T-06 の引数を `target` に直し（`urn` は存在しない引数で E_INVALID_ARGUMENT だった）、名指しした値で呼び直して収まることを確かめる検査を追加
- 前セッションの一時スクリプト `tools/_restore_default_max_tokens.py` は、HEAD と同じ値に戻すだけ（実質は CRLF 化だけ）だったので捨てた

## 3. 次にやること（優先順）

1. ~~**BUG-0065**（T-02 skeleton 系の 7 件が落ちる。`KeyError: 'children'` など）~~ — **解消済み**（T-02 の json の記号で null・空のキーを省かない形に戻した。仕様 §6.2。json で省くかは師匠の判断〈推奨: 省かない〉。続きの票は BUG-0066〈T-06・T-07 も省いている〉・BUG-0067〈max_tokens を上げると返るファイル数が減る〉）
2. ~~**BUG-0062**（新 .venv で langs extra・フィクスチャを手で入れないと全体テストに届かない）~~ — **解消済み**（langs を dev 組に・契約テストのフィクスチャを自動取得し、取れなければ赤。§0 は `uv run pytest -q` だけ）
3. **BUG-0058**（レイテンシ）— 師匠判断（2026-10-30）で優先度を下げた。再開は「毎回別シンボル＋exact 経路強制」で計時ログを拾うところから（票の末尾）
4. 票に状態欄は無い。処理済みかは `git log --oneline --grep=BUG-00xx` で確認する（2026-10-31 時点で BUG-0041〜0065 はすべて対応コミットあり（BUG-0066〜0068 は票のみ）。BUG-0058 は実測の記録のみ）

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
- exe では `sys.executable` が Python ではない。`doctor` の自己診断は隠しサブコマンド `_doctor-selftest`（`cli/selftest.py`）で exe 自身を起動する
- リリース: `v*` のタグを push すると `.github/workflows/release.yml` が Windows でビルド・smoke・Release に zip を上げる（python.org の Python に固定して、その LICENSE.txt を同梱）
- 確かめたこと（2026-10-31、このPC）: `build.ps1 -Zip -InstallDir <一時>` が smoke 13 項目すべて ok、zip は 394 項目・区切りは `/`・展開した exe が動く。
  動いている exe があると exit 1 で止まり、`-Force` で止めて置き換え、`-Uninstall` で消える。リリースのワークフローはまだ一度も走らせていない（初回のタグで確かめる）
