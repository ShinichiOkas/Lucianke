# BUG-0078 T-02 の縮退の段階が粗く（6,324 → 441）、予算 6,000 で 618 トークンしか使わずにカーソルを返す

- 報告: 2026-10-31 / 見た人: 親（BUG-0067 の直し役が報告した実測を、AI の利用者として読んだ）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（BUG-0067 の票の「原因・直し方・確認」から。F-PY〈httpx〉、`get_file_skeleton {"paths": ["httpx/_urls.py", "httpx/_api.py"], "format": "json"}`）

- `_urls.py` の json の縮退の段階: 6,627（丸ごと）→ 6,325（doc なし）→ 6,324（urn なし）→ **441（入れ子を落とす）**。`_api.py` は 2,033 より小さくならない。
- BUG-0067（`afc06fc`）で「縮退したレコードでページを閉じる」規則になった後、`max_tokens` 3,000〜6,500 の応答は `_urls.py` の最上位だけ（618 トークン）と `next_cursor`。
  6,000 の予算のうち約 1 割しか使わず、もう 1 回の呼び出しが要る。

## 利用者（呼ぶ側の AI）として困ること

- 渡した予算を使い切らずに続きを返すので、呼び出しの回数が増える（往復ごとにコンテキストを使う）。
- 6,000 あれば `_urls.py` の入れ子の多くを見られるはずなのに、最上位の名前だけになる（段階が 6,324 の次が 441 しかない）。

## 関連

- BUG-0067（単調性。規則は正しい。段階の粗さが利用率を落としている）、BUG-0055 / BUG-0065（T-02 の json の量）

## 原因・直し方・確認（2026-10-31）

### 原因

- 骨格のレコード（`src/lucianke/syntax/skeleton.py` の `build_file_record`）の縮退の段は「`doc:` を全部落とす → `urn:` を全部落とす → 非公開を全部落とす → 入れ子を全部落とす」の 4 段だけだった。BUG-0067 の規則（縮退版でページを閉じる）の下では、収まる最も浅い段が最後の段（最上位だけ）になると、残りの予算はそのまま使われない。F-PY の `_urls.py` は入れ子 50 件を一度に落とす段（6,324 → 441）しか無かったので、json の 3,000〜6,500 は 618 トークンとカーソルだった。
- 同じ粗さは compact の `doc:` の段にもあった（`_urls.py` は丸ごと 2,073 → `doc:` を全部落として 1,647。予算 2,000 で 82%、1,500 で最上位だけの 219 / 1,500 = 15%）。

### 仕様の判断（主な利用者は MCP で呼ぶ AI。渡した予算を使い切った応答で往復を減らし、縮み方はなだらかに）

- 段を **1 件ずつ** にした（仕様書 §11 T-02 規約 8 を改訂）。鎖は予算に依らない固定の列で、段 t = 1, 2, … の順に 1 つずつ落とす:
  1. `doc:` 行を 1 件ずつ。深い順、同じ深さでは文書の後ろから（クラスや最上位の関数の doc は最後まで残る）。
  2. 導けない URN の `urn:` 行（TS のオーバーロードなど）を 1 件ずつ。同じ順。
  3. 非公開メンバー（どの深さでも）を文書の後ろから。
  4. 入れ子の公開メンバーを、残す順「浅い順 → 文書順」（クラスごとに上から）の逆に。メソッドの中の入れ子の関数（深さ 2）は、どのクラスのメソッド（深さ 1）より先に落ちる。最上位の公開シンボルは残す。
- 選んだ理由: AI が骨格を読むのは、どの名前があるかを知って `get_symbol_bodies` などで本文を取るため。属性行（doc）より名前、非公開より公開、入れ子の関数よりクラスのメソッドを残す。文書順に残すので、欠けるのは「後ろのクラスの後ろのメンバー」で、どこが欠けたか読みやすい（欠けるクラスは高々 1 つが途中、それより後ろは空）。
- 選ばなかった案: 入れ子のシグネチャ・doc を先に落とす（json はシンボルごとの固定のキーが大半で、シグネチャを落としても小さくならない。`signature: null` が「引数なし」と区別できない）。大きいクラスのメンバーから落とす（完全なクラスは増えるが、どこが欠けたかの規則が読みにくい。入れ物ごとの件数で欠けたクラスは分かるので、文書順にした）。
- 注（compact の最後の行。json は `items[].truncated_note` に同じ文）: `- truncated: N members omitted (<属性>): <入れ物> <件数>, …; whole file: max_tokens>=M with this path alone`。
  - `<属性>`: `doc`・`urn`（全部）か `K doc lines`・`K urn lines`（一部）。json は urn: 行を持たない（どのシンボルも `urn` を持つ）ので json の注は `urn` を挙げない（挙げると json の大きさが段を深くして増え、単調でなくなった。測って見つけた）。
  - `<入れ物>`: 落としたメンバーの親の修飾名（最上位は `(top level)`）と件数（子孫も数える）。文書順に 8 件まで、残りは `(others)`。json は `items[].truncated_by_container[]`（`{container, omitted}`。丸ごとなら `[]`）。
  - `M`: そのファイルだけを丸ごと返す `max_tokens` の目安（形式ごとの丸ごとの大きさ + 前置きの分 200 を 100 単位に切り上げ）。落とした分はカーソルでは辿れない（カーソルは次のファイルへ進む）ので、呼び直しの手として添えた。ほかの道具の案（`search_workspace_symbols` の `regex` と `scope_paths` でクラスのメンバーを引く）は索引に頼り、`revision` を読めないので、どの場合も効くこの呼び直しだけにした。
