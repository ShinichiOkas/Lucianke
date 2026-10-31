# untyped_refs

BUG-0093・BUG-0094・BUG-0095 のフィクスチャ（`tests/unit/test_engine_indexer_untyped_refs.py`・
`tests/contract/test_T06_T07_untyped_refs.py`）。LSP なしの `structural` の索引で、どの参照がどう結ばれるか。

| 箇所 | 書き方 | 期待 |
| --- | --- | --- |
| `use_modules.py` `run` | `import pkg.helpers` の `pkg.helpers.flush()` | `pkg/helpers.py#flush` に `structural`（BUG-0093） |
| `use_modules.py` `run` | `import pkg.sub.deep` の `pkg.sub.deep.go()`、`import pkg.sub as ps` の `ps.deep.go()` | `pkg/sub/deep.py#go` に `structural` |
| `use_modules.py` `via_module` | `from pkg import conf` の `conf.settings.reload()`・`conf.Settings.debug`（モジュールの名前の属性の連鎖） | `Settings.reload`・`Settings.debug` に `structural` |
| `use_modules.py` `run` | `import nsp.tools.fmt`（`__init__.py` の無い名前空間パッケージ）の `nsp.tools.fmt.render()` | `nsp/tools/fmt.py#render` に `structural` |
| `use_conf.py` `apply` | `from pkg.conf import settings` の `settings.reload()`（`settings = Settings()`） | `Settings.reload` に `structural`（BUG-0094） |
| `use_conf.py` `apply` | `typed.reload()`（`typed: Settings = make()`） | `Settings.reload` に `structural` |
| `use_conf.py` `apply` | `loose.reload()`（`loose = make()`。型が分からない） | 名前だけの呼び出し（`_name_calls`）。T-06 は `heuristic` |
| `use_conf.py` `apply` | `lock.acquire()`（`lock = Lock()`。外部の型） | `lk://_ext/threading#Lock.acquire` |
| `use_conf.py` `apply` | `settings.debug`（読み） | `Settings.debug` に `structural` |
| `use_conf.py` `apply` | `from pkg import shared` の `shared.reload()`（`pkg/__init__.py` が `from pkg.conf import settings as shared` で再び公開。シンボルではない） | 定義元の `settings`（`Settings()`）までたどって `Settings.reload` に `structural`（BUG-0100。前は名前だけの呼び出し） |
| `use_attrs.py` `tick` | `mon.stdout_lines += 1`（注釈の無い引数） | 名前だけの参照（`rw`）。`Monitor.stdout_lines` が唯一の候補なので T-06 は `heuristic`（BUG-0095） |
| `use_attrs.py` `tick` | `mon.samples = mon.samples + 1` | 名前だけの参照（`w` と `r`）。唯一の候補はインスタンス属性 `Monitor.samples`（`self.samples = 0`） |
| `use_attrs.py` `tick` | `mon.total`（`Monitor.total` とインスタンス属性 `Counter.total`） | 曖昧。T-06 の `Monitor.total` は件数と候補を `data.unattributed_refs` に書く |
| `use_attrs.py` `tick` | `mon.name`（組み込み型の属性と同名） | 結ばず、`builtin_name` と書く |
| `use_attrs.py` `wide` | `c.hits += 1`（`Gauge.hits` と、インスタンス属性 `Counter.hits`。`Wide(Counter)` の `self.hits` は `Counter.hits` と同じ属性） | 曖昧（候補 2 つ） |
| `use_attrs.py` `where` | 注釈の無い引数 `node` の `node.parent`（`Monitor.parent` だけが同名だが、同じファイルの `p: Path` の `p.parent` が外部の `pathlib.Path.parent`） | 結ばず、`external_name` と書く |
| `scripts/run.py` `go` | `other.lines`（`r: Raw` の `r.lines` は、兄弟のモジュール `rawlib` を import の解決が外部 `lk://_ext/rawlib#Raw.lines` にしたもの） | 兄弟のモジュールは外部の型の証拠にしない。唯一の候補のインスタンス属性 `Raw.lines` に `heuristic` |
| `untyped_bind.py` `f` | 注釈の無い引数 `x` の `x.save()`・`x.note` | `structural` にしない（BUG-0085）。T-06 は同じファイルの段の `heuristic` |
| `web/use.ts` `poke` | `m: any` の `m.hits += 1`・`m.hits` | TS の `Meter.hits` だけが候補（Python の `hits` は数えない）。`heuristic` |
| `web/use.ts` `poke` | `import { meter }` の `meter.reset()`（`export const meter = new Meter()`） | `Meter.reset` に `structural` |
