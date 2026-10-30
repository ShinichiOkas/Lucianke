# BUG-0068 トークン計数の較正テストが md_ja・py_src・py_tests の 3 種で skip（記録と一致する標本が少ない）＝ §5.7 の誤差 ±10% を検証していない

- 報告: 2026-10-31 / 見た人: BUG-0065 を直した役（全体テストを流したとき）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 症状

`uv run pytest -q -p no:cacheprovider -rfs`（`9d96cd2` + BUG-0065 の直し、2026-10-31）: `3783 passed, 32 skipped`。skip の 32 件のうち 3 件が次のもの。

```
SKIPPED [1] tests\unit\test_core_tokens_calibration.py:142: md_ja: 記録と一致する標本が少ない（書き換わった）。calibrate.py で result.json を更新する
SKIPPED [1] tests\unit\test_core_tokens_calibration.py:142: py_src: 記録と一致する標本が少ない（書き換わった）。calibrate.py で result.json を更新する
SKIPPED [1] tests\unit\test_core_tokens_calibration.py:142: py_tests: 記録と一致する標本が少ない（書き換わった）。calibrate.py で result.json を更新する
```

- skip しているのは `test_total_error_within_ten_percent_per_kind`（種類ごとの推定誤差が ±10% 以内か）。日本語の Markdown・`src` の Python・`tests` の Python の 3 種で、仕様書 §5.7 の「誤差 ±10% 以内」を確かめていない。
- skip の理由の文は「calibrate.py で result.json を更新する」と手順を書いている。
- BUG-0065 を直す前の全体テスト（BUG-0065 の票。`32 skipped`）でも skip の件数は同じ 32 件だった（内訳は見ていない）。

## 再現

```powershell
uv run pytest -q -rs tests/unit/test_core_tokens_calibration.py
```

## 原因・直し方・確認（2026-10-31）

- 原因: テストの標本が **リポジトリの生のファイル**（`src/**/*.py`・`tests/**/*.py`・`docs/*.md`・`docs/spikes/*.md`・仕様書の例）で、基準値（`o200k_base` のトークン数）は S-12 の `bench/tokens/result.json`（`c3901c3`、2026-10-29）に記録したもの。テストは生のファイルの文字数が記録と一致するものだけを使い、照合できた基準トークンが記録の 50% 未満の種類は skip する作りだった（「書き換わった種類は判定しない」を設計として持っていた）。記録の後、`src/` は 124 コミット・仕様書は 19 コミット書き換わり、`c149e0d` で照合できたのは次のとおり（いずれも 50% 未満）:
  - py_src: 記録 71 ファイル・230,230 トークンのうち 14 ファイル・27,554（12%）。今は 136 ファイル
  - py_tests: 記録 48 ファイル・206,505 のうち 20 ファイル・82,841（40%）。今は 134 ファイル
  - md_ja: 記録 3 ファイル・194,390 のうち 1 ファイル・29,181（15%。仕様書と設計書が外れた）
  - 標本全体の空洞化の検査（照合できた基準 5 万トークン以上）は残りの種類の分で通っていたので、全体としては緑のまま 3 種の検証だけが抜けた。標本が生のファイルである限り、`calibrate.py` で `result.json` を更新しても次の編集で（このテスト自身を直すだけでも py_tests が）また外れる。
- 直し方（基準の作り方は変えず、標本を凍結した）:
  - `bench/tokens/freeze_corpus.py`（新規）: `calibrate.py` の `load_corpus` から種類ごとに標本を選び、本文を `tests/fixtures/tokens/<種類>/<番号>.txt` にバイト列で写し、`o200k_base` のトークン数・SHA-256・文字数を `tests/fixtures/tokens/manifest.json` に書く。大きい種類は文字数の上限まで（py_src・py_tests 10 万字、md_ja 8 万字、md_spikes 4 万字。8,000 字を超える本文は行の境目でほぼ等分してから、名前のハッシュ順に選ぶ。内容で選り好みしない）、ほか（poly・compact・spec_json_sql・合成の base64・hex）は全部。118 標本・基準 149,748 トークン・486,725 バイト（出どころ `c149e0d`）。tiktoken 0.14.0 は手元で使えた（初回の語彙の取得にネットワークが要る。テストは要らない）。
  - `tests/unit/test_core_tokens_calibration.py`: 生のファイルと `result.json` を読むのをやめ、凍結した標本だけを読む。skip を全部なくした。凍結した本文が manifest と食い違う（SHA-256・文字数）・ファイルが無い・manifest に無いファイルがある・必須の 12 種類（`REQUIRED_KINDS`）が欠ける・基準が 5 万トークン未満、のどれでも **失敗** にし、作り直しのコマンドを出す。種類ごとの検査は `REQUIRED_KINDS` で固定（標本から種類が消えたら黙って検査が減るのではなく赤）。陽性対照は「どれか 1 種が落ちる」から「md_ja と syn_base64 の両方が落ちる」に強めた。推定式（`core/tokens.py`）を変えても基準値は変わらないので凍結し直さない。
  - `bench/tokens/result.json` は S-12 の記録としてそのまま残した（`calibrate.py` を流し直すと候補式の当てはめ・提案まで書き換わり、S-12.md の数字と食い違うため）。`calibrate.py` の説明・設計書 §7.2.1・`docs/spikes/S-12.md` の「反映」に凍結の方式を書いた。`core/tokens.py` の係数は変えていない。
