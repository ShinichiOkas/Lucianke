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
