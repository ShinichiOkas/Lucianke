# name_scopes (BUG-0077)

Name-only matching of method calls whose receiver type is unknown (BUG-0071) picks candidates per
call site, nearest first: the calling file, then the test fixture that holds the call
(`tests/fixtures/<name>/`), then the workspace. Test doubles compete only for calls in their own
file. Used by `tests/contract/test_T06_T07_name_scopes.py`.

- `app/fit.py` (production): `packer.pack()` is `app/budget.py#Packer.pack`; `conn.execute()` is
  not attributed (only test doubles define `execute`); `job.start()` is ambiguous (`Runner.start`,
  `Engine.start`).
- `tests/test_fit.py`: `packer.pack()` is `Packer.pack` (no closer definition);
  `self.conn.execute()` is the same file's `FakeConn.execute` (a `self.x` receiver is left to name
  matching even when the file defines the name). `tests/test_other.py`: `conn.execute()` is not
  attributed.
- A plain `x.name()` on an unannotated parameter is a name match even when the calling file defines
  the name once (`tests/fixtures/other/app/mod.py`: `p.pack()` is the calling file's `Packer.pack`,
  `conf=heuristic`); the structural extractor binds only receivers whose type it knows (BUG-0085).
- `tests/fixtures/sample/` has its own `Packer.pack`: its calls are never attributed to
  `app/budget.py#Packer.pack`. `sink.settle()` is ambiguous inside the fixture (`Left.settle`,
  `Right.settle`); `mailer.send()` is not attributed (the fixture has only a test double);
  `calc.compute()` falls back to the workspace (`Calc.compute`).
- `tests/fixtures/other/` is a sibling fixture with another `Packer.pack`: invisible to the rest.
- `app/jobs.py#Session` delegates to the object it wraps: `self.proc.terminate()` is not attributed
  to `Session.terminate` (a method of the class that encloses the call is never the match), and
  `self.proc.reset()` is ambiguous (`Calc.reset`, `Session.reset`), not `Calc.reset`.
