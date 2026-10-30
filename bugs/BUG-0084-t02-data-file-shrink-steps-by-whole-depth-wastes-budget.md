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
