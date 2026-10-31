# BUG-0119 圧縮済みの JS（`*.min.js` の 1 行 数 MB）を構文解析して、1 ファイルに数十秒かけ、1〜2 文字の名前のシンボル・関係を数千〜数万件作る

- 報告: 2026-10-31 / 見た人: BUG-0112 の直す役（Mnemo の能力測定の基準のワークスペースを索引したとき。BUG-0112 の直した後の版で測った）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（BUG-0112 の修正後のソース、`Indexer._prepare` を 1 ファイルずつ、この PC）

| ファイル | 大きさ | 取り込みの時間 | シンボル | 関係 |
|---|---|---|---|---|
| `vendor/plotly/plotly.min.js` | 4.56 MB（ほぼ 1 行） | 46 秒 | 6,869 | 41,160 |
| `companions/.../vis-network.min.js` | 689 KB | 3.5 秒 | 314 | 1,972 |
| `mnemo_gui/assets/katex/katex.min.js` | 272 KB | 1.4 秒 | 190 | 612 |

- ワークスペース全体（約 4,200 ファイル）の初回の索引 約 250 秒のうち、`plotly.min.js` 1 本で約 46 秒。
- そのワークスペースの `.gitignore` は `vendor/` を除いていない。Lucianke は `*.min.js` を他の JS と同じに扱う（`MAX_FILE_BYTES` = 5 MB 未満なので構文木を作る）。
- 作られるシンボルの名前は `e`・`t`・`r` のような圧縮後の名前（例: plotly の 41,160 件の関係の大半）。

## 利用者（呼ぶ側の AI）として困ること

- 名前で引く検索（T-03）・名前の一致（`_name_calls`）の候補に、意味の無い 1〜2 文字の名前が大量に混ざる。
- 初回の索引の時間の多くを、読む必要の無いファイルに使う。

## 原因・直し方・確認（2026-10-31）

- 原因: 取り込み（`Indexer._prepare`）が JS / TS を名前・形に依らず構文解析していた（構文木を作らないのは秘密・バイナリ・`MAX_FILE_BYTES` 超えだけ）。
- 直し方: JS / TS のうち、名前が `*.min.js` / `*.min.mjs` / `*.min.cjs`（`MINIFIED_SUFFIXES`）のもの、または 32 KB 以上（`MINIFIED_MIN_BYTES`）で 1 行の平均が 300 バイト以上（`MINIFIED_AVG_LINE_BYTES`。手で書いたコードは 30〜50）のものを、構文木を作らず `files` 行だけ（`lexical_fallback`。行数・大きさ・`sha256` は残す）にする（`_is_minified`）。ツールがそのファイルを直接開く（T-02 など）ときは今までどおり解析する。仕様書 §12.4 項 5、設計書 §7.6.2 に理由つきで書いた。
- 確認: 単体 `tests/unit/test_engine_indexer_minified.py`（名前の `vendor/lib.min.js`・約 60 KB の 1 行の `dist/bundle.js` は `lexical_fallback` でシンボル 0、同じ中身で行の普通な `dist/plain.js` は 1,999 シンボル、手で書いた JS・Python は今までどおり）。BUG-0112 の e2e は、JS を 1 行 1 関数（約 1 MB・100 KB の 1 行を含む）にして解析させるよう直した。
