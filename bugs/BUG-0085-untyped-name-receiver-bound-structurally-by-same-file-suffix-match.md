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
