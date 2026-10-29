# ドッグフーディング報告（2026-10-29 14:04〜14:40、問題出し担当）

実クライアント（stdio、mcp 2.x）で `get_file_skeleton`・`get_symbol_bodies` をこのリポジトリ自身に当てた結果。台本は `run_dogfood.py`（観点 1〜8）と補助の `probe*.py`・`*_probe.py`。生の出力は `out/`（コミットしない）。
作業中に他の役が `src/` を更新し続けたため、サーバーの動き（`tools=` の数・予算の扱い）は途中で変わった。数字は取得時点のもの。

| 観点 | 結果 | 実測 |
|---|---|---|
| 1 骨格の網羅と範囲 | 合格 | `src/`・`tests/` の 172 ファイル、`ast` の関数・クラス・メソッド 3,395 件（入れ子の関数を除く）。漏れ 0、余り 0、範囲のずれ 0（デコレータ含む開始行も一致）。`@property` は `[Property]`、入れ子の関数も骨格に出る（余りには数えず） |
| 2 行の文法 | 合格 | 172 ファイルの compact 応答すべてが `compact_grammar.validate` を通過（違反 0）。bodies の 50 件・TS・エラー応答も 0 |
| 3 予算 | 不合格 | BUG-0014（`~used` が数え直しより 28〜30 小さく、500 で 4/13 ページ・200 で 2/2 ページが超過）、BUG-0015（空ページの cursor が進まない）。それ以外は、2000 で 4 ページ・500 で 13 ページを cursor で辿ると 15 ファイルが重複なく欠落なく揃う。cursor を別の引数に付ける／壊すと `E_INVALID_ARGUMENT` |
| 4 本体 | 合格 | ランダム 50 シンボル（seed 固定）: 本体の行・範囲（`context_lines=0` で `ast` と一致）・`file_sha256`・compact の `sha:`（12 桁）・`@L` の連番と行の内容が全件一致。違反 0 |
| 5 ヘッダとメタデータ | 一部不合格 | compact ヘッダの形（`conf= lsp= rev= ws= ~used/max tok`）、json のキー（`_resolution_meta`・`page`・`budget`・`timing_ms`・`warnings`・`error`）は仕様どおり。`ws=` は途中から `Lucianke` が出る（BUG-0004 は解消した様子）。BUG-0014・BUG-0017・BUG-0018 |
| 6 エラー | ほぼ合格 | 存在しないファイル=`E_TARGET_NOT_FOUND`、`../x.py`・絶対パスのルート外・`src/../../x.py`=`E_INVALID_ARGUMENT`（`outside_root`）、21 件=`E_INVALID_ARGUMENT`、曖昧（TS の `charge`）=`E_TARGET_AMBIGUOUS` と候補、無い symbol=`E_TARGET_NOT_FOUND`、スキーマ違反も `E_INVALID_ARGUMENT`（JSON-RPC エラーにならない）、部分失敗は `ok:true`＋失敗項目＋`warnings`。差は BUG-0016・BUG-0018 |
| 7 速さ | 不合格（高負荷下） | `get_file_skeleton`（budget.py 1 本）100 回: 往復 p50 14.9 ms / p95 91.7 ms / max 399 ms、json の `timing_ms` は p50 13 / p95 289 / max 896（他の役が同時にビルド・テストを走らせている状態）。NFR-PERF-1 の p95 < 50 ms は満たさず。`get_symbol_bodies` の 100 回測定は BUG-0020 のため完走できず（1 回あたりは 10〜13 ms） |
| 8 TypeScript | 合格 | `app.ts`・`payment.ts` の骨格（`[Interface]`/`[Class]`/`[Constructor]`/`[Method]`、オーバーロード 3 件が別シンボル、`doc:`・`raises: Error`・`export`・`async`）と文法に違反なし。本体はクラス・インターフェース・コンストラクタ・`client`・`checkout` が行と一致。`charge`（オーバーロード）は `E_TARGET_AMBIGUOUS` で候補 3 件（URN に `/1`・`/2~hash`） |

## バグ票（`bugs/`）
- BUG-0014 ヘッダの `~used` が数え直しより常に 28〜30 小さく、max_tokens を超える応答がある
- BUG-0015 レコードが入らない小さい max_tokens で同じ cursor が返り続け、先へ進めない
- BUG-0016 `get_symbol_bodies` の本体切り詰めに `next_cursor` が無く（`line_range` の注記のみ）、`budget.truncated` も false
- BUG-0017 json の `used_tokens_est` が JSON 実サイズの約 1/7
- BUG-0018 `revision` が WORKING_TREE 以外で `E_UNSUPPORTED`（存在しない参照も）、TOML・Markdown・拡張子なしが `E_UNSUPPORTED`
- BUG-0020 連続 200 回超の呼び出しでサーバーが無応答／`Connection closed`（断続的）
