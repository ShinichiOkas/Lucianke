# BUG-0132 ワークスペースの根に basedpyright の設定が無いと、親のディレクトリの設定を採り、ワークスペースのファイルを解析の対象から外す（exact の答えが空になる）

見たことだけを書く（Lucianke の側の対処は調べていない）。2026-10-31、BUG-0129 の直す役が見た。

## 症状
- F-POLY（`tests/fixtures/poly`。`pyproject.toml` はあるが `[tool.pyright]` / `[tool.basedpyright]` が無く、`pyrightconfig.json` も無かった）を
  ワークスペースにして basedpyright 1.40.2 を起動すると、basedpyright は親のディレクトリの設定を採った。Lucianke のリポジトリの根に
  `pyrightconfig.json`（`include: ["src", "tests"]`・`exclude` に `tests/fixtures`）と `pyproject.toml` の `[tool.basedpyright]`
  （`include = ["src"]`）がある。
- その状態で、F-POLY の `src/shop/services/limits.py` の `LimitTable.get`（`src/shop/services/payment.py` の `_validate_limit` から呼ばれる）の
  `callHierarchy/incomingCalls` を basedpyright に直接送ると（Lucianke を通さない。`prepareCallHierarchy` は 3 ms）:

| 条件 | incomingCalls の所要 | 答え |
|---|---|---|
| リポジトリの中の F-POLY（設定の根は Lucianke のリポジトリ） | 120 秒で返らず | — |
| 同上、リポジトリの `pyrightconfig.json` だけ一時的に退けた（`[tool.basedpyright]` が残る） | 38.6 秒 | `null`（呼び出し元なし） |
| F-POLY をリポジトリの外にコピー | 0.33 秒 | `_validate_limit`（payment.py） |
| リポジトリの中の F-POLY に `pyrightconfig.json` を置いた（BUG-0129 の直し） | 0.24〜0.95 秒 | `_validate_limit`（payment.py） |

- basedpyright のコード（`dist/pyright-langserver.js` の設定の探索）に「pyproject.toml が見つかっても `[tool.pyright]` の節が無ければ、
  その pyproject.toml が無いものとして親のディレクトリを探す」という注がある。
- Lucianke はこの答えを `confidence_level: exact` で返す（F-POLY の T-07 は 10 秒の予算で切れていたので、`null` の答えが exact で
  返った呼び出しは見ていない）。

## 再現
1. 根に `pyrightconfig.json`（`include` が自分の下のディレクトリだけ）があるディレクトリの下に、`[tool.pyright]` の無い `pyproject.toml` を持つ
   小さな Python のプロジェクトを置く。
2. そのプロジェクトをワークスペースにして Lucianke の T-06 / T-07 を呼ぶ（または basedpyright に直接 `callHierarchy/incomingCalls`）。

## 影響しうる使い方
ワークスペースが、pyright の設定を持つ親（モノレポの根・ホームディレクトリの `pyrightconfig.json` など）の下にあるとき。

## 仕様書
§12.3（LSP）、§11 T-06・T-07。関連: BUG-0129（F-POLY だけはフィクスチャに設定を置いて直した）。
