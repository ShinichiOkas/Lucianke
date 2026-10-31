# reexports

BUG-0100（再び公開した名前を定義元までたどる）・BUG-0101（名前空間パッケージを外部にしない）・BUG-0104（TS / JS のバレル）・
BUG-0105（import する側の `from x import *`）・BUG-0109（TS / JS の既定の import。`web/defaults/`）・BUG-0108（TS の型の別名を注釈にした受け手。`web/aliases/`）・BUG-0113 / BUG-0114（TS の受け手の注釈〈添字の型・配列〉と型リテラルのメンバー。`web/members/`）のフィクスチャ
（`tests/unit/test_engine_indexer_reexports.py`・`tests/contract/test_T06_T07_reexports.py`）。LSP なしの `structural` の索引。

| 箇所 | 書き方 | 期待 |
| --- | --- | --- |
| `use.py` `run` | `from pkg import flush` の `flush()`（`pkg/__init__.py` が `from .helpers import flush`） | `pkg/helpers.py#flush` に `structural`（1 段） |
| `use.py` `run` | `from pkg import deep_go` の `deep_go()`（`pkg/__init__.py` → `pkg/sub/__init__.py` → `pkg/sub/impl.py`） | `pkg/sub/impl.py#deep_go` に `structural`（2 段） |
| `use.py` `run` | `from pkg.alias import fl` の `fl()`（`pkg/alias.py` が `from .helpers import flush as fl`。`__init__.py` でない普通のモジュール） | `pkg/helpers.py#flush` に `structural`（別名） |
| `use.py` `run` | `from pkg import absolute`（`pkg/__init__.py` が `from pkg.helpers import flush as absolute`。絶対 import） | `pkg/helpers.py#flush` に `structural` |
| `use.py` `run` | `from pkg import starred` の `starred()`（`pkg/__init__.py` の `from .stars import *`、`pkg/stars.py` の `__all__ = ["starred"]`） | `pkg/stars.py#starred` に `structural` |
| `use.py` `run` | `from pkg import hidden` の `hidden()`（`pkg/stars.py` が定義するが `__all__` に無い） | 行を作らない（`*` は公開しない） |
| `use.py` `run` | `from pkg import public` の `public()`（`from .loose import *`、`pkg/loose.py` に `__all__` が無い） | `pkg/loose.py#public` に `structural` |
| `use.py` `run` | `from pkg import _private` の `_private()`（`pkg/loose.py` の `_` で始まる名前） | 行を作らない |
| `use.py` `run` | `from pkg import loop` の `loop()`（`pkg/cyc_a.py` と `pkg/cyc_b.py` が互いに再び公開する循環） | 行を作らない（循環で止まる） |
| `use.py` `run` | `TokenEstimator().count("x")`・`EST.count("y")`（`EST = TokenEstimator()`）・`EST.reset()`（基底 `Base` のメソッド） | `pkg/est.py#TokenEstimator.count`・`pkg/est.py#Base.reset` に `structural` |
| `use.py` `run` | `Lock()`（`pkg/__init__.py` が外部の `from threading import Lock` を再び公開） | `lk://_ext/threading#Lock` |
| `use.py` `Sub` | `class Sub(TokenEstimator)` | `INHERITS` が `pkg/est.py#TokenEstimator` |
| `use.py` | `return flush`（呼び出しでない読み）・import 文の行 | `REFERENCES` / `IMPORTS` が定義元 |
| `use_ns.py` `run` | `import nsp.tools.fmt` の `nsp`・`nsp.tools`（`nsp/`・`nsp/tools/` に `__init__.py` が無い） | 参照を作らない。外部 `lk://_ext/nsp` にしない（BUG-0101） |
| `use_ns.py` `run` | `import nsp.tools as nt` の `nt.fmt.render()`、`from nsp import tools` の `tools.fmt.render()` | `nsp/tools/fmt.py#render` に `structural`（前は外部 `lk://_ext/nsp#tools.fmt.render`） |
| `use_ns.py` | `import nsp.tools.fmt` の import 文の行 | import したモジュール `nsp/tools/fmt.py` への `IMPORTS`（先頭の `nsp` にはファイルが無い） |
| `web/use.ts` `run` | `import { flush } from './lib'` の `flush()`・`lib.flush()`（`web/lib/index.ts` が `export { flush } from './helpers'`。`./lib` は `./lib/index.ts`） | `web/lib/helpers.ts#flush` に `structural`（BUG-0104） |
| `web/use.ts` `run` | `fl()`（`export { flush as fl }`）・`mainFn()`（`export { default as mainFn }`、`helpers.ts` の `export default function main`） | `helpers.ts#flush`・`helpers.ts#main` |
| `web/use.ts` `run` | `add(1, 2)`・`new Calc().run()`・`o: Options`（`export * from './math'`） | `web/lib/math.ts#add`・`#Calc`・`#Calc.run`・`#Options` |
| `web/use.ts` `run` | `deep.deepGo()`（`export * as deep from './deep'`）・`deepGo()`（`index.ts` → `deep/index.ts` の `export *` → `deep/impl.ts`）・`viaDir()`（`from './lib/deep'`） | `web/lib/deep/impl.ts#deepGo` |
| `web/use.ts` `run` | `plus(1, 2)`（`export * from './local'`、`local.ts` が import した `add` を `export { add as plus }`）・`ownAlias()`（`export { own as ownAlias }`） | `math.ts#add`・`local.ts#own`（3 段） |
| `web/use.ts` `run` | `mathDefault()`（既定の export は `export *` で公開しない）・`secret()`（export していない）・`loop()`（`cyc_a.ts` ↔ `cyc_b.ts` の循環） | 行を作らない |
| `star_use.py` `go` | `from pkg import *` の `flush()`・`deep_go()`・`TokenEstimator().count()`（`pkg` のリテラルの `__all__`、`pkg/__init__.py` がさらに再び公開） | 定義元に `structural`（BUG-0105） |
| `star_use.py` `go` | `starred()`（`pkg.stars` の `__all__`）・`public()`（`pkg.loose`、`__all__` なし）・`Widget().spin()`・`ITEM.spin()`・`class Mine(Widget)` | 定義元に `structural` |
| `star_use.py` `go` | `hidden()`（どの `__all__` にも無い）・`_private()`・`fresh()`（どの `*` の先にも無い） | 行を作らない。`nowhere.attr()` は名前だけ（`_name_calls`） |
| `star_use.py` `go` | `dyn_fn()`（`pkg/dyn.py` の `__all__ = list(_EXPORTED)` はリテラルでない） | `pkg/dyn.py#dyn_fn` に `heuristic`（名前だけで決めた） |
| `web/defaults/use.ts` `useDefaults` | `import main, { other } from './fn'` の `main()`・`other()`（`export default function main`） | `web/defaults/fn.ts#main`・`#other` に `structural`（BUG-0109） |
| `web/defaults/use.ts` `useDefaults` | `import Widget from './cls'`（`export default class Widget`）の `new Widget().spin()`・`w: Widget` の `w.spin()` | `cls.ts#Widget`・`#Widget.spin`（型の使用も） |
| `web/defaults/use.ts` `useDefaults` | `helper()`（`export default helper`）・`viaClause()`（`export { viaClause as default }`） | `ident.ts#helper`・`clause.ts#viaClause` |
| `web/defaults/use.ts` `useDefaults` | `fwd()`・`new FwdWidget().spin()`（`barrel.ts` の `export { default } from './fn'`・`export { default as Widget } from './cls'`）・`fnMod.default()`（`import * as fnMod`） | `fn.ts#main`・`cls.ts#Widget.spin` |
| `web/defaults/use.ts` `useDefaults` | `anon()`・`anonFwd()`（`export default (): number => 5`。バレルの転送も）・`cfg.level`（`export default { level: 1 }`） | モジュール `anon.ts`・`obj.ts`（ファイルの URN）。`level` は名前だけ（`_name_calls`） |
| `web/defaults/use.ts` | import 文の行 | 既定の export の定義（無名ならモジュール） |
| `web/aliases/use.ts` `readAll` | `o: Options`（`type Options = Base & { extra: string; tick(): void }`）の `o.retry`・`b: Box<number>`・`l: Loose`（`Partial<Base>`）・`c: Chain`（別名の別名）・`m: MaybeBase`（`Base \| null \| undefined`）・`d: Derived`（`interface Derived extends Options`）の `.retry` | `web/aliases/base.ts#Base.retry` に `structural`（BUG-0108） |
| `web/aliases/use.ts` `readAll` | `e.onlyOther`（`type Either = Base \| Other`。持つのは `Other` だけ）・`g.start()`（`type EngineAlias = Engine`）・`d.own` | `base.ts#Other.onlyOther`・`base.ts#Engine.start`・`types.ts#Derived.own` |
| `web/aliases/use.ts` `readAll` | `o.extra`・`o.tick()`（別名自身の型リテラルのメンバー） | v13 からシンボル `types.ts#Options.extra`・`#Options.tick` に `structural`（BUG-0114。前は行も名前だけの行も作らなかった）。`o.extra.length` の `length` は名前だけ（`extra: string`） |
| `web/aliases/use.ts` `readAll` | `e.shared`（合併で持つ構成が 2 つ）・`f.length` / `f.arity`（`type Fn = (x: number) => number`）・`bag.anything`（添字のシグネチャ） | 名前だけ（`_name_calls`） |
| `web/aliases/local.ts` `same` | 同じファイルの `type Mine = Base & { tag: string }` の `m.retry` | `base.ts#Base.retry` |
| `web/aliases/local.ts` `same` | `m.tag`（別名自身の型リテラルのメンバー） | `local.ts#Mine.tag`（BUG-0114） |
| `web/members/types.ts` | `type KyOptions = { retry?: Retry \| number; hooks: Hooks; inline: { before: string[]; after(): void }; timeout; readonly stop: typeof stop }`・`interface Opts { nested: { depth; inner: { leaf } } }`・`type Shape = { kind: 'a'; a } \| { kind: 'b'; b }` | 型リテラルのメンバーはシンボル（`KyOptions.retry`・入れ子の `KyOptions.inline.before`・`Opts.nested.inner.leaf`・`Shape.a`。合併の同名の `kind` は最後が素の URN）。T-02 では別名の子。`typeof stop` の `stop` は `consts.ts#stop`（メンバー自身にしない） |
| `web/members/use.ts` `readAll` | `r: Opts['retry']` の `r.limit`・`r.delay(1)` | `retry.ts#Retry.limit`・`#Retry.delay`（`Opts.limit` に結ばない。BUG-0113） |
| `web/members/use.ts` `readAll` | `xs: Opts[]` の `xs.length`・`xs.map()`、`ro: readonly Retry[]` の `ro.length` | `lk://_ext/globals#Array.length`・`#Array.map`・`#ReadonlyArray.length`（`Opts` のメンバーにしない） |
| `web/members/use.ts` `readAll` | `leafy: Opts['nested']['inner']` の `leafy.leaf`、`k: KyOptions` の `k.timeout`・`k.retry`、`o: Options`（`extends KyOptions`）の `o.timeout`・`s: Shape` の `s.a` | `types.ts#Opts.nested.inner.leaf`・`#KyOptions.timeout`・`#KyOptions.retry`・`#Shape.a` |
| `web/members/use.ts` `readAll` | `o.hooks.before.length`（`hooks: Hooks` は別ファイル）・`o.inline.before.length`・`o.inline.after()`（入れ子の型リテラル） | `KyOptions.hooks` → `retry.ts#Hooks.before` → `Array.length`、`KyOptions.inline` → `#KyOptions.inline.before` → `Array.length`、`#KyOptions.inline.after` |
| `web/members/use.ts` `readAll` | `s.kind.length`（合併の両方の構成にある `kind`）・`u: Opts['count']` の `u.toFixed()` | `s.kind` は素の URN `#Shape.kind`、`length`・`toFixed` は名前だけ（型が分からない） |
| `web/members/use.ts` `others` | `await p`（`Promise<Retry>`）の `got.limit`、`idx: Opts[keyof Opts]`・`gone: Opts['missing']`・`gen: T['retry']`（型引数）の `.limit` | `retry.ts#Retry.limit`、ほかの 3 つは名前だけ |
| `web/members/local.ts` `Server.run` | 同じファイルの `interface Cfg { db: { host; port } }` の `c.db.host`・`h: Cfg['db']` の `h.port`、フィールド `opts: { z: number }` の `this.opts.z` | `local.ts#Cfg.db.host`・`#Cfg.db.port`・`#Server.opts.z` |
