# SYNC-1 スパイクの結論の反映（S-1・S-2・S-3・S-7・S-9・S-10 と core の構築報告）

日付 2026-10-29。設計書 = `docs/Lucianke アーキテクチャ設計書.md`、仕様書 = `docs/Lucianke仕様書.md`。節番号は変えていない。新しい事実には `（S-nn で確認）` を添えた。

## 反映した点（25 点。表の 1 行 = 1 点）

| 出どころ | 点 | 設計書 | 仕様書 |
|---|---|---|---|
| S-1 | ADR-2 維持。低水準はコンストラクタ引数方式、SDK は引数を検証せず例外も捕捉しない（最外周で捕捉）、`Server.middleware` は暫定で不使用、`mcp` を `<2.4` に固定 | ADR-2、§7.1.2、§11.1 | — |
| S-1 | ADR-3 は結論維持で文言修正（テキストストリーム、`TextIOWrapper(newline="\n")`、fd 0 も NUL、手動退避のあと引数なし `stdio_server()` 不可、`SetStdHandle` 不要） | ADR-3 | §12.1 の 2・3（stdin の隔離） |
| S-1 | roots は「取れたら」。`initialized` のあと 5 s、取れなければ cwd と T-00 `set_roots`。2026-07-28 版は `list_changed` 廃止 | §6.5、§8.1 は §6.5 に従う | §5.1、§4.3.1、FR-WS-1 |
| S-1 | スキーマ違反は `isError: true` の `E_INVALID_ARGUMENT`。JSON-RPC エラーは未知のツール名だけ | §7.1.2（元から） | §5.1、§5.8、§13.6 |
| S-1 | HTTP 系依存は外せず stdio でも import、コールドスタート約 1 s（許容） | ADR-2、§6.5、付録 A.1・A.2・A.3 | §14.4 |
| S-1・S-9・S-2・S-3・S-7・S-10 | §12.1 に「結果」列を追加（合格と報告書へのリンク）。§13 に「状態」列を追加（R-1 縮小、R-2 解消、R-3 解消、R-5 縮小） | §12.1、§12.2、§13 | — |
| S-9 | 往復 p95 1.08 ms の予算、`ping` の bench を回帰基準に | §1.1、§7.1.2、§12.2 タスク 4 | — |
| S-2 | 18 言語すべて読み込み可（ABI 14/15）、登録表から外す文法なし、`TAGS_QUERY` に頼らず `.scm` は自前、タグは `matches()`、`tree-sitter` を `<0.27` に固定 | §7.4.1、§11.1、付録 A | — |
| S-2 | `parse()` は GIL を握りプールは並列化せずループも止まる。解析は常に `read_callback`（16 KiB）、`progress_callback` 禁止 | ADR-13、§6.1、§7.4.2 項目 1、§7.6.2 | — |
| S-2 | 構文木はソースの約 20〜26 倍。`TREE_CACHE_MAX_BYTES` は「ソース長 × 25」で数える | ADR-13、§7.4.2 項目 3 | §12.8 |
| S-2 | `error_spans`、Python は粗い ERROR なので個数の前後比較を主に、増分解析は MAY のまま、`Node.text` と `ParsedFile.source` | §7.4.2 項目 5・6、§7.7.4 | — |
| S-2 | 列はバイト、`LineIndex` は `\n` だけで行を切る、変換は `syntax` の出口 1 か所 | ADR-14、§9.6 | — |
| S-3 | `replace`・`commit_edits` はあるが展開は自前。`outermost` 必須。非対応言語、`c_sharp` は `csharp`、`PanicException` を事前に弾く | §7.4.7 | T-12（`language`、処理規約 2） |
| S-3 | 不正パターンは 0 件。単独ノードにならないときだけ `E_SYNTAX_INVALID`。位置はコードポイントで変換不要。予算（約 400 ファイル / 5 s） | §7.4.7、ADR-14 | T-12（処理規約 1） |
| S-7 | §7.6.4 の順序（接続 → grep の temp → `query_only` → `setlimit` → authorizer → progress）、`setup_phase` を廃止、`;` 除去、`EXPLAIN` の前置、`perf_counter`、1 要求 1 接続 | §7.6.4 手順 1〜7、§6.2 | T-20（処理規約 1） |
| S-7 | authorizer の許可集合（FTS5 影テーブル、`json_each` / `json_tree`、`PRAGMA data_version`、列を読まない別名） | §7.6.4 判定表 | §9.5 |
| S-7 | 3 重防御は DB 本体の書き込みについてだけ成り立つ。`ATTACH` が空ファイルを作る、禁止語検査は省かない | §7.6.4 注記・受入 | §13.2、T-20（処理規約 2） |
| S-7 | 再帰 CTE の深さ 10,000 上限は実装できないので時間で止める。`setlimit` で巨大値を抑える | §7.6.4 手順 6 | §13.2 |
| S-10 | 再試行は `winerror in {5, 32}`、10〜160 ms（合計 310 ms）、全体 1 s。tmp はバイナリで書き close してから改名 | §7.7.5 手順 3・5、§9.5 | — |
| S-10 | 読み取り専用属性は `E_MUTATION_BLOCKED(read_only_file)`。tmp に付けない | §7.7.5 手順 3 | §5.8、§10.6 |
| S-10 | 改名で保持されないもの（作成時刻・明示 ACE・代替ストリーム）、Windows のディレクトリ `fsync` は行わない、`posix_replace` は MAY、テストの型 | §7.7.5 | §10.6 |
| core | `Urn`・`Selector` は `urn.py` | §4.1、§5 | — |
| core | Windows は Job Object で孫まで落とす（縮退あり） | ADR-12、§9.5 | — |
| core | `ProcessRegistry` は `core/procs.py` | §4.1、§9.4、付録 B | — |
| core | `Kind` に `File` を足す（ファイル単位の要素。`symbols` には入れない） | §5 | §6.1（EBNF と規約 7）、§9.3 |

