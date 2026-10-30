# BUG-0085 型の分からない名前の受け手 `x.save()` が、同じファイルの唯一の `.save` に `structural` で結ばれる（名前の一致〔heuristic〕を通らない）

- 報告: 2026-10-31 / 見た人: BUG-0079・BUG-0081 を直す役（抽出器の出力を見ていて見た）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `8a72e31` + BUG-0079・BUG-0081 の直し。`tests/unit/test_syntax_relations.py` の `_relations` で抽出、import の解決なし）

```python
class Base:
    def save(self):
        return 1


class Holder:
    ref: Base


def f(x, y: Base, other):
    x.save()            # 抽出: CALLS save -> lk://ws/m.py#Base.save（structural, name_only=False）
    other.ref           # 抽出: REFERENCES ref -> lk://ws/m.py#Holder.ref（structural）
```

- `x`・`other` は注釈の無い引数（型は構文から分からない）。
- 受け手が式（`a.b.save()`・`f().save()`・`x[0].save()`）のときは BUG-0079 の直しで `name_only`（`_name_calls`）になるが、受け手が名前
  （`x.save()`）のときは、同じファイルに `.save` が 1 つだけなら、その行が `relations` に `structural` で入る。
- 同じファイルに `.save` が 2 つ以上あると `name_only` になる（`_name_calls` に入り、T-06 / T-07 が名前で判定する）。

## 利用者（呼ぶ側の AI）として困ること

- 受け手の型が分からない呼び出しが、名前の一致（BUG-0071。`conf=heuristic`・`match: "name"`、ワークスペース全体で一意か・テストフィクスチャの
  近さ〔BUG-0077〕を見る）を通らず、印の無い `structural` として出る。`x` が別のファイルのクラスのインスタンスでも、同じファイルの同名に結ばれる。

## 原因・直し方・確認（2026-10-31）

### 原因

- 抽出器 `syntax/relations.py` の `_resolve_member` は、受け手の型が分からないときの最後の手段として「同じファイルの定義の修飾名の
  末尾一致（一意のとき。囲みの定義自身は除く）」と「import した同名（`resolve_name(name)`。外部 URN は除く）」に当てていた。BUG-0079 は
  受け手が **式**（`a.b`・`f()`・`x[0]`）のときだけこの 2 つを外し、受け手が **名前**（`x.save()` の `x`）のときは残していた。
- 名前の受け手の型は `_type_ref` が決める（注釈・`x = Foo()` / `new Foo()`・`self` / `cls` / `this`・モジュールやクラスの名前）。決まらない
  （注釈の無い引数・型の分からないローカル・ループ変数・型の分からないモジュール直下の変数・クラスの外の `self` / `this`）と、上の末尾一致に
  落ちて `structural`（`name_only=False`）の行になっていた。同じファイルにその名前が 1 つだけなら、受け手が別のファイルのクラスでも
  組み込み型でも、その 1 つに結ばれていた。import した同名の関数に結ばれるもの（`urn.relax()` → `from lucianke.core.urn import relax`）もあった。

### 直し方（決めたこと）

- **名前の受け手も、型が構文から分かるときだけ `structural` で結ぶ**（`_resolve_member`）。分かるのは: パラメータ・変数の注釈、
  `x = Foo()`（同じファイル・import したクラスの構築。TS / JS は `new Foo()`）、`self` / `cls` / `this`（囲みのクラス）、モジュールの名前
  （`import m`・`from pkg import m`・TS の `import * as m`。`m.f()` はメソッドではなくモジュールの関数 `<m>#f`。外部の `os.path.join` は
  `lk://_ext/os#path.join`）、ほかに既にある推論（属性の型〔BUG-0079〕・基底から継承したメンバー〔BUG-0075〕）。
