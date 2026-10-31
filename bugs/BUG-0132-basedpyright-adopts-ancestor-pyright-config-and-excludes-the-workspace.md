# BUG-0132 ワークスペースの根に basedpyright の設定が無いと、親のディレクトリの設定を採り、ワークスペースのファイルを解析の対象から外す（exact の答えが空になる）

見たことだけを書く（Lucianke の側の対処は調べていない）。2026-10-31、BUG-0129 の直す役が見た。

## 症状
- F-POLY（`tests/fixtures/poly`。`pyproject.toml` はあるが `[tool.pyright]` / `[tool.basedpyright]` が無く、`pyrightconfig.json` も無かった）を
  ワークスペースにして basedpyright 1.40.2 を起動すると、basedpyright は親のディレクトリの設定を採った。Lucianke のリポジトリの根に
  `pyrightconfig.json`（`include: ["src", "tests"]`・`exclude` に `tests/fixtures`）と `pyproject.toml` の `[tool.basedpyright]`
  （`include = ["src"]`）がある。
- その状態で、F-POLY の `src/shop/services/limits.py` の `LimitTable.get`（`src/shop/services/payment.py` の `_validate_limit` から呼ばれる）の
  `callHierarchy/incomingCalls` を basedpyright に直接送ると（Lucianke を通さない。`prepareCallHierarchy` は 3 ms）:

| 条件 | incomingCalls の所要 | 答え |
|---|---|---|
| リポジトリの中の F-POLY（設定の根は Lucianke のリポジトリ） | 120 秒で返らず | — |
| 同上、リポジトリの `pyrightconfig.json` だけ一時的に退けた（`[tool.basedpyright]` が残る） | 38.6 秒 | `null`（呼び出し元なし） |
| F-POLY をリポジトリの外にコピー | 0.33 秒 | `_validate_limit`（payment.py） |
| リポジトリの中の F-POLY に `pyrightconfig.json` を置いた（BUG-0129 の直し） | 0.24〜0.95 秒 | `_validate_limit`（payment.py） |

- basedpyright のコード（`dist/pyright-langserver.js` の設定の探索）に「pyproject.toml が見つかっても `[tool.pyright]` の節が無ければ、
  その pyproject.toml が無いものとして親のディレクトリを探す」という注がある。
- Lucianke はこの答えを `confidence_level: exact` で返す（F-POLY の T-07 は 10 秒の予算で切れていたので、`null` の答えが exact で
  返った呼び出しは見ていない）。

## 再現
1. 根に `pyrightconfig.json`（`include` が自分の下のディレクトリだけ）があるディレクトリの下に、`[tool.pyright]` の無い `pyproject.toml` を持つ
   小さな Python のプロジェクトを置く。
2. そのプロジェクトをワークスペースにして Lucianke の T-06 / T-07 を呼ぶ（または basedpyright に直接 `callHierarchy/incomingCalls`）。

## 影響しうる使い方
ワークスペースが、pyright の設定を持つ親（モノレポの根・ホームディレクトリの `pyrightconfig.json` など）の下にあるとき。

## 仕様書
§12.3（LSP）、§11 T-06・T-07。関連: BUG-0129（F-POLY だけはフィクスチャに設定を置いて直した）。

## 原因・直し方・確認（2026-10-31）

### 原因

- basedpyright 1.40.2（pyright 1.1.414）は言語サーバーとして起動すると、設定をワークスペースの根の `pyrightconfig.json` → 根の
  `pyproject.toml` の順に探す（根より上は探さない）。ところが `_getExtendedConfigurations` は、根の `pyproject.toml` に
  `[tool.basedpyright]` / `[tool.pyright]` が無い（または読めない）と「その `pyproject.toml` が無いものとして」親のディレクトリの
  `pyrightconfig.json` / `pyproject.toml` を探して採る。設定ファイルを明示（`analysis.configFilePath`）したときはこの探索をしない。
  Lucianke は明示していなかったので、F-POLY は Lucianke のリポジトリの `pyrightconfig.json`（`include: src・tests`、`exclude: tests/fixtures`）
  を採り、F-POLY のファイルが解析の対象から外れた（開いた文書だけ）。`incomingCalls` は Lucianke 自身の `src`・`tests` を走査して返らず、
  T-07 は 1 問い合わせの予算 10 秒で切れていた。
