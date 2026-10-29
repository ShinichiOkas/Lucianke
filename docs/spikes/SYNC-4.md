# SYNC-4 OD-20 の決定・SYNC-3 の申し送り・S-12 の採用・実装との差・Compact の例の反映

日付 2026-10-29。設計書 = `docs/Lucianke アーキテクチャ設計書.md`、仕様書 = `docs/Lucianke仕様書.md`。節番号は変えていない。実装で決まった点には `（実装で確定 2026-10-29）` を添えた。第 1〜3 回は [SYNC-1.md](SYNC-1.md)・[SYNC-2.md](SYNC-2.md)・[SYNC-3.md](SYNC-3.md)。

共有作業ツリーで他の役が仕様書 §5.7 と設計書 §7.2.2（応答全体の予算、BUG-0014・BUG-0017）を未コミットで編集していたので、本回のコミットは **自分の編集だけを HEAD の blob に当てて作った**（その役の変更は含めていない。作業ツリーには残っている）。

## 反映した点（表の 1 行 = 1 点）

| 節 | 何を | 根拠 |
|---|---|---|
| 設計書 §7.9.2 | 履歴の経路を「未決」から**決定**に: 履歴専用の読み取り専用 `git` 子プロセス（`git log --follow …` と `git diff-tree -r -M50% …`、`LC_ALL=C`、`core/procs` 経由）、書き込み系は起動しない、`git` が無ければ自前の path 限定 walk（`Walker` そのものは使わない）。`git` は別プロセスとして起動するだけで同梱・リンクしない（第 16 章 L-4 に適合）と 1 文。M1 は dulwich だけ | f68766b、5a24430、師匠 2026-10-29「推奨: 読み取り専用で使うことを認る」 |
| 設計書 §13 R-7、§12.1 S-8 行、§4.1 の `history.py` | 「推奨・未決」「(b) は M2 のベンチで測る」を「決定。(b) pygit2 は採らない。子プロセスの経路は M2 着手時に実装し F-PY で p95 を測る」に | f68766b |
| 仕様書 §4.2、§4.6、§11.7、第 16 章の GitPython 行 | 「シェルの `git` を呼ばない（SHOULD NOT）」に履歴専用の読み取り専用 `git` 子プロセスの例外を明記。§11.7 の「libgit2 系・gitoxide を想定」を dulwich に。OD-20 の「(a) の `Walker` に戻る」を「dulwich の自前 path 限定 walk を `max_commits` で区切る」に直した（設計書の S-8 の結論に合わせる） | f68766b |
| 仕様書 §12.2 | `positionEncoding` が無ければ UTF-16、サーバー別の上書きは持たない、送る列は文字境界だけ、丸めが要るときは降格（ruff はパニック）、実測 4 サーバー | 432c6c0、SYNC-3 申し送り 1、S-11 |
| 仕様書 §12.3 | 環境変数はサーバー名単位 `LUCIANKE_LSP_<サーバー名>_CMD` が先、言語単位が後（lint は `…_LINT_CMD`）。TypeScript の既定は typescript-language-server、代替は vtsls | 432c6c0、SYNC-3 申し送り 3、設計書 §7.5.1 |
| 仕様書 §12.7 | `LUCIANKE_LSP_<サーバー名>_CMD` と `lsp.position_encodings`（MAY、既定 `["utf-8","utf-16"]`、**未実装**の印） | 432c6c0、SYNC-3 申し送り 2 |
| 仕様書 T-06・T-07 事後条件 | 未解決 import が外部シンボル `lk://_ext/<パッケージ名>#<名前>`（`is_external`）として現れる。T-06 は `import` の使用箇所を返す、T-07 は `children` を持たない葉ノード | 432c6c0、SYNC-3 申し送り 4、53ef0e2 → 53ce5aa、`tests/contract/test_T06_T07.py` |
| 設計書 §7.2.1 | 「提案」を**採用済み**（27e0821）に。`tokens.toml` は設けず `core/tokens.py` の `TOKEN_COEFFICIENTS`（旧名 `TOKENS_PER_CHAR` は別名）。×1.03 は見送り（合成 base64 が −8.3%）。回帰テスト `test_core_tokens_calibration.py` と結果、速度。1 レコードの推定に行末の改行を数える（改行 0.25 → 0.6）、縮退に終端の改行の分を引いた予算、`estimate_tokens` も同じ推定器 | 0ee8775、27e0821、`docs/spikes/S-12.md`「反映」 |
| 設計書 §12.1 S-12 行、§13 R-12 | 「代替で合格見込み」→「`lean_wide` に替えて合格。27e0821 で置き換え済み」。R-12 に採用済みと残り（TS・Go・Rust・韓国語は未検証、×1.03 見送り） | 0ee8775 |
| 仕様書 T-08 | 規約 4: `revision` が `WORKING_TREE` 以外は M1 では `E_UNSUPPORTED`（`revision_not_supported_until_M2`）。(a) のみを返すのは M2。エラー一覧にも追記。`lsp_sources[]` に `role`（`types` / `lint`）で言語 × 役ごと。QuickFix の ID は `qf_` + 4 桁 16 進の連番、問い合わせは現在ページ（`cursor` 以降）の先頭から LSP 由来の error / warning 100 件まで。規約 9: ワークスペース全体では LSP に開かせるのは 100 ファイルまで（超過分は構文エラーだけ + `warnings`） | 0ee8775、`tools/t08_get_diagnostics.py`、`lsp/action_store.py` |
| 仕様書 T-10 | compact の `files_changed` の sha256 は先頭 12 桁 + `…`（json は 64 桁）。`E_INVALID_ARGUMENT` に「`target` が `path` だけ」と「`lexical_fallback` への実書き込み〔`allow_heuristic_mutation: true` かつ `dry_run: false`〕で `expected_file_sha256` が無い」 | 0ee8775、`tools/t10_edit_symbol_body.py` |
| 仕様書 T-03 | `exact` は `name`（最後の部分）の等価比較だけで、`Class.method` の限定名では当てない、と明記（仕様の事後条件 1 に実装〔af20c57〕が合っていたので文言の補足のみ） | 0ee8775、af20c57 |
| 仕様書 §5.6-2、設計書 §7.2.2 の擬似コード | `total_estimate` が渡した件数より大きいとき、全部入っても `next_cursor` と `# more: … remaining≈N`（N = 入らなかった分 + 渡されなかった分）。設計書の文章は f0f328e で入っていたので擬似コードに `unseen` を足した | 0ee8775、f0f328e（BUG-0008） |
| 仕様書 §5.8、T-02 | `E_UNSUPPORTED` の `details.reason` に `invalid_data_file`（壊れたデータファイル）ほか（`revision_not_supported_until_M2`・`not_in_milestone`・`file_too_large`・`secret_file`・`binary_file`）。T-02 のエラーに項目単位の `E_UNSUPPORTED` | 432c6c0、f96b2b7、`tools/_data_files.py`・`_rev.py`・`_unsupported.py` |
| 仕様書 T-11 事後条件 3 | `edits_count` は「LSP の参照件数 + 1」（T-06 の件数とは一致しなくてよい。basedpyright の rename は `tests/` を含めないことがある） | 432c6c0、19b の観察（親の依頼） |
| 仕様書 §14.4-2 | 母集団を「成功し、かつ `is_degraded == false`（そのツールの最高水準で応答した）呼び出し」に。`exact` だけの集計も併記。T-02・T-03・T-05 は最高水準が `structural` で `exact` では母集団が空になる | 432c6c0、親の決定（ベンチ 18b の判断の追認。§5.5 の `is_degraded` に合わせる） |
| 仕様書 §6.1 | EBNF の `fold` を `"{ ..." , int , ( " lines" \| " nodes" ) , " omitted }"` に（規約 9 の折りたたみの記述も `nodes` を含める）。T-02 の本文（規約 4）と §6.1.1 の例は既に `nodes` / `lines` と整合 | 432c6c0、020a19e |
| 仕様書 §6.1（EBNF） | 検査器の docstring が決めていた点に EBNF を合わせた: ヘッダ末尾の `~used/maxtok`、本文中の `banner`、空でもよい `kv-line`、`tree-line` の `(cycle)`、トレーラは `# summary:` → `# more:` の順（それぞれ省略可）。`kind` の定義は変えていない | 7073ef0、`tests/contract/compact_grammar.py` |
| 仕様書 T-05・T-12 の例 | コードブロックを含む compact の例は外側のフェンスを 4 つに（規約 5 のとおり。3 つだと Markdown が入れ子で閉じてしまい、後ろの例の抽出も狂う） | 7073ef0、5a7940a |
| 仕様書 T-16・T-17 の例 | 文法に当てはまらなかった行を `- ` の note 行にした: T-16 の `trace:` の各ステップ、T-17 に `commits:` の見出しを足してコミット行。検査器と合う（T-16 は M3、T-17 は M2 で未実装。実装するときは note 行で出す） | 7073ef0 |
| 設計書 §7.4.1、§7.6.2 | `FormatAdapter` の Protocol に `skeleton_record` と `symbols`、`selector` は式の文字列（`"jsonpath:$.a"`）。Tier 3 の索引は最上位の節だけをシンボルに（JSON / YAML / TOML は `Key`、XML は根と直下の子の `Tag`。128 KiB 超・JSONL・根が配列・ipynb・壊れたファイルは `files` 行だけ。1 ファイル 500 件まで） | 7073ef0、a9e9818（7f） |
| 設計書 §7.7（T-05 の失敗分岐） | 「1 件も入らない → `next_cursor` + `warnings`」を「`next_cursor` なし」に（§5.6-4 と設計書 §7.2.2 は 4414fad で済み。残っていた 1 か所） | 432c6c0、4414fad（BUG-0015） |

