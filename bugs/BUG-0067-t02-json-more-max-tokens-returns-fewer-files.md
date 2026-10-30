# BUG-0067 get_file_skeleton の json で max_tokens を上げると返るファイル数が減る（F-PY: 6000 で 2 件、8000 で 1 件）

- 報告: 2026-10-31 / 見た人: BUG-0065 を直した役（F-PY の契約テストが 1 件しか返らない理由を測ったとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `b1ce8ac` + BUG-0065 の直し。F-PY〈httpx〉・`format: "json"`）

`get_file_skeleton {"paths": ["httpx/_urls.py", "httpx/_api.py"], "max_tokens": N}`:

| max_tokens | 返った items | `_urls.py` の `truncated_members` | `used_tokens_est` | `next_cursor` |
|---|---|---|---|---|
| 6000 | `_urls.py`・`_api.py` の 2 件 | 50（縮退） | 2,637 | なし |
| 8000 | `_urls.py` の 1 件だけ | 0（丸ごと） | 6,806 | あり（`_api.py` は次ページ） |
| 12000 | 2 件 | 0 | 8,824 | なし |

- 各ファイル単独（`max_tokens=50000`）の json の量: `_urls.py` 6,751、`_api.py` 2,199 トークン。
- 予算を 6000 → 8000 に上げると、返るファイルが 2 件 → 1 件に減り、6000 で返っていた `_api.py` が次ページに回る。
- BUG-0055 の省き方（`1e1a9aa`）を入れていたときは、同じ 2 ファイルで 6000 が 1 件・8000 が 2 件だった（量が変わると、どの予算で減るかが入れ替わる）。

## 手がかり（見たことだけ）

- 仕様書 §11 T-02 処理規約 8: 「予算に収まらないとき、ファイル内では `doc:` → `urn:` → 非公開メンバー → 入れ子メンバー（最上位は残す）の順に落とし、落とした件数を `- truncated: N members omitted` に書く。それでも収まらなければ残りのファイルを次ページに回す。」
- `tests/contract_fixtures/test_fx_read_T00_T08.py::test_t02_postcondition_1_2_5_skeleton_matches_the_source` は `max_tokens=6000` で 2 ファイルとも 1 ページに返ることを求めている。今これが通るのは、`_urls.py` が単独で 6000 に入らず縮退するから（BUG-0065 の票）。
- compact で同じことが起きるかは測っていない。

## 再現

上の呼び出しを `max_tokens` 6000・8000・12000 で、F-PY のワークスペース（`bench/.cache/httpx` の複製。`tests/contract_fixtures/_rig.py` の `open_rig("F-PY", …)`）に対して行う。
