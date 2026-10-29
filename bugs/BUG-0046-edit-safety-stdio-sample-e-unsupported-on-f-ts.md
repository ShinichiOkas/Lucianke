# BUG-0046 編集の構文妥当性ベンチの stdio 標本で、F-TS の操作 6/20 が E_UNSUPPORTED になる（プロセス内では成功）

- 報告: 2026-10-29 15:21 / 見た人: 編集の構文妥当性ベンチの役（22c）
- 見たこと:
  - `bench/run_edit_safety.py` の本計測（1,000 件、シード 20261029）で、同じ操作を実サーバー（stdio）にも投げて比べる標本 20 件のうち 6 件が一致しなかった。すべて F-TS（ky）の操作（恒等 replace_body 3、delete_symbol 2、insert_after 1）で、stdio 側が `E_UNSUPPORTED` を返した。プロセス内では成功した。F-PY の標本は一致した。
  - 当時はエラーのメッセージと details を記録していなかった（de03d22 で記録するよう台本を直した）。
  - 同じ ky の複製で `delete_symbol` を単発で stdio に投げると成功した。
  - 結果: `bench/results/20261029-151938-windows-edit-safety.json` の `stdio_sample.mismatches`。
- 再現手順: `uv run python bench/run_edit_safety.py --scale 0.3 --no-lsp`（約 2 分）で `stdio_sample.mismatches[].stdio.message/details` を見る。
- 調査はしていない（直す役の仕事）。

## 原因
- **再現できなかった。原因は断定できない。** 当時のエラーの `reason` / `message` は記録されておらず（de03d22 より前）、その後のベンチ 2 回（規模 0.3 と 1.0）では F-TS の stdio 標本が全部一致し、単発の再現も 3 つの負荷条件で失敗しなかった（下の「確認」）。
  - 当時の実行は、他の役が共有の作業ツリーで十数本並行していて、プロセス内の 1,000 件が 451 秒（今は 82 秒）かかる負荷だった（`bench/results/20261029-151938-windows-edit-safety.json` の `timing` と `load`）。stdio のサーバーは**ライブの作業ツリー**から `python -m lucianke` で起動するので、起動した瞬間の他の役の書きかけの状態を読み込んだ可能性もある（F-PY のサーバーは約 1 分前に起動して一致、F-TS のサーバーだけが 6/6 失敗。1 つのサーバーは import した版で最後まで動くので、「そのサーバーを起こした瞬間の木」が原因なら 6 件が全部失敗することと合う）。推測であって、確かめていない。
- 確かめたこと: T-10 の経路で `E_UNSUPPORTED` を返す箇所のうち、実行時の状態（木ではなく）で決まり、かつ「待てば直る」のは `WiredServices.edit_transaction` の `index_not_open`（束がまだ開いていない）だけ。入口の `ready_gate`（`hub.wait_started`、上限 10 秒）が最初の `sync_roots` の完了を待つが、上限を超えると素通しし、そこで `E_UNSUPPORTED`（仕様書 §5.8 では「その操作を提供しない」）が返っていた。待てば直る状態を「未対応」と返すのは利用者に不親切（再試行すべきか分からない）。
  - 起動直後に F-TS（ky）へ T-10 を投げる再現を、負荷なし・10 プロセス並行・CPU 32 本飽和（待ち 0 秒）で各 2〜3 回回したが、`index_not_open` にはならなかった。

## 直し方
- 当時の失敗が `index_not_open` だったかは不明だが、この経路で確実に直せる誤分類を直した（`src/lucianke/tools/_runtime.py` の `WiredServices.edit_transaction`）:
  - 束の表（`hub`）があり最初の `sync_roots` がまだ → `E_TIMEOUT`（`details.reason="index_opening"`、`retry_after_ms=1000`）。「待てば直る・再試行する」と分かる。
  - 束を開けなかった（`hub.failed`）・`hub` が無い → 従来どおり `E_UNSUPPORTED`（`index_not_open`）。開けなかった理由を `details.open_error` に載せ、次に起きたとき応答だけで原因が分かるようにした。
- 回帰テスト: `tests/e2e/test_edit_cold_start_stdio.py`（実プロセスを起こし、最初の呼び出しから TS の `delete_symbol`〈dry_run〉。成功するか、失敗するなら `E_TIMEOUT`/`index_opening` で再試行して成功する。`E_UNSUPPORTED` は不可。条件待ち）、`tests/unit/test_tools_runtime.py`（未開始は `E_TIMEOUT`、開けなかったときは `E_UNSUPPORTED` + `open_error`）。
- **未解決**: 当時の失敗が `index_not_open` 以外（作業ツリーの途中状態など）だった場合、これでは直らない。再発したら `bench/results/*-edit-safety.json` の `stdio_sample.mismatches[].stdio.message/details`（de03d22 で記録される）に `reason` が出るので、それで確定する。

## 確認
- `bench/run_edit_safety.py --scale 0.3 --no-lsp` が `stdio_ok=True`（verdict=pass、298/298）、既定の規模（1,000 件、シード 20261029）`--no-lsp` も `stdio_ok=True`（1000/1000、ep6 300/300）。stdio 標本は 20/20 一致。
- `tests/e2e`（58 件のうち T-10 の契約テストを含む）と `tests/unit/test_tools_runtime.py` が green、`ruff check` が green。