## 確認だけで変えなかったもの

| 項目 | 結果 |
|---|---|
| 仕様書 §5.7 と設計書 §7.2.1 の「文字種別と連の数の係数」 | SYNC-2 で既に lean_wide の文言に直っていた。残っていたのは「提案」「`tokens.toml`」「追従する」の語（設計書）。§5.7 は他の役の未コミット編集（`used_tokens_est` の定義）が入っているため触らなかった |
| 仕様書 §6.1 の `fold` の `nodes omitted` | EBNF には 020a19e の前から入っていた（`( " lines omitted }" \| " nodes omitted }" )`）。coordinator の依頼の形に書き換えただけ |
| 仕様書の compact の例 26 ブロック | 検査器 `compact_grammar.validate` に通した結果、25 が適合、1 が §5.4.2 の `...` の例（規約 9 で検査対象外と宣言済み）。通した手段は使い捨ての抽出スクリプト（フェンス長を見る）。`tests/contract/test_compact_grammar.py` は仕様書を読まず、例を写して持っているので、仕様書の例の変更では落ちない（**仕様書から例を抜いて検査するテストを足すのは tests 側の宿題**） |
| 仕様書 §5.6-4（BUG-0015） | 設計書 §7.2.2 と同じ（next_cursor なし）。他に矛盾していたのは上記の設計書 1 か所だけ |