- 単調性（BUG-0067）はそのまま: `Record.shrink(ask)` は段を二分探索する。調べる位置は範囲 `[1, 最後の段]` と各位置の判定（`段の大きさ <= ask`）だけで決まり、判定は `ask` について単調なので、`ask` を上げても深い段（小さい版）を返さない。

### 速さ（json の二分探索を版ごとに JSON を作って数えないように）

- 段の数がメンバー数ほどに増えたので、段ごとに本文を作り直して数えると大きいファイルで遅い（試作で、メンバー 3,000 件の合成ファイルの json が 1.5 秒）。
- `core/tokens.py` に `TokenEstimator.units`（切り上げ前の値）を足した。どの項の連も改行をまたがないので、行の列の値は行ごとの和と改行の分（`NEWLINE_UNITS`）で、連結して数えた値と一致する。詰めた JSON の配列 `"k":[A,B],"m"` は `units('"k":[],"m"') + Σ(units(X) − SYMBOL_RUN_UNITS)`（要素が記号で始まり記号で終わり、前後の記号の連とつながる）。骨格はシンボルごとに行と JSON の部品の `units` を 1 回だけ数えて覚え、段の大きさを足し算で出す（性質テストで、全段・全予算で数え直しと一致することを確かめた）。
- `Record.shrink_json`（任意）を足した。json の大きさで段を直接選ぶ。パッカーは `pack(..., form="json")`（`mcp/fit.py` が json のときに渡す）でこれを先に使い、実際に数えて収まればそのまま、収まらなければ従来の `ask` の二分探索に戻る。
- 縮退版の `data`（json の項目）は読まれたときに作る（compact の応答では作らない）。
- 測った値（`Packer.pack`、初回 / 2 回目以降）: F-PY の `_client.py`・`_models.py`・`_urls.py`、F-TS の `Ky.ts` で compact 5〜35 / 0 ms、json 1〜50 / 0〜2 ms（直す前は json 1〜35 / 0〜7 ms）。メンバー 3,000 件の合成ファイルは初回 30〜370 ms（直す前も最大 620 ms）。

### 直したファイル

- `src/lucianke/syntax/skeleton.py`: 段の鎖（`_assign_ranks`・`_rank_urns`・`_Detail(doc_cut, urn_cut, cut)`）、二分探索（`deepest_fit`）、行・JSON の部品の覚え（`_Out`・`_Meter`・`_symbol_json_units`）、注と `truncated_by_container`・`truncated_note`、`shrink_json`、遅延の `data`（`_LazyData`）。
- `src/lucianke/core/tokens.py`（`units`・`UNIT_DEN`・`NEWLINE_UNITS`・`SYMBOL_RUN_UNITS`）、`src/lucianke/core/records.py`（`Record.shrink_json`）、`src/lucianke/mcp/budget.py`（`pack(form=…)`、`_shrink` の json の近道）、`src/lucianke/mcp/fit.py`（json のとき `form="json"`）。
- `docs/Lucianke仕様書.md`（§11 T-02 規約 8、json の表に `truncated_by_container`・`truncated_note`）、`docs/Lucianke アーキテクチャ設計書.md`（§7.2 の `Record`・形式ごとの見積もり、§7.4.4）。
- テスト:
  - `tests/unit/test_syntax_skeleton.py`: 1 件ずつ落ちる順（doc → 非公開 → 入れ子を後ろから）と注・入れ物ごとの件数・目安、TS の urn の段と json の注、`shrink`・`shrink_json` が予算に単調で収まること（5 通りの指定）、`shrink_json` の見積もりが数え直しと一致すること、入れ物の上限と `(top level)`。
  - `tests/unit/test_mcp_budget_monotone.py`: T-02 の骨格レコード 2 件をパッカーで予算を 7 ずつ上げて詰め、compact・json とも各ファイルのシンボル・doc: 行が減らず、縮退版でページを閉じるときの残りが 1 段未満（compact 40・json 260 トークン）であること。
  - `tests/unit/test_core_tokens.py`: `units` の行の足し算と JSON の配列の足し算（乱数）。
  - `tests/contract/test_T02_skeleton.py`: パイプライン経由で、予算を上げても各ファイルの中身が減らず縮退版のページが予算をほぼ使い切ること（compact・json）、注の目安で呼び直すと丸ごと返ること。`doc:` が 1 件ずつ入れ子から落ちること。
  - `tests/contract_fixtures/test_fx_read_T00_T08.py`: F-PY `_urls.py`・F-TS `Ky.ts` を丸ごとの 30〜90% の予算で呼び、利用率 85% 以上・シンボルが減らない・目安で丸ごと返る（json・compact）。

