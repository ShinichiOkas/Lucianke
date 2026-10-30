# untyped_calls (BUG-0071)

Method calls whose receiver type is unknown to the syntax tree (`Any`, an unannotated parameter,
`self.x` of unknown type, the result of a call). Used by
`tests/contract/test_T06_T07_name_calls.py` and `tests/unit/test_name_calls.py`.

- `pack` has one production method (`Packer.pack`) and one test double (`FakePacker.pack`):
  untyped `x.pack(...)` calls are matched to `Packer.pack` by name (`conf=heuristic`).
- `run` has two production methods (`Packer.run`, `Runner.run`): untyped `x.run()` is not attributed.
- `get` is also a method of `dict`: untyped `x.get(...)` is not attributed.
- `runner.pack()` with `runner: Runner` and `Runner().pack()` have a known receiver type without
  that member: never matched to `Packer.pack`.
- `send` exists in Python (`Mailer.send`) and TypeScript (`Sender.send`): name matching stays
  within one language family.
- The calls sit in files that do not define a method of the same name (a same-file definition is
  already chosen by the structural extractor).