## 未決・宿題として残したもの

- **履歴専用 `git` 子プロセスの実装**（M2 着手時）: 内部設計（許可リスト、`GIT_OPTIONAL_LOCKS` 等の環境、タイムアウト）は書いていない。F-PY で p95 を測る。
- **`lsp.position_encodings`** と `tests/lsp_contract` は未実装（仕様書 §12.7 に「未実装」と書いた）。
- **トークン推定**: TS・Go・Rust・韓国語は未検証、×1.03 は見送り（合成 base64 −8.3%）。M1 のベンチで再較正。
- **T-08 の 100 ファイル・QuickFix 100 件の上限**は実装の定数（`MAX_LSP_FILES` = `LSP_OPEN_DOCS_MAX // 2`、`CODE_ACTION_MAX_DIAGS`）を仕様にした。実装が仕様の意図（予算内で素早く返す）を満たしているので実装に合わせた。大きな差ではないが、数字を変えるなら両方を直す。
- **T-16・T-17 の compact の実装**（M3・M2）は、note 行で出す必要がある（今回の例の形）。T-17 のコミット行は `core/compact_lines.note` で組める。
- **検査器の宿題**: 仕様書の例を直接読むテスト（上記）。
- 他の役の未コミット編集（仕様書 §5.7、設計書 §7.2.2）は本回で触っていない。その役がコミットするときに、本回の 6 コミットと重なる箇所（設計書 §7.2.2 の擬似コード 2 行）が衝突しないかだけ確かめてほしい。
