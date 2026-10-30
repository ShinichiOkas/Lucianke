# receiver_calls（BUG-0079・BUG-0081）

受け手の決め方を確かめるフィクスチャ（`tests/unit/test_engine_indexer_receivers.py`・`tests/contract/test_T06_T07_receivers.py`）。

## `super()`（BUG-0081）: 書いたクラスの C3 線形化の次のクラスから探す

| 場所 | 呼び出し | 結ぶ先 |
|---|---|---|
| `pkg/sub.py` `Sub.__init__` | `super().__init__()` | `Base.__init__`（別ファイルの基底） |
| `pkg/sub.py` `Sub.save` | `super().save()` | `Base.save`（`Sub.save` 自身ではない） |
| `pkg/sub.py` `Sub.load` | `super(Sub, self).load()` | `Base.load` |
| `pkg/leaf.py` `Leaf.save` | `super().save()` | `Mid.save`（途中の基底の上書き。別ファイル） |
| `pkg/leaf.py` `Leaf.load` | `super().load()` | `Base.load`（2 段上。`Mid` は持たない） |
| `pkg/diamond.py` `B.hello` | `super().hello()` | `A.hello`（`B` の線形化 B, A） |
| `pkg/diamond.py` `D.hello` | `super().hello()` | `B.hello` |
| `pkg/diamond.py` `D.greet` | `super().greet()` | `C.greet`（C3。深さ優先なら `A.greet`） |
| `pkg/diamond_use.py` `E.greet` | `super().greet()` | `C.greet`（基底が別ファイルの菱形） |
| `pkg/mixed.py` `Worker.save` | `super().save()`（`Worker(threading.Thread, Base)`） | 結ばない（先の外部の基底が同名を持つかもしれない）。`Base.save` の T-06 / T-07 に理由つきで書く |
| `pkg/mixed.py` `Plain.save` | `super().save()`（`Plain(Base, threading.Thread)`） | `Base.save` |
| `pkg/mixed.py` `Runner.run` | `super().run()`（`Runner(threading.Thread)`） | 結ばない（ワークスペースに届く先が無い） |
| `web/sub.ts` `Sub.save` | `super.save()`（メソッドの中とアロー関数の中） | `Base.save` |

## 属性を辿った受け手（BUG-0079）: `a.b.m()` の受け手は `a.b`

| 場所 | 呼び出し | 結ぶ先 |
|---|---|---|
| `pkg/chain.py` `Url.__init__` | `other.raw.copy_with()`（`raw` の型は分からない） | 結ばない（`Url.copy_with` にしない。名前だけの呼び出し） |
| `pkg/chain.py` `Url.__init__` | `other.parsed.copy_with()`（`parsed: Parsed`） | `Parsed.copy_with` |
| `pkg/chain.py` `Url.__init__` | `self.view.copy_with()`（`view` は `-> Parsed` の property） | `Parsed.copy_with` |
