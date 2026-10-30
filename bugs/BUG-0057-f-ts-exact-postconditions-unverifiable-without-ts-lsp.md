# BUG-0057 この環境では F-TS の exact 水準の事後条件を確かめられない（TypeScript の LSP が無い）

- 報告: 2026-10-29 / 見た人: BUG-0036 の担当 / 見たことだけ
- 見たこと:
  - `typescript-language-server` も `vtsls` も PATH・npm グローバル・ky の `node_modules/.bin` に無い（`where` で見つからない）。ネットワークは使わない方針。
  - F-TS（ky）の複製に対して `rig.lsp_ready` は false。T-04 事後条件 4、T-06 事後条件 1、T-07 事後条件 2、T-11 事後条件 2・3、T-13 事後条件 1〜3（適用するもの）は exact 水準が条件なので、tests/contract_fixtures/ は降格応答の約束（applied: false で何も変わらない、構造層の答え）だけを確かめている。
  - T-11 の存在しない対象は、LSP が無い言語では E_TARGET_NOT_FOUND ではなく E_UNSUPPORTED（details.reason = no_lsp_for_language）を返す（対象を探す前に断る）。
- 影響: M1-1 の F-TS 側は exact の事後条件が未検証。skips = "fail" のため skip にはしていない（条件分岐）。
- 直す役への手がかり（調査ではなく受入側の期待）: TypeScript の LSP を CI / この機械に入れる（`npm i -g typescript-language-server typescript`）か、bench/fetch_fixtures.py --install で ky の node_modules に入れて、tests/contract_fixtures が `lsp_ready` の分岐側を通ることを確かめる。

## 師匠の判断（2026-10-30）

- **本バージョンでは TypeScript（F-TS）の exact 検証を受入ゲートから外す（「検証ゲートのみ」ドロップ）。次バージョンで対応する。** TS は使用頻度が低いため。
- **TS の構文層・索引・編集のコードはそのまま残す**（将来入れるので捨てない。3 択の「コードごと撤去」は選ばない）。
- やること: exit_criteria.toml の F-TS 依存の検査から `fixture:F-TS` / TS LSP 前提を外し、F-PY のみ必須にする。契約テストの F-TS 分岐（降格応答の約束を確かめる現状）はそのまま残す（回帰防止）。
- この PC には node/npm が無いことを実機で確認（`node`/`npm` が bash・PowerShell の PATH にも、`C:\Program Files\nodejs`・`%APPDATA%\npm` にも不在）。ローカルでの TS LSP 導入はこの判断により不要。
