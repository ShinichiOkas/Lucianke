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

## 原因・直し方・確認（2026-10-31）

BUG-0081 と一緒に直した（どちらも抽出器 `syntax/relations.py` の `_resolve_member` の受け手の決め方）。

### 原因

- 抽出器の `_resolve_member` は、受け手の型が分からないとき、最後の手段として「同じファイルの定義の修飾名の末尾一致（一意のとき）」と
  「import した同名」に当てていた。受け手が `url._uri_reference` のような **式** でもそうしていた。`_type_ref` は `a.b` の型を、`a` が外部の
  モジュール・クラスのときしか決めなかった（`url: URL` でも `url._uri_reference` の型は `None`）。httpx の `_urls.py` には `.copy_with` が
  `URL.copy_with` しか無いので、`URL.__init__` の中の `url._uri_reference.copy_with(**kwargs)` が `URL.copy_with` に結ばれた
  （囲みの関数自身〈`URL.__init__`〉だけを除いて数えていた）。`type(url)` に畳んだのではなく、受け手が分からないまま同じファイルの同名に当てていた。
- 同じ当て方で、`base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")`（`bytes.decode`）が同じファイルの `CursorCodec.decode` に、
  `h.docs.reset()` が `LspPool.reset` に、`super().close()` が同じファイルの別のクラスの `close` に結ばれていた（下の確認）。

### 直し方（決めたこと）

- **受け手が式で型が分からなければ、同じファイルの同名にも import した同名にも当てない**（`_resolve_member`）。`a.b.m()`・`self.x.m()`・
  `f().m()`・`x[0].m()` はどれも `name_only`（`_name_calls`）にして、問い合わせのときの名前の一致（BUG-0071・BUG-0077 の近さ）に任せる。
  名前の受け手（`x.m()`）の末尾一致は今回は変えていない（別の票 BUG-0085）。
- **`a.b` の型は構文から分かるときだけ決める**（`_type_ref`・`_attr_type`）: `a` の型が同じファイルのクラス（`self` の囲みのクラスを含む）で、
  `b` がその注釈つきのクラス属性・インスタンス属性（`self.b: T = ...`・dataclass のフィールド）・`self.b = T()`・戻り値の注釈つきの
  property（`@property` / `@cached_property`。TS は getter・フィールド・パラメータプロパティ）のとき。同じファイルの基底も C3 の順に見る
  （別ファイルの基底・中身の分からない外部の基底に先に当たったら分からないとする）。別ファイルのクラスの属性の型は分からないとする。
  外部のモジュール・クラスの属性の連鎖（`os.path.join`）は今までどおり。
- TS / JS の `this.x.m()` も同じ規則（`this.x` の型はフィールド・パラメータプロパティ・`this.x = new T()`・getter）。
- スキーマ v7（BUG-0081 と共通）の移行で Python / TS / JS のファイルを取り込み直させる（既にある誤った行は取り込み直さないと消えない）。
- 仕様書: T-06 処理規約 9 (a)・T-07 処理規約 11（理由を 1 行添えた）。設計書 §7.6.2 に 1 文。

### 確認

Lucianke 自身（`origin/main` = `8a72e31` のスナップショットを、修正前のコード〈`origin/main` の `src`〉と修正後のコードで同じ手順で in-process に
索引。LSP なしの `structural`）:

```
relations: 89686 -> 89927（消えた 194 行: REFERENCES 135・CALLS 59／増えた 435 行: CALLS 362・REFERENCES 73）
_name_calls: 10106 -> 9789
_member_refs（行, 結べた）: (265, 44) -> (310, 46)。うち super()（via_super = 1）: 14 行、結べた 2 行
索引の大きさ: 42.0 MB -> 42.0 MB（走査は 205 秒 -> 77 秒。並行に他の役が走っている PC の揺れ）
```

消えた誤った行の例（修正前の `relations`）:

```
logging_setup.py#DailyJsonlHandler.close     -> logging_setup.py#NonBlockingLineHandler.close  @226  super().close()（基底は logging の Handler）
logging_setup.py#NonBlockingLineHandler.close -> logging_setup.py#DailyJsonlHandler.close      @148  super().close()
engine/indexer.py#_BatchFailure.__init__      -> engine/indexer.py#Indexer.__init__              super().__init__(...)（基底は Exception）
lsp/client.py#LspError.__init__               -> lsp/client.py#LspClient.__init__        @60   super().__init__(...)
mcp/budget.py#CursorCodec.encode              -> mcp/budget.py#CursorCodec.decode        @132  base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
lsp/pool.py#LspPool._run_once                 -> lsp/pool.py#LspPool.reset               @637  h.docs.reset() など 6 か所
```

増えた正しい行の例（修正後）:

