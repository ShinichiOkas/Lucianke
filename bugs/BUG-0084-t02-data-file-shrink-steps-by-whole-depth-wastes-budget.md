# BUG-0084 T-02 のデータファイル（JSON など）の縮退が深さ 1 段ずつで粗く、予算の 3〜5 割を使わずに返す

- 報告: 2026-10-31 / 見た人: BUG-0078 を直した役（コードの骨格の段を細かくした後、データファイルも同じか測った）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `208f4a0` + BUG-0078 の直し。`get_file_skeleton {"format": "json", "paths": [<path>], "max_tokens": N}`）

F-TS の複製元 `bench/.cache/ky` の `package.json`（丸ごとの応答 1,679 トークン）:

| max_tokens | used_tokens_est | 利用率 | `truncated_members` |
|---|---|---|---|
| 335 | 156 | 47% | （項目 0 件。警告） |
| 671 | 659 | 98% | 45 |
| 1007 | 660 | 66% | 45 |
| 1343 | 1317 | 98% | 14 |
| 1595 | 1317 | 83% | 14 |

- 段は 1,679（丸ごと）→ 1,317（14 落とす）→ 659（45 落とす）と、間が無い（`truncated: N members omitted (depth limited to D)`）。
- `tests/fixtures/tokens/manifest.json`（丸ごと 502）は 200〜401 で項目 0 件と警告（`used` 154）。

## 利用者（呼ぶ側の AI）として困ること

- BUG-0078 と同じ: 渡した予算を使い切らずに返し、残りを見るには呼び直しが要る。コード（Python・TS・JS）の骨格は BUG-0078 で 1 件ずつ落とす段になったが、データファイル（`syntax/adapters/_common.py` の `build_record` の `shrink`。深さを 1 段ずつ浅くする）は粗いまま。
- 縮退版の注に、丸ごと取り直す `max_tokens` の目安が無い（コードの骨格には BUG-0078 で付けた）。

## 再現

`tests/contract_fixtures/_rig.py` の F-TS の複製、または `bench/.cache/ky` をルートにして、上の呼び出しを `max_tokens` 335・671・1007・1343・1595 で行う。

## 関連

- BUG-0078（コードの骨格の段を 1 件ずつに。仕様書 §11 T-02 規約 8）、BUG-0067（単調性）、仕様書 §11 T-02 規約 4・5（`max_depth`）

## 原因・直し方・確認（2026-10-31）

### 原因

- データファイル・Markdown・Tier 4 のアウトラインの縮退（`syntax/adapters/_common.build_record` の `shrink`）は、深さ `d` を `depth - 1` から 1 まで 1 段ずつ浅くして最初に収まる版を返すだけだった。段の大きさは「ある深さより深いノードを全部」で、`package.json` のように浅い階層に項目が多い（`scripts`・`dependencies` に各数十件）ファイルは、段の間が 1,679 → 1,317 → 659 と空く。
- 注も `- truncated: N members omitted (depth limited to D)` だけで、どこを落としたかも、丸ごと取り直す `max_tokens` の目安も無かった（コードの骨格は BUG-0078 で両方付けた）。
- json の縮退は `Record.shrink_json` が無く、パッカーが compact の行の大きさで `ask` を 16 回二分探索していた。

### 直し方（決めたこと）

- **段を 1 件ずつに**（`_bfs_rank`・`_prune`）: 表示しているノードに「浅い順 → 文書順」の連番を振り、後ろから 1 件ずつ落とす（最上位は残す。親は子より先なので落とした先頭 `V − t` 件は常に木）。落としたノードの親の `{ ...N nodes omitted }` に子孫の数を足す。段の鎖は予算に依らず、`shallowest` が二分探索するので結果は予算に単調（BUG-0067）。
- **注をコードの骨格と揃える**: `- truncated: N members omitted: <入れ物> <件数>, …; whole file: max_tokens>=M with this path alone`（入れ物は親の修飾名を `.` でつないだもの。8 件まで、残りは `(others)`。`N` は予算で落とした表示中のノードの数で、`max_depth` で畳んだ分は含めない〔以前は含めていた〕）。json は `truncated_by_container[]`・`truncated_note` も返す。目安 `M` は compact は行、json は data の JSON の大きさ + 200 を 100 単位に切り上げ。
- **`shrink_json`**（`Record.shrink_json`。既定の推定器のときだけ）を足し、json は data の JSON の大きさで段を直接選ぶ。`tools/_skeletons._retag`（Tier 4）も `shrink_json` を付け替える（`dataclasses.replace` が元の `shrink_json` を残していた）。
- 仕様書 §11 T-02 規約 8、設計書 §7.4.4（予算内での縮退）に書いた。

### 確認

- F-TS の `bench/.cache/ky/package.json`（json 丸ごと 1,687 トークン）:

  | max_tokens | 直す前 | 直した後 |
  |---|---|---|
  | 1007 | 660（66%・45 落とす） | 996（99%・31 落とす） |
  | 1343 | 1317（98%・14） | 1334（99%・16） |
  | 1595 | 1317（83%・14） | 1587（99%・6） |
  | 760 | 659 | 758（100%・45） |
  | 671〜740 | 659（98%） | 1 件も返らない（警告） |

  最も小さい版（最上位だけ）の json が、`truncated_by_container` と `truncated_note` の分だけ約 660 → 約 750 トークンに増えた（コードの骨格と同じ欄）。この帯の予算では以前は最上位だけが返ったが、今は 1 件も返らず警告が `max_tokens` を勧める。
- 新しい試験 `tests/contract/test_T02_data_shrink.py`（5 件）: package.json 風のファイルで予算を掃引し、compact・json とも利用率 9 割以上・予算に単調・注の形（`whole file: max_tokens>=` で取り直すと丸ごと）・json の `truncated_by_container` の和が `truncated_members`。直す前は 4 件が赤（compact の 783 で 189 トークン、など）。単体の `test_syntax_adapters.py::test_shrink_drops_one_member_at_a_time_deepest_and_last_first` と契約 `test_T02_data_files.py::test_shrink_reduces_depth_and_says_so` を新しい注に直した。