- 判断と理由（役が決めた。師匠に戻す問いは無い。基準は「主な使い手の AI エージェントに役立つか」〈師匠の判断 2026-10-31〉）:
  - 生のファイルの照合をやめて凍結した: 推定が過小だと応答が `max_tokens` を超え、使い手の AI が予算を守れない（R-12）。その守りが開発の書き換えで黙って抜けるのは使い手にとって最悪なので、検証は書き換えに依らず毎回走り、崩れたら赤になる形にした。生のファイルの SHA-256 を記録する案は、`src/` を 1 行直すたびに赤になる（毎回作り直しが要る）ので採らなかった。
  - 全部ではなく部分集合を写した: 生のコーパスは今 420 万字（UTF-8 で約 8 MB）あり、リポジトリ（pack 3.3 MiB）に対して重すぎる。部分集合（約 0.5 MB）でも種類ごとの誤差が全体と ±2 ポイント以内で一致することを上の数字で確かめた。選び方は名前のハッシュ順（内容で選り好みしない）。
  - `result.json` は書き換えなかった: 回帰テストに要るのは `o200k_base` の基準値だけで、`calibrate.py` を流し直すと S-12 の候補式・提案まで変わり記録が崩れる。基準値は凍結の manifest に持たせた。
  - 閾値は ±10% のまま（緩めない）。今回は全種類が収まったので精度の票は無い。もし将来 ±10% を外れる種類が出たら、閾値でなく推定式（係数）を直す（過小は応答のあふれ、過大は使える予算の無駄で、どちらも使い手の AI に害がある）。
- 推定の誤差（凍結した標本、`lean_wide`。合計の誤差 / 標本単位の平均絶対誤差。±10% 以内が合格）: compact −0.9% / 5.5%、md_ja −1.4% / 1.9%、md_spikes −1.0% / 2.2%、poly_code −1.1% / 3.3%、poly_data +1.0% / 1.3%、poly_doc +2.4% / 5.1%、poly_ipynb −3.6% / 3.6%、py_src −0.4% / 2.3%、py_tests +0.1% / 3.2%、spec_json_sql −2.9% / 5.3%、syn_base64 −8.3% / 8.3%、syn_hex +7.7% / 7.7%。全体 −1.2%。compact を 1 行ずつ数えて足すと −1.7%。**全種類合格**（±10% を超える種類は無いので、精度の票は書いていない。base64 の −8.3% は S-12 から既知で変わらず）。
  - 凍結の前の生のコーパス全体（ファイル単位、`c149e0d`）でも全種類合格: py_src −1.0%（124 ファイル）、py_tests −0.5%（134）、md_ja +0.2%（4）、md_spikes −2.7%（16）。凍結した部分集合は全体と ±2 ポイント以内で一致する。
  - 旧式（陽性対照）は md_ja −17.8%、md_spikes −21.2%、compact −11.3%、py_src +12.2%、ipynb −43.1%、base64 −62.5%、hex −57.2% で落ちる。
- 確かめた「古い記録は大きく落ちる」: 凍結した 1 標本に 1 バイト足す・py_src の標本を丸ごと消す・manifest に無いファイルを置く、の 3 通りでそれぞれ 2 failed・skip 0（戻すと 16 passed）。
- 確認（作業ツリー = `c149e0d` + この直し）:
  - `uv run ruff check src tests` → All checks passed
  - `uv run pytest -q -rs tests/unit/test_core_tokens_calibration.py` → **16 passed・skip 0**（直す前は 13 passed・3 skipped）
  - `uv run pytest -q tests/unit/test_core_tokens_calibration.py tests/unit/test_core_tokens*.py tests/unit/test_mcp_budget*.py` → 125 passed
  - 全体: `uv run pytest -q -p no:cacheprovider -rfs` → **3788 passed, 29 skipped, 1 failed**（902 秒）。skip は 32 → 29 で、減った 3 件がこの票の較正の 3 件（残りは TypeScript の LSP なし・POSIX のみ・symlink 権限などの環境由来）。1 failed は `tests/unit/test_import_graph.py::test_dependency_directions`（`cli/selftest.py` が合成根 `bootstrap_io` を import。`4ba7c0a` の exe 配布で入ったもので、この直しは `src/` を触っていない。単独でも `c149e0d` のまま落ちる）。BUG-0066 の役が票 BUG-0072 にしている。
- 別の票: CI と同じ `uv run ruff check`（引数なし）が bench の 2 本の台本で 4 件落ちる（BUG-0073。この直しとは無関係）。
