# ベンチのフィクスチャ候補（OD-10、仕様書 §14.2 / §17）

実測日 2026-10-29。`git ls-files` の `.py` / `.ts`+`.tsx` の物理行数（空行・コメント込み）。テスト = パスに test / spec を含むもの。
取得は `uv run --no-project python bench/fetch_fixtures.py`（`bench/.cache/<名前>/` に SHA 固定で浅く clone。`bench/.cache/` は git 管理外）。
仮置きは `bench/fixtures.toml`（`status = "provisional"`）。師匠が選んだら `decided_by` と `status = "fixed"` に直す。

## F-PY（Python、目安 1 万〜5 万行、テストと pyproject.toml）

| 候補 | SHA（固定） | ソース | テスト | 合計行 | ライセンス | メモ |
|---|---|---|---|---|---|---|
| **encode/httpx（推奨）** | `b5addb64f0161ff6bfe94c124ef76f6a1fba5254`（2026-02-23） | 23 ファイル / 8,827 行 | 37 ファイル / 8,926 行 | 17,753 | BSD-3-Clause | 型注釈が全面（mypy strict）。同期 + 非同期の対、`_client.py` 2,019 行・`_models.py` 1,277 行と大きなファイルがあり、継承・オーバーロード・プロトコルがある。`tests/` と `pyproject.toml` あり。MCP SDK の依存先で、Lucianke 自身の仲間（Python の I/O ライブラリ）に近い |
| pallets/click | `2247b35ea1c47c727d7a06e51fa280e12a863ff6`（2026-10-04） | 31 ファイル / 13,286 行 | 60 ファイル / 17,092 行 | 30,378 | BSD-3-Clause | `core.py` が数千行の巨大ファイル。docstring が厚く S1 の Doc 要約が映える。基準値 B が大きく出やすい（削減率に有利） |
| encode/starlette | `8aa828ceaff764a2e57932fb42fdf105a47c4c83`（2026-10-24） | 47 ファイル / 7,932 行 | 37 ファイル / 14,749 行 | 22,681 | BSD-3-Clause | ASGI フレームワーク。ファイルが小さめでテストが多い（S2 の「呼ぶテスト」が豊富）。基準値 B は小さめ |

見送り（範囲外、参考）: rich 51,866 行（上限超）、black 141,148 行、flask 18,333 行、attrs 21,026 行（適格だが上位 3 に入れず）。

## F-TS（TypeScript、同様）

| 候補 | SHA（固定） | ソース | テスト | 合計行 | ライセンス | メモ |
|---|---|---|---|---|---|---|
| **sindresorhus/ky（推奨）** | `3541888878c4d303a8bc2ad61815f4e048f55a5d`（2026-10-09） | 31 ファイル / 4,917 行 | 67 ファイル / 31,314 行 | 36,231 | MIT | httpx と同じ種類（HTTP クライアント）で F-PY と対になる。`source/core/Ky.ts` 1,376 行。`tsconfig.json`・`test/`（ava）・`test-d/`（型テスト）あり。型解決に `npm ci` が要る（`fetch_fixtures.py --install`） |
| h3js/h3 | `f0e17808ac2439b6434bb10609a961067f4d555b`（2026-10-22） | 91 ファイル / 14,951 行 | 88 ファイル / 30,065 行 | 45,016 | MIT | サーバーフレームワーク。ソースが大きく B が出やすい。上限 5 万行に近い。pnpm・TypeScript 7 系で環境が重い |
| typestack/class-validator | `2e1a5c27dbd65b80e27fe96b49bd6e6641fa3603`（2026-02-26） | 160 ファイル / 6,450 行 | 16 ファイル / 8,005 行 | 14,455 | MIT | デコレータ中心でクラスが多い。ファイルが細かく B が小さい。テストが少なく S2 の「呼ぶテスト」が薄い |

見送り: zustand 9,390 行（下限未満）、jotai 19,249 行、zod 102,861 行・hono 92,389 行・typebox 115,452 行・remeda 70,474 行・got 66,103 行（上限超）、ofetch・class-transformer（小さすぎ）。

## 選ぶのは師匠（1 行の問い）

「F-PY は httpx、F-TS は ky（どちらも HTTP クライアントで対）を既定にしてよいか。別にするなら、Python は click（B が大きく出る巨大ファイル）、TS は h3（サーバー系）が次点」。答えが無ければ推奨（httpx + ky）で進める。

## 留意（台本の設計に効くこと）

- 推奨の 2 件はどちらも「型エラーが 0 の健全なリポジトリ」で、S4（型エラーを 1 件直す）には既存の型エラーが無い。台本は、固定の 1 行変更（型エラー注入）を `bench/scenarios/` に持ち、注入後の作業コピーに対して B・L を測る。
- 基準値 B はソースの大きさに強く依存する（grep のヒットしたファイルは全文を読むため）。大きなファイルの多いフィクスチャほど有利になる。判定に偏りが出ないよう、推奨は「中くらいの規模で、大小のファイルが混ざる」ものにした。
