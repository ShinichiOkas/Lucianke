# BUG-0075 基底クラスが別ファイルにあると、派生クラスの受け手からの継承メソッドの呼び出しが参照・呼び出し階層から黙って落ちる

- 報告: 2026-10-31 / 見た人: BUG-0071 を直す役（structural の索引を in-process で作って見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 再現（2 ファイル、LSP なし）

```
pkg/__init__.py   （空）
pkg/base.py       class Base:
                      def helper(self):
                          return 1
pkg/sub.py        from pkg.base import Base


                  class Sub(Base):
                      def run(self):
                          return self.helper()


                  def use(s: Sub):
                      return s.helper()
```

`Indexer(...).scan()` で取り込んだあと:

- `SELECT ... FROM relations WHERE target_urn LIKE '%helper%'` → 0 行
- `SELECT ... FROM _name_calls` → 0 行（受け手の型〈`Sub`〉は分かっているので、BUG-0071 の名前で結ぶ対象にもならない）

つまり `find_references_and_implementations` の `Base.helper` には `sub.py:6`（`self.helper()`）も `sub.py:10`（`s.helper()`）も出ない。
`query_call_hierarchy` の incoming も同じはず（見ていない）。

## 利用者（呼ぶ側の AI）として困ること

- 基底クラスのメソッドを変えるとき、派生クラス経由の呼び出し元が 0 件に見え、影響を見落とす。
- 応答には落とした件数も位置も出ない。

## 原因・直し方・確認（2026-10-31）

### 原因

- 抽出器（`syntax/relations.py` の `_member_of`）は、受け手の型が分かる属性アクセスの先を「受け手のクラスのメンバー」まで決める。
  - 受け手が **同じファイルのクラス**（`self.helper()`・`cls.make()`・同じファイルの `s: Sub`・`Sub()`）: クラス自身と **同じファイルの基底だけ** を
    たどり（深さ優先）、別ファイルの基底は読み飛ばして `None` を返していた。受け手の型は分かっているので `name_only`（BUG-0071 の `_name_calls`）にもならず、行が消えた。
  - 受け手が **別ファイルのクラス**（`from pkg.sub import Sub` の `s: Sub`）: `pkg/sub.py#Sub.helper` を返す。`Sub` に `helper` が無いのでその URN は
    `symbols` に無く、関係を整える段（`engine/indexer._settle_relations`）が「宙に浮いた目標」として消していた。
- どちらも、`INHERITS` の行（`Sub → Base`）は索引にあるのに、メンバーを探すときに使っていなかった。
- ついでに見えた穴: 同じファイルの基底のたどり方が深さ優先で、菱形（`D(B, C)`・`B(A)`・`C(A)`）で `C` の上書きより `A` を選んでいた。
  `class Sub(mod.Base)` は `Base` という名前だけを引き直していて、`mod` の `Base` を見ていなかった。

### 直し方（決めたこと）

- **探す順序は C3**（`core/mro.py` の `c3_linearize`。Python の MRO と同じ。TS / JS は単一継承なので左から右と同じ）。抽出器と索引の両方がこれを使う。
- **抽出器**: 同じファイルのクラスにメンバーが無いとき、同じファイルの基底だけで線形化が閉じるならその場で C3 の順に探す。別ファイルの基底・この場で
  解決できない名前・`mod.Base` の形が線形化のどこかにあれば、`<このクラス>.name`（例 `pkg/sub.py#Sub.helper`）を返して索引の整えに任せる
  （別ファイルの基底の祖先が並びを変えうるので、部分的には決めない）。TS の `interface I extends A` も基底として読む（`implements` は読まない）。
