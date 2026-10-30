# inherited_calls（BUG-0075）

基底クラスが別ファイルにあるとき、派生クラスの受け手から継承したメンバーへの呼び出し・参照を、索引の `INHERITS` 行を
C3 の順にたどって結ぶことを確かめるフィクスチャ（`tests/unit/test_engine_indexer_inherited.py`・
`tests/contract/test_T06_T07_inherited.py`）。

| 場所 | 呼び出し | 結ぶ先 |
|---|---|---|
| `pkg/sub.py` `Sub.run` | `self.helper()` | `Base.helper`（2 段上の基底・別ファイル） |
| `pkg/sub.py` `Sub.loud` | `self.shout()` | `Mid.shout`（`Mid` が上書き） |
| `pkg/sub.py` `Sub.build` | `cls.make()` | `Base.make`（classmethod） |
| `pkg/sub.py` `Sub.own` | `self.tag()` | `Sub.tag`（自分のもの） |
| `pkg/sub.py` `use` | `s.helper()`（`s: Sub`） | `Base.helper` |
| `pkg/sub.py` `fresh` | `Sub().shout()` | `Mid.shout` |
| `pkg/use.py` `go` | `s.helper()`（`s: Sub`。`base` も `mid` も import しない） | `Base.helper` |
| `pkg/use.py` `label` | `s.name`（`s: Sub`） | `Base.name`（クラス属性） |
| `pkg/mixed.py` `Plain.go` | `self.helper()`（`Plain(Base, threading.Thread)`） | `Base.helper` |
| `pkg/mixed.py` `Worker.go` | `self.helper()`（`Worker(threading.Thread, Base)`） | 結ばない（先の外部の基底が同名を持つかもしれない） |
| `pkg/mixed.py` `Shape.go` | `self.helper()`（`Shape(ABC, Base)`） | `Base.helper`（`ABC` は素通り） |
| `web/sub.ts` `Sub.run` | `this.helper()` | `Base.helper` |
| `web/sub.ts` `use` | `s.helper()`・`l.ping()`（`l: Loud`、`Loud extends Pingable`） | `Base.helper`・`Pingable.ping` |
| `web/sub.ts` `fresh` | `new Sub().helper()` | `Base.helper` |