- 分からなければ、同じファイルの唯一の同名にも import した同名にも当てず `name_only`（`_name_calls`）にする。問い合わせのときの名前の一致
  （BUG-0071・BUG-0077 の近さ）が `conf=heuristic`・`match: name` で結ぶか、曖昧・組み込み型と同名と書く。`_resolve_member` の
  末尾一致と import した同名の当て方は消した（受け手が式か名前かで分けない。`recv` が無いときも結ばない）。
- **前方参照の文字列の注釈を読む**（`_py_ann_name`）: `x: "Foo"`・`Optional["Foo"]`・`-> "Foo"` の中身が名前 1 つなら型にする。名前の受け手を
  末尾一致で当てなくなったので、`from __future__ import annotations` を使わないコードの注釈つきの受け手が `heuristic` に落ちないように。
- 呼び出しでない参照（`other.ref` の読み・`mon.count += 1`）は名前の一致の対象ではない（`name_only` は `CALLS` だけ）ので、結ばずに落ちる
  （BUG-0079 の式の受け手と同じ）。誤った `structural` よりは落とす方がよい（呼ぶ側の AI は確かな行として読む）。黙って落ちる件は票 **BUG-0095**。
- TS / JS も同じ規則（同じ `_resolve_member`）。注釈の無い引数・`any`・型の分からないローカル・クラスの外の `this` は名前だけ。
- **スキーマ v8**（表は変えない）: v7 からの移行で関係を抽出する言語（Python / TS / JS）のファイルの `sha256` を空にして、次の差分走査で
  取り込み直させる（既にある誤った行は取り込み直さないと消えない。v5〜v7 と同じ形）。
- 仕様書: T-06 処理規約 9 (a) に名前の受け手の規則と理由（1 行）、T-07 処理規約 11 に 1 文。設計書: §7.6.2 の「末尾一致は名前の受け手にだけ
  使う」を直し、内部表の段落に schema v8。抽出器の docstring の規則 2〜4 も直した。

### 確認

Lucianke 自身（`8c10a2a` のスナップショットを、修正前のコード〈`8c10a2a` の `src`〉と修正後のコードで、同じ手順で順に in-process に索引。
LSP なしの `structural`。取り込み直し待ちのファイル〈`sha256` が空〉は前後とも 0）:

```
relations: 91350 -> 89769（消えた 1581 行: REFERENCES 1293・CALLS 288／増えた 0 行）
_name_calls: 9896 -> 10184（+288。消えた CALLS 288 行がすべて名前だけの呼び出しに移った）
移った 288 箇所の名前の判定: heuristic で同じ先 208・heuristic で別の先 12・ambiguous 16・builtin_name 34・候補なし 18
索引の大きさ: 44.3 MB -> 43.8 MB（走査 45 秒 -> 44 秒）
```

消えた誤った行の例（修正前の `relations` の `structural` 行 → 修正後の名前の判定）:

```
src/lucianke/lifecycle.py:239         _log.error(...)              -> lifecycle.py#StepResult.error（dataclass のフィールド）   → 候補なし（logging.Logger）
src/lucianke/index/connection.py:95   conn.close()                 -> connection.py#ReaderPool.close                           → builtin_name（sqlite3.Connection）
src/lucianke/lsp/pool.py:349          params.get("value")          -> pool.py#LspPool.get                                      → builtin_name（dict）
src/lucianke/mcp/budget.py:125        text.encode("utf-8")         -> budget.py#CursorCodec.encode（囲みのクラス自身）         → 候補なし（str）
src/lucianke/cli/doctor.py:166        m.group(1)                   -> doctor.py#Check.group                                    → 候補なし（re.Match）
tests/unit/test_core_urn.py:217       urn.relax()                  -> core/urn.py#relax（import した同名の関数）               → heuristic Urn.relax
src/lucianke/tools/t02_get_file_skeleton.py:100  view.engine_name(...) -> tools/_skeletons.py#engine_name（import した同名）  → heuristic RevisionView.engine_name
tests/unit/test_app_roots_decision.py:53 など 8 箇所  d.gate(...)   -> 別のテスト関数の中の入れ子の関数 gate                    → heuristic RootsDecision.gate
```

