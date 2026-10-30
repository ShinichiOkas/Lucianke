# BUG-0081 上書きしたメソッドの中の `super().save()` が、基底クラスの `save` の参照・呼び出し元に出ない（同名が 2 つで「型不明・曖昧」に回る）

- 報告: 2026-10-31 / 見た人: BUG-0075 を直す役（structural の索引を in-process で作って見た。LSP なし）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 再現（2 ファイル、LSP なし）

```
pkg/__init__.py   （空）
pkg/base.py       class Base:
                      def save(self):
                          return 1
pkg/sub.py        from pkg.base import Base


                  class Sub(Base):
                      def save(self):
                          return super().save()
```

`Indexer(...).scan()` のあと:

- `relations` の `Sub.save` の行は `CALLS -> lk://_ext/builtins#super` だけ。`Base.save` を指す行は無い。
- `_name_calls` に `('lk://superws/pkg/sub.py#Sub.save', 'save', 6)` が 1 行。

`find_references_and_implementations {"target": {"path": "pkg/base.py", "symbol_name": "Base.save"}}`（compact）:

```
! 1 call site(s) of .save() with a receiver of unknown type are not listed: 2 methods share the name (Base.save @pkg/base.py:2, Sub.save @pkg/sub.py:5); e.g. pkg/sub.py:6 (data.unattributed_calls)
references: 0 files, 0 sites (tests: 0)
```

`query_call_hierarchy`（`Base.save`、incoming、depth 1）も呼び出し元 0 件で、同じ「型不明・同名 2 つ」の警告だけ。

## 利用者（呼ぶ側の AI）として困ること

- 上書きしたメソッドから `super()` で基底を呼ぶのは、基底のメソッドの代表的な呼び出し元。`Base.save` を変えるとき、その呼び出し元が 0 件に見える。
- 警告は「受け手の型が分からない」と言うが、`super()` の受け手（囲みのクラスの基底）は構文から分かる。上書きがある限り同名は必ず 2 つ以上なので、名前の一致（BUG-0071）では決して結ばれない。

## 原因・直し方・確認（2026-10-31）

BUG-0079 と一緒に直した（どちらも抽出器の受け手の決め方）。

### 原因

- 抽出器（`syntax/relations.py`）は `super()` を受け手として知らなかった。`super().save()` の受け手（`super()` の呼び出し）の型は
  `_type_ref` で決まらず、受け手の型が分からない呼び出しとして扱われた。同じファイルに `save` がほかに 1 つだけならそれに（たまたま
  基底のこともあれば、別のクラスのこともある。Lucianke の `logging_setup.py` では 2 つのハンドラの `close` が互いに結ばれていた）、
  そうでなければ `name_only`（`_name_calls`）になり、上書きがある限り同名は 2 つ以上あるので名前の一致（BUG-0071）では決して結ばれず、
  「型不明・曖昧」の警告になった。

### 直し方（決めたこと）

- **`super().m()` は `C` の C3 線形化で `C` の次から最初に `m` を持つクラスのメンバーに結ぶ**（`structural`。`match` なし）。`C` は
  `super()` なら囲みのクラス、`super(C, self)` なら `C`、TS / JS の `super.m()` なら囲みのクラス（メソッドの中と、その中のアロー関数。
  オブジェクトリテラルのメソッドの `super` はそのオブジェクトのプロトタイプなので対象外）。`super` を自分で束ねた名前は組み込みの
  `super` ではない。`super(type(self), self)`・クラスの外は結ばない。`super().attr`（参照）も同じ。
- **索引の整えで決める**: 抽出器は `RelationCandidate.super_of`（`C` の URN）にし、取り込みが内部表 `_member_refs` に **`via_super = 1`** で
  直接入れる（新しい列。スキーマ **v7**）。整え（`index/inherited.resolve_member_refs`）は BUG-0075 と同じ C3（`INHERITS` 行、ファイルをまたぐ）で、
  `C` 自身を飛ばして探す（`C` の上書き〈呼び出しの囲みのメソッド自身〉には結ばない）。同じファイルの基底だけで決まる場合も同じ経路にした
  （結べなかった行を理由つきで引けるようにするため）。基底のメンバーの増減・上書き・基底の並びの変更は BUG-0075 と同じ範囲で結び直る
  （`C` は編集した基底の派生クラスなので）。
- **結ばない**: BUG-0075 と同じく、中身の分からない外部の基底（`object`・`Generic`・`Protocol`・`ABC` 以外の `lk://_ext/...`）・クラスでない
  基底に先に当たったら結ばない。名前の一致にも回さない（受け手は構文から分かっている）。
- **結ばなかった理由を書く**（`index/inherited.blocked_member_refs`・新しい `tools/_blocked_refs.py`）: 先に当たった基底の後ろに対象のクラスがあり
  （その基底が名前を持たなければ対象に届く）、その間に同名を持つワークスペースのクラスが無い行を、`structural` の T-06 の答えと T-07 の
  根の incoming に、件数・理由・先頭の位置として `warnings` と `data.unattributed_calls`（`reason: "external_base" | "unknown_base"`、
  `sites[]` に `via_super`・`receiver_class`・`blocked_by`）で書く。`exact` の答えには書かない（LSP は外部の基底の中身も知っている）。
  BUG-0075 の結べない行（`Worker(threading.Thread, Base)` の `self.helper()`）も同じ形で書く（同じ理由で結ばない行なので）。
  届く先がワークスペースに無い行（`Runner(threading.Thread)` の `super().run()`）はどの対象にも書かない。
- **スキーマ v7**: `_member_refs.via_super INTEGER NOT NULL DEFAULT 0`。v6 からの移行は列を足し、Python / TS / JS のファイルの `sha256` を
  空にして次の差分走査で取り込み直させる（`super()` の行と BUG-0079 の誤った行は取り込み直さないと直らない）。
- 仕様書: T-06 処理規約 9 (b)(c) と json の `unattributed_calls[]`、T-07 処理規約 11（理由を 1 行添えた）。設計書: 内部表の一覧（v7）と §7.6.2。

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
