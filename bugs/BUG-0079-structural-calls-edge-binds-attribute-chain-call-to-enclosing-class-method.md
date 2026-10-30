# BUG-0079 構造層の CALLS が、属性を辿った受け手の呼び出し `url._uri_reference.copy_with(...)` を、囲みのクラスの同名メソッド `URL.copy_with` に結び付ける（LSP は結び付けない）

- 報告: 2026-10-31 / 見た人: BUG-0074 を直す役（F-PY の契約テストの調査中に見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `208f4a0`〈BUG-0071 の d5e8614 を含む〉+ BUG-0074 の直し。F-PY = httpx、`bench/fixtures.toml` の SHA）

- `httpx/_urls.py:117-119`（`URL.__init__` の中）:

  ```python
          if isinstance(url, str):
              self._uri_reference = urlparse(url, **kwargs)
          elif isinstance(url, URL):
              self._uri_reference = url._uri_reference.copy_with(**kwargs)
  ```

  `copy_with` の定義は `httpx/_urls.py:327`（`URL.copy_with`）と `httpx/_urlparse.py:186`（`ParseResult.copy_with`）の 2 つ。
  `urlparse` は `httpx/_urlparse.py` の関数。
- 索引（`query_project_db`）の `relations` に、次の行がある:

  ```
  source_urn = lk://httpx/httpx/_urls.py#URL.__init__
  target_urn = lk://httpx/httpx/_urls.py#URL.copy_with
  call_site_line = 119, call_site_col = 54, relation_kind = CALLS, confidence_level = structural
  guard_condition = !(isinstance(url, str)) && isinstance(url, URL)
  ```

- 同じワークスペースで LSP（basedpyright）の `callHierarchy/incomingCalls`（`URL.copy_with`）は 21 件。`httpx/_urls.py` からは
  `copy_set_param`（343）・`copy_add_param`（346）・`copy_remove_param`（349）・`copy_merge_params`（352）だけで、119 行は入っていない。
- `query_call_hierarchy {"target": {"path": "httpx/_urls.py", "symbol_name": "URL.copy_with"}, "direction": "incoming", "depth": 3}`:
  - `exact` で答えたとき（LSP が予算内に答えたとき）: 辺 56 本。119 行の辺は無い。
  - `structural` に降格したとき（LSP の予算〈`EXACT_BUDGET_S`〉を使い切ったとき。負荷の高い全体テストで起きた。`CallEdgeSource` の
    `budget_s` を 0 にすると毎回起きる）: `URL.__init__ @httpx/_urls.py:119  when: !(isinstance(url, str)) && isinstance(url, URL)` が
    `URL.copy_with` の呼び元として出る。

## 再現手順

1. F-PY（httpx）を索引する（`tests/contract_fixtures` の `rig` と同じ）。
2. `query_project_db` で `SELECT r.* FROM relations r JOIN files f ON f.file_id = r.source_file_id WHERE f.path = 'httpx/_urls.py' AND r.call_site_line = 119 AND r.relation_kind = 'CALLS'` → 上の行が出る。
3. LSP を使わせずに（`structural`）`query_call_hierarchy` を上の引数で呼ぶ → `URL.__init__` が呼び元に出る。

## 利用者（呼ぶ側の AI）として困ること

- 降格した応答で、実際には `ParseResult.copy_with` を呼んでいる箇所が `URL.copy_with` の呼び元として返る。`URL.copy_with` を変えたときの
  影響範囲に、関係のない `URL.__init__` が入る。`exact` で答えたときと呼び元の集合が変わる。