- 票の外だが同じ根: 利用者自身の設定が対象を絞る（`include: ["src"]` など）と、basedpyright は対象の外のファイル（`tests/`・`scripts/`）を
  開かれるまで探さない。T-06・T-07 は対象の外の呼び出しを落としたまま `exact` で返していた（`include: ["src"]` の F-POLY の
  `process_charge` は routes.py の 2 箇所だけ。`tests/test_payment.py` の `test_charge_ok` が黙って消えた）。開いた文書は対象の外でも探す
  （実機で確認）。

### 直し方（決めたこと）

- **設定の根を明示する**（`lsp/servers.pinned_pyright_config`・`build_settings(config_file=...)`・`LspPool._run_once`）: 根に
  `pyrightconfig.json` が無く `pyproject.toml` があるときは、その `pyproject.toml` を `workspace/configuration` の
  `basedpyright.analysis.configFilePath`・`python.analysis.configFilePath` で返す（basedpyright は `basedpyright.analysis` があれば
  `python.analysis` を読まないので両方）。節があれば同じ設定を読む（尊重は変わらない）、無ければ既定の設定で根の下を解析する。
  根に `pyrightconfig.json` があれば明示しない（利用者の設定を尊重）。どちらも無ければサーバーは親を探さないので明示しない（存在しない
  パスを渡すと、ディレクトリを設定として読もうとしてエラーをログに出す）。起動引数・`initializationOptions` は使わない（basedpyright の
  langserver は設定ファイルの指定を `workspace/configuration` でしか受けない。コードで確認）。リポジトリには何も書かない。
  pyright（代替）は `python.analysis.configFilePath` を読むかを確かめていない（入っていない）。
- **対象の外のファイルを先に開かせる**（`tools/_lsp_exact.prime_set`。tsserver の `tsconfig` の外と同じ部品。BUG-0047）: T-06 の参照検索・
  T-07 の incoming は、聞く前に、basedpyright の解析の対象の外で対象の名前を単語境界で含む Python のファイル（索引の Tier 1〜2、未オープン、
  テスト優先、1 回に 100 件まで、超えた分は `warnings`）を `didOpen` する。答えは LSP が見たものそのままなので `exact` のまま正しい
  （降格して `structural` にするより、呼ぶ側の AI にとって有用）。
- **解析の対象の求め方**（`lsp/pyright_scope.py`）: basedpyright 1.40.2 に合わせる。根の `pyrightconfig.json`（JSONC）→ 根の `pyproject.toml` の
  節。`extends`（文字列）を辿り、最も派生した設定の値が勝つ（パスは書いた設定のディレクトリから）。`include` が無ければ根、`exclude` には
  サーバーの既定（`**/node_modules`・`**/__pycache__`・`**/.*`・`**/__editable__.*`。1.40.2 は利用者の `exclude` があっても足す）。照合は
  `getFileSpec` と同じ正規表現（`**` は 0 以上の階層、`*`・`?` は階層の中、末尾 `($|/)`、Windows は大文字小文字を区別しない）。仮想環境の
  自動の除外は見ない（内とみなす = 開かせない側）。読めない設定・両方の節は判断しない（開かせない）。利用者が `include` / `exclude` を
  書いていない（既定の除外だけ）ときも開かせず、ファイルの一覧も引かない（ふつうのワークスペースの答えの速さを変えない）。
- **対象は LSP の起動のときに覚える**（`LspHandle.analysis_scope`）: basedpyright は起動した後の根の設定の編集を受け取らない（BUG-0135 の票）。
  Lucianke が編集を追って対象を引き直すとサーバーとの食い違いが出るので、起動のたびに引き直してそれで決める。
- 仕様書: T-06 処理規約 2・T-07 処理規約 2（Python の開かせる規則）、§12.3 に「Python の設定の根」の行。設計書: §7.5.1（Python の備考）・§7.5.6。
- F-POLY の `pyrightconfig.json`（BUG-0129 の直し）はそのまま残した（あっても同じ答え）。

