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