- **索引の整え**（新しい `index/inherited.py`、`_settle_relations` から呼ぶ）:
  - **退避**: 宙に浮いた目標のうち、「クラスの部分」（`path#Sub.helper` の `path#Sub`）がワークスペースのクラス・インターフェースである
    `CALLS` / `REFERENCES` の行を、内部表 **`_member_refs`**（schema **v6**）に移す（受け手のクラス・名前・行を作り直す列・いま結んである先 `resolved_urn`）。
  - **結ぶ**: 受け手のクラスの C3 線形化（基底は `relations` の `INHERITS` 行を書いた順。ファイルをまたぐ）を先頭から見て、最初にその名前を持つクラスの
    シンボル（オーバーロードは最後の宣言＝実装。`_redirect_overloads` と同じ）を先にした `structural` の行を `relations` に作る。派生クラスや途中の基底の
    上書きが先に見つかるので、上書きはそちらに結ぶ。
  - **誤って結ばない**: 中身の分からない外部の基底（`lk://_ext/...`）に先に当たったら結ばない（その基底が同名を持つかもしれない。例
    `Worker(threading.Thread, Base)` の `self.helper()`）。`object`・`typing.Generic`・`typing.Protocol`・`abc.ABC` はユーザーのメソッドを持たないので素通りする
    （`Shape(ABC, Base)` は `Base.helper`）。クラスでない基底（`Base = make_base()` の別名）も止める。循環・矛盾した並びは結ばない。名前で結ぶ（BUG-0071）
    こともしない（受け手の型が分かっているので、別のクラスの同名に結ばない方針のまま）。
  - **編集に追従**: 結び直す範囲は「整えるファイルの退避行」と「整えるファイルのクラスとその推移的な派生クラス（`INHERITS` を逆にたどる再帰 CTE）を
    受け手にする退避行」。基底のメンバーの増減・上書きの追加と削除・基底の並びの変更は、そのクラスのファイルの取り込み → 整えで結び直る。
    `mid` を import していない `use.py` の `s.helper()` も、`Mid` に上書きを足せば **取り込み直さずに** `Mid.helper` に結び直る。
    基底の URN が消えたときは、直接の派生クラスのファイル（基底を import している）が import 元の解き直し（BUG-0025・BUG-0034）で一緒に整う。
    先が変わったら前の行（LSP が `exact` で書き戻した同じ位置・同じ先の行も）を消して差し替える。同じ位置・同じ先の行が既にあれば足さない。
- **整えで決める理由**（問い合わせのときではなく）: 行が `relations` に入るので、T-06 / T-07 だけでなく T-09 の影響範囲・PageRank など `relations` を読む
  すべてにそのまま効く。取り込みの順序（基底が後から来る）にも、増分の編集にも、上の範囲の結び直しで追従できる。
- **水準は `structural`**（`heuristic` ではない）: 受け手の型と継承関係という構文から読める事実で決まる（BUG-0071 の「名前の一意性からの推定」とは違う）。
- **スキーマ v6**: `_member_refs` を足す。v5 からの移行は v5 と同じく表を足し、Python / TS / JS のファイルの `sha256` を空にして次の差分走査で取り込み直させる
  （v5 までの取り込みで捨てた行は、取り込み直さないと戻らない）。
- 仕様書: T-06 処理規約 8（新設）と処理規約 7 の言い回し、T-07 処理規約 10（新設）。理由を 1 行添えた。設計書: 内部表の一覧に `_member_refs`、§7.6.2 に整えの段。

### 確認

Lucianke 自身（`origin/main` のスナップショットを、修正前のコードと修正後のコードで同じ手順で in-process に索引。LSP なしの `structural`）:

修正前:

```
=== T-06 CommonArgs.workspace_id (include_tests=false)
target: [Field] lk://snap/src/lucianke/tools/_base.py#CommonArgs.workspace_id
references: 0 files, 0 sites (tests: 0)
=== T-07 ToolServices.workspace incoming depth 1
<- WiredServices.workspace @src/lucianke/tools/_runtime.py:454
<- ToolServices.resolve_workspace_id @src/lucianke/tools/_services.py:73
<- _ingest_poly @tests/contract/test_T03_T04.py:51
=== T-06 ToolServices.roots (include_tests=false)
references: 1 files, 1 sites (tests: 0)
src/lucianke/tools/_services.py:55:27  [call] in ToolServices.workspace  roots = list(self.roots())
```

修正後:

```
=== T-06 CommonArgs.workspace_id (include_tests=false)
references: 13 files, 17 sites (tests: 0)
src/lucianke/tools/t00_configure_workspace.py:718:26  [r] in _set_python_env  ws = wc.resolve(args.workspace_id)
...（args: XxxArgs(CommonArgs) の args.workspace_id。t00〜t20 のハンドラ）
=== T-07 ToolServices.workspace incoming depth 1
<- WiredServices.workspace @src/lucianke/tools/_runtime.py:454
<- ToolServices.resolve_workspace_id @src/lucianke/tools/_services.py:73
<- _ingest_poly @tests/contract/test_T03_T04.py:51
<- test_t06_exact_answers_get_a_note_not_records @tests/contract/test_T06_T07_name_calls.py:263
<- make_rig @tests/contract/test_T13_code_action.py:198
=== T-06 ToolServices.roots (include_tests=false)
references: 2 files, 2 sites (tests: 0)
src/lucianke/tools/_runtime.py:467:46  [call] in WiredServices.resolve_workspace_id  if hub is None and not list(self.roots()):
src/lucianke/tools/_services.py:55:27  [call] in ToolServices.workspace  roots = list(self.roots())
```

（`_runtime.py:467` は `WiredServices(ToolServices)` の `self.roots()`、`test_T13_code_action.py:198` は `T13Services(ToolServices)` の `svc.workspace(None)`。
`relations` は 87,987 → 88,027 行、`_member_refs` は 258 行〈結べた 40: CALLS 3・REFERENCES 37／結べない 218: ほぼ `self._stream` のような
シンボルの無いインスタンス属性と、外部の基底を持つクラス〉、索引は 40.9 MB → 41.1 MB、走査は 26.5 秒 → 23.3 秒〈同じ PC、揺れの範囲〉。）

テスト（決定的。フィクスチャ `tests/fixtures/inherited_calls`、バイト列のまま `-text`。README に各呼び出しの意図）:

- `tests/unit/test_engine_indexer_inherited.py`（14 件）: 取り込み直後（2 段上の基底・途中の上書き・`cls.make()`・自分のメンバー・`s: Sub`・`Sub()`・
  `base` も `mid` も import しない `use.py`・クラス属性の参照・`structural` で `target_file_id` が基底のファイル）、外部の基底が先なら結ばない／`ABC` は素通り、
  TS の `extends` と interface の `extends`、編集（派生クラスに上書きを足す・消す、途中の基底に上書きを足す〈`use.py` は取り込み直さずに追従〉・消す、
  基底のメンバーを消す〈別の定義に結ばない〉・戻す、基底の並びを変える、途中の基底のファイルを消す、TS の上書き）、派生クラスを先に取り込んで基底が後から来る、
  差分走査、v5 → v6 の移行、`_member_refs` がファイルと一緒に消える。
- `tests/contract/test_T06_T07_inherited.py`（6 件）: T-06 の参照に `structural`（`match` なし・`conf=heuristic` なし）で出る、compact の文法、
  途中の上書きが勝つ（`Base.shout` には出ず `Mid.shout` に出る）、TS、T-07 の incoming と outgoing。
- `tests/unit/test_syntax_relations.py`（6 件追加）: 別ファイルの基底は `<このクラス>.name`、import したクラスの受け手、同じファイルの菱形は C3、
  外部の基底が先なら `None`（`name_only` にもしない）、`mod.Base` は整えに任せる、TS の `this` と interface。
- `tests/unit/test_core_mro.py`（7 件）: 単純な連鎖、菱形、Python の説明の例、矛盾・循環は `None`、重複した基底、深さの上限。
- 既存のスキーマのテスト（内部表・索引の許容リスト、版 6、移行の連鎖）を v6 に合わせた。
- `uv run ruff check`（引数なし）: クリーン。
- 関係・索引器・スキーマ・T-06 / T-07 の単体と契約（LSP なし。上の新しいテストを含む 23 ファイル）: `343 passed, 6 skipped`。`4ca4684` に載せ直した後（19 ファイル）: `326 passed`。
- `tests/contract_fixtures`（`547a320` の上）: `38 passed, 5 skipped, 1 failed`。落ちたのは F-PY の T-07 事後条件 3（`when` の検査が `!(isinstance(url, str))` の `!` を外さずに照合した。BUG-0074 の箇所で、その辺〈`httpx/_urls.py:119`〉は修正前からある〔票 BUG-0079〕）。BUG-0074 の直し（`4ca4684`）に載せ直してその 1 件を流し直すと `1 passed, 1 skipped`（skip は TS の LSP が無い PC の F-TS）。
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の穴: **BUG-0081**（上書きしたメソッドの中の `super().save()` が基底の `save` に結ばれない。同名が 2 つになるので BUG-0071 の名前の一致でも結ばれず、
  「型不明・曖昧」の警告になる。見たことだけ）。