### 確認

票の再現（Lucianke のリポジトリの根の `pyrightconfig.json`〈`exclude: tests/fixtures`〉の下の `tests/fixtures/` に、`pyrightconfig.json` を外した
F-POLY の写しを置き、Lucianke の T-07 `incoming`・`depth: 1` で `LimitTable.get` を 3 回。使い捨ての試験で、明示を外した場合と比べた）:

| 条件 | 1 回目 | 2・3 回目 | 答え |
|---|---|---|---|
| 明示しない（直しの前と同じ） | 10.02 秒（予算切れ） | 10.01・10.02 秒 | `structural`・`relations(CALLS)` の `_validate_limit` |
| 明示する（直しの後） | 0.28 秒 | 0.01・0.00 秒（PaaD） | `exact`・`basedpyright+tree-sitter` の `_validate_limit`（payment.py） |

`include: ["src"]` の F-POLY（根の `pyrightconfig.json`）: T-06 の `process_charge` は直しの前 `exact` で routes.py の 2 箇所 → 直しの後 `exact` で
`tests/test_payment.py` を含む 3 箇所（`totals: files 2・sites 3・tests 1`）、T-07 の incoming は 2 ノード → `test_charge_ok` を含む 3 ノード。

試験（決定的）:

- 新しい `tests/contract/test_lsp_ancestor_pyright_config_bug0132.py`（実機の basedpyright。無ければ skip）: `include: ["libs"]` の
  `pyrightconfig.json` を持つ親の下の F-POLY（設定ファイルなし）で T-07 incoming が予算の内に `exact` の `_validate_limit` を返す
  （直しの前は `structural` に降格して赤）・T-06 が payment.py の 1 箇所を `exact` で返す。`include: ["src"]` の F-POLY で T-06・T-07 が
  `tests/` の呼び出しを `exact` で含む。
- 新しい `tests/unit/test_lsp_pyright_config_pin_bug0132.py`: 明示するかの判断（`pyproject.toml` だけ・節あり・`pyrightconfig.json` あり・
  どちらも無し）、`build_settings` の節（basedpyright は両方、pyright は `python` だけ、ruff・tsls は変えない）、偽のサーバーが
  `workspace/configuration` で `configFilePath` を受け取ること、起動したハンドルが解析の対象を覚えること。
- 新しい `tests/unit/test_pyright_scope_bug0132.py`: 既定の除外・`include` の絞り・利用者の `exclude` と既定の足し合わせ・`**`・`*`・`?`・
  JSONC・`[tool.basedpyright]` / `[tool.pyright]`・`pyrightconfig.json` の優先・`extends`（相対パス・派生した側の勝ち・辿れない / 循環）・
  読めない設定・根の設定の編集に従う覚え方・Windows の大文字小文字。Python の候補（対象の外で名前を含むものだけ・テストが先・TS を混ぜない・
  既定の除外だけならファイルの一覧も引かない・読めない設定や対象が分からないときは開かせない・起動のときの対象で決める・上限）。
- 既存の `tests/unit/test_lsp_prime.py` の「TS 系だけ」の期待を「TS 系と Python」に直した。

関係する unit・contract（LSP のプール・クライアント・機能、T-06・T-07 の LSP・PaaD・予算・補足の欄・TS の tsconfig の外、BUG-0132 の新しい
試験）359 件と見張り 4 本（`test_import_graph`・`test_server_launch_guard`・`test_stdout_purity`・`test_leak_guard`）が green。`uv run ruff check` クリーン。

### 残ったこと

- 根の設定ファイルを LSP の起動の後に作る・書き換える・消すと、起動し直すまで basedpyright に届かない（BUG-0135 の票）。直すときは、
  `LspHandle.analysis_scope`（と、根に `pyrightconfig.json` を作ったときは明示の判断）も合わせて引き直すこと。
- pyright（代替のサーバー）が `python.analysis.configFilePath` を読むかは確かめていない。