## 反映しなかった点と理由

- `pyproject.toml` の実際の固定（現状 `mcp>=2.3,<3`）、`core/model.py` の `Kind.FILE` 追加: コードは対象外。設計書 §11.1 と §5 に目標を書いた。構築者への引き継ぎが要る。
- S-2 項目 8 の BOM の扱い（1 行目の開始オフセットで除外するか正規形で除くか）: §10 の正規化規則との整合確認が先。ADR-14 に「LSP との境界でだけ合わせる」とだけ書いた。
- S-3 項目 5 の Kotlin・Swift を M1 の構文パターン検索に使うか: 方針の判断。事実（ast-grep が文法を内蔵）だけを §7.4.1 に書いた。パターン用の包み（`context` / `selector`）と前ふるいは M2 の案・未測定として §7.4.7 に残した。
- S-1 の `InputRequiredResult` 方式、S-2 の増分解析、S-10 の `posix_replace`: M2 以降・MAY として記述にとどめた。
- 仕様書 §12.5（終了処理）への Job Object: NFR-OPS-2 の文言は子孫を明示しないため、設計書 ADR-12 だけに書いた。
- 親の要点に無かった S-7・S-10 の「設計への反映（推奨）」は、報告書のとおり反映した（取り消すなら §7.6.4、§7.7.5、仕様書 §9.5・§10.6・§13.2・§5.8 の `E_MUTATION_BLOCKED`）。
- 判断した 2 点: 0 件ヒントは `details.hint` ではなく `warnings`（エラーではないため。S-3 の報告とは異なる）。仕様書 §9.3 に「`File` は `symbols` に格納しない」と追記した（元は「kind は §6.1 と同一」）。

## 未反映のスパイク

S-4、S-5、S-6、S-8、S-11、S-12（§12.1 の結果欄は「未反映」のまま。S-4・S-5・S-6・S-12 は報告書が既にあるが、書いた役が反映中のため今回は扱わない。S-8・S-11 は報告書なし）。