### 確認（2026-10-31）

`get_file_skeleton {"paths": ["httpx/_urls.py", "httpx/_api.py"], "max_tokens": N}`（F-PY。括弧は「返したシンボル数 / `truncated_members`」）。直す前は `208f4a0`、直した後はこの直し。

json:

| max_tokens | 直す前 items | 直す前 used（利用率） | 直した後 items | 直した後 used（利用率） | cursor |
|---|---|---|---|---|---|
| 2000 | `_urls`(3/50) | 618（31%） | `_urls`(14/39) | 1,935（97%） | あり |
| 2500 | `_urls`(3/50) | 618（25%） | `_urls`(18/35) | 2,390（96%） | あり |
| 3000 | `_urls`(3/50) | 618（21%） | `_urls`(22/31) | 2,888（96%） | あり |
| 3500 | `_urls`(3/50) | 618（18%） | `_urls`(27/26) | 3,495（100%） | あり |
| 4000 | `_urls`(3/50) | 618（15%） | `_urls`(31/22) | 3,950（99%） | あり |
| 4500 | `_urls`(3/50) | 618（14%） | `_urls`(35/18) | 4,431（98%） | あり |
| 5000 | `_urls`(3/50) | 618（12%） | `_urls`(39/14) | 4,930（99%） | あり |
| 5500 | `_urls`(3/50) | 618（11%） | `_urls`(43/10) | 5,401（98%） | あり |
| 6000 | `_urls`(3/50) | 618（10%） | `_urls`(48/5) | 5,967（99%） | あり |
| 6500 | `_urls`(3/50) | 618（10%） | `_urls`(52/1) | 6,423（99%） | あり |
| 7000 | `_urls`(53/0) | 6,806（97%） | `_urls`(53/0) | 6,816（97%） | あり |
| 7500〜8500 | `_urls`(53/0) | 6,806（91〜80%） | `_urls`(53/0) | 6,816（91〜80%） | あり |
| 9000 | 2 件 | 8,823（98%） | 2 件 | 8,844（98%） | なし |

- 6000 の注: `truncated: 5 members omitted (doc omitted): QueryParams 5; whole file: max_tokens>=6900 with this path alone`。`_urls.py` だけを 6900 で呼ぶと丸ごと（used 6,760）。
- 7000〜8500 で `_api.py` が入らないのは、`_api.py` が最上位の公開関数だけ（最も小さい段でも 2,033）で、規約 8 で最上位は落とさないため（変えていない）。丸ごとの項目は新しいキー（`truncated_by_container: []`・`truncated_note: null`）の分だけ 10 トークン増えた。

compact:

| max_tokens | 直す前 | 直す前 used（利用率） | 直した後 | 直した後 used（利用率） |
|---|---|---|---|---|
| 1500 | `_urls` 最上位だけ（50 省略） | 219（15%） | `_urls` 骨格 67 行（6 省略。QueryParams 6） | 1,478（99%） |
| 2000 | `_urls` 骨格 75 行（doc を全部省略） | 1,634（82%） | `_urls` 骨格 98 行（doc 7 件だけ省略） | 1,995（100%） |
| 2500・3000 | `_urls` 丸ごと | 2,073（83%・69%） | 同じ | 2,073（83%・69%） |
| 3500〜9000 | 2 件とも丸ごと | 3,183 | 同じ | 3,183 |

- 1000 の直した後: `_urls` 骨格 51 行（22 省略。QueryParams 22）、989（99%）。2500・3000 は `_api.py` の最も小さい段（最上位の公開関数だけ）が残りに入らない。
- テスト（`origin/main`〈`0fee57d`〉にこの直しを載せた作業ツリー）:
  - `uv run ruff check` → All checks passed
  - `tests/unit/test_syntax_skeleton.py`・`tests/unit/test_mcp_budget*.py`（4 本）・`tests/unit/test_core_tokens.py`・`tests/contract/test_T02_skeleton.py`・`tests/contract/test_T02_data_files.py` → 196 passed
  - `tests/contract_fixtures/test_fx_read_T00_T08.py` → 28 passed, 3 skipped（skip は F-TS の exact 水準 3 件。TypeScript の LSP が無い。BUG-0057。BUG-0074 の T-07 の 1 件も通った）
  - 関係する試験（`test_mcp_render`・`test_mcp_pipeline*`・`test_mcp_response_budget`・`test_syntax_adapters`・`test_compact_grammar`・`test_revision_reads`・`test_perf_reuse`・`test_T01_repo_map`・`test_T05_bodies`・`test_T10_edit_body`・`test_T10_delete`・`test_vertical_slice_stdio`・`test_import_graph` ほか 22 本）→ 588 passed（リベースの前）
  - 全体テストは親が main でまとめて走らせる（この役は走らせていない）。
- 見つけた別の粗さ（見たことだけ）: データファイル（JSON など。深さを 1 段ずつ浅くする縮退）も段が粗く、`ky` の `package.json` は予算 1,007 で 660（66%）→ 票 BUG-0084。