```
index/public.py#QueryTimeout.__init__ -> core/errors.py#LuciankeError.__init__     @137  super().__init__(...)（基底は別ファイル）
lsp/pool.py#LspPool._run_once         -> lsp/documents.py#DocumentSync.reset       @637  （上の LspPool.reset だった行。属性の注釈の型）
lsp/pool.py#LspPool._run_once         -> lsp/diagnostics.py#DiagnosticsHub.reset   @638
lsp/pool.py#LspPool._run_once         -> workspace/types.py#Workspace.root         @643  （REFERENCES）
```

たまたま正しかった行（`h.documents.version_of(path)` の `h` は型の分からないループ変数で、同じファイルの唯一の `version_of` に当たっていた）は
`_name_calls` に移り、問い合わせのときに名前の一致（BUG-0071・BUG-0077）が `conf=heuristic` で結ぶ。

F-PY（httpx）: `tests/contract_fixtures/test_fx_read_T00_T08.py::test_t07_structural_edges_follow_the_attribute_receiver` が、
`httpx/_urls.py` の `url._uri_reference.copy_with(**kwargs)` の行（119）に `structural` の `URL.copy_with` 行が無いことを確かめる（通過）。

テスト（決定的。フィクスチャ `tests/fixtures/receiver_calls`、バイト列のまま `-text`。README に各呼び出しの意図）:

- `tests/unit/test_syntax_relations.py`（6 件追加）: 属性の連鎖で型が分からない受け手（`url.raw.copy_with()`・`self.raw.copy_with()`・
  `make().copy_with()`・`url.items[0].copy_with()`）は `name_only`、注釈つきのクラス属性・`self.x: T = ...`・property の戻り値の型なら
  その型のメンバー、同じファイルの基底から継承した属性の型、TS の `this.x.m()`・`u.x.m()`（フィールド・パラメータプロパティ・getter）、
  Python の `super()`・`super(C, self)`・`super().attr`・`super(type(self), self)`（結ばない）・クラスの外・引数の `super`、TS の `super.m()`
  （アロー関数の中・オブジェクトリテラルのメソッドは対象外）。
- `tests/unit/test_engine_indexer_receivers.py`（11 件）: 別ファイルの基底への `super().save()`・`super().__init__()`、`super(Sub, self).load()`、
  途中の基底の上書き（`Leaf` → `Mid.save`）と 2 段上（`Base.load`）、同じファイルの菱形（C3。`D.greet` → `C.greet`）と基底が別ファイルの菱形、
  外部の基底が先なら結ばない（`_member_refs` に `via_super = 1`・`resolved_urn` NULL、`blocked_member_refs` が `Base.save` にだけ返す）、
  TS の `super.save()`、基底のファイルの編集で結び直る、`super()` が自分自身に結ばない、属性の連鎖（BUG-0079）、v6 → v7 の移行。
- `tests/contract/test_T06_T07_receivers.py`（6 件）: T-06 の `Base.save` の参照に `structural` で出る（「型不明・同名 2 つ」の警告は出ない）、
  `Worker.save` は `reason: external_base` で `data.unattributed_calls` と `warnings` に、`super(Sub, self)`・菱形、TS、T-07 の incoming・
  outgoing（`Sub.save` の呼び先は `Base.save`）、`Url.copy_with` の incoming に `Url.__init__` が出ない。
- `tests/contract/test_T06_T07_inherited.py`: `Worker.go` の `self.helper()`（BUG-0075 で結ばない行）が `Base.helper` の答えに
  `reason: external_base` で出るように直した。スキーマのテスト（版 7）を合わせた。
- `uv run ruff check`（引数なし）: クリーン。
- 関係・索引器・名前の呼び出し・継承・スキーマ・T-06 / T-07 の単体と契約（LSP なし。上の新しいテストを含む 22 ファイル）: `c7f2b7e`（BUG-0077）・`0fee57d`（BUG-0076）に載せ直した後 `567 passed`、`9f4ffa3`（BUG-0078）に載せ直した後も `567 passed`。
- `tests/contract_fixtures`（同じく載せ直した後）: `41 passed, 6 skipped`（skip は TS の LSP が無い PC での F-TS の exact の事後条件 5 件と、F-TS には当てはまらない上の BUG-0079 の試験 1 件）。F-PY の事後条件はすべて通過。
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の穴: **BUG-0085**（受け手が型の分からない名前〔`x.save()`〕のときは、同じファイルの唯一の `.save` に `structural` で結ばれる。
  名前の一致〔heuristic〕を通らない。見たことだけ）。
- 見つけた別の穴: **BUG-0086**（`9f4ffa3` に載せ直した後、別の pytest と同時に流した F-PY の T-06 の契約テストが事後条件 3〔`include_tests=false` の件数が `true` より少ない〕で 1 回落ちた。単独で流すと `9f4ffa3` 単独でも直しを載せても通る。見たことだけ）。