たまたま正しかった行（`bench/_stdio.py:338` の `mon.violate(...)` → `Monitor.violate` など 208 箇所）は、同じ先に `conf=heuristic`（「in the calling
file」など）で結ばれる。`LspPool.rebind` の中の `h.rebind(ws)`（`LspHandle.rebind` と `LspPool.rebind` が同じファイル）などは曖昧と書く。

テスト（決定的）:

- `tests/unit/test_syntax_relations.py`（5 件追加）: Python の注釈の無い引数・型の分からないローカル・ループ変数・モジュール直下の変数・
  クラスの外の `self` の `x.save()` は `name_only`（同じファイルの唯一の `Base.save` にも import した同名の `save` にも結ばない）、注釈・
  前方参照の文字列（`"Base"`・`Optional["Base"]`）・同じファイルと import したクラスの構築は `structural`、`import helpers` / `from pkg import mod` /
  外部の `os` の関数はモジュールの関数、TS（注釈の無い引数・`any`・ローカル・クラスの外の `this` は名前だけ、注釈・`new`・`import * as` は
  `structural`）、JS の注釈の無い引数。
- `tests/unit/test_engine_indexer_receivers.py`（3 件追加）: フィクスチャ `receiver_calls` の `pkg/names.py`・`web/names.ts`（README に表）で、
  注釈の無い引数の `x.persist()` は `relations` に無く `_name_calls` に入る、`x: Ledger`・`Ledger()` / `new Ledger()` は `Ledger.persist`、
  `names_mod.persist()` はモジュールの関数。v7 → v8 の移行（コードのファイルだけ `sha256` が空になる）。
- `tests/contract/test_T06_T07_receivers.py`（2 件追加）: T-06 の `Ledger.persist` の参照は `annotated`・`built` が `structural`、`untyped` が
  `heuristic`・`match: name`（`by_name: 1`、「in the calling file」の警告）、モジュールの関数 `persist` の参照は `module_function` だけ、
  T-07 の outgoing の `untyped` → `Ledger.persist` は `heuristic`（Python・TS）。
- `tests/contract/test_T06_T07_name_scopes.py`: `tests/fixtures/other/app/mod.py` の `p.pack()`（注釈の無い引数）を「抽出器が結ぶので名前の一致では
  ない」と確かめていた所を、名前の一致の同じファイルの段（`conf=heuristic`）で結ぶ、に直した（README も）。スキーマのテスト（版 8）を合わせた。
- `uv run ruff check`（引数なし）: クリーン。
- 関係・import・索引器・名前の呼び出し・継承・受け手・スキーマ・T-06 / T-07 の単体と契約（LSP なし。上の新しいテストを含む 27 ファイル）:
  `9d6ef2e`（BUG-0086・BUG-0080 の後）に載せ直して `372 passed, 6 skipped`（skip は TS の LSP が無い PC での exact の試験）。
- `tests/contract_fixtures`（同じく載せ直した後）: `45 passed, 6 skipped`（skip は TS の LSP が無い PC での F-TS の exact の事後条件 5 件と、
  F-TS には当てはまらない BUG-0079 の試験 1 件）。F-PY の事後条件はすべて通過。
- 全体テストは流していない（親が main で 1 回にまとめて流す）。
- 見つけた別の穴（見たことだけの票）: **BUG-0093**（`import pkg.helpers` の `pkg.helpers.flush()` は名前だけの呼び出しになり、モジュールの関数は
  名前の一致の候補でないのでどこにも結ばれない）、**BUG-0094**（`from pkg.conf import settings` の `settings.reload()` は
  `pkg/conf.py#settings.reload` という無い先になり、`relations` にも `_name_calls` にも残らない）、**BUG-0095**（型の分からない受け手の属性の
  読み書きは結ばれず、T-06 に件数も警告も出ない。この直しで Lucianke 自身の `REFERENCES` 1,293 行がそうなった）。どれも `8c10a2a` でも同じ
  （BUG-0095 は数が増えた）。
