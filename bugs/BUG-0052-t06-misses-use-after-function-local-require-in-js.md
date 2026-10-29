# BUG-0052 T-06 / 関係抽出が JS・TS の関数の中の `const { foo } = require(...)` で束縛した名前の使用を返さない

- 報告: 2026-10-29 / 見た人: BUG-0048 を直す役（BUG-0048 の Python の修正のあとに、同じ形を JS で確かめた）
- 症状: `syntax/relations.py` の `extract_relations`（`language_id="javascript"`、`resolve_name` に `{"foo": "lk://ws/foo.js#foo", "bar": "lk://ws/foo.js#bar"}`）に次のソースを渡すと、モジュール直下で束縛した `bar(2)`（5 行目）は `CALLS bar lk://ws/foo.js#bar` で返るが、関数の中で束縛した `foo(1)`（2 行目）は返らない（`require` の呼び出し 2 件と `bar` だけが返る）。
  ```
  function f() {
    const { foo } = require("./foo");
    return foo(1);
  }
  const { bar } = require("./foo");
  bar(2);
  ```
- 期待: 関数の中の `require` で束縛した名前の使用も、モジュール直下と同じく `./foo` の `foo` への参照・呼び出しとして返る。
- 再現手順: `tests/unit/test_syntax_relations.py` の `_relations("javascript", src, "m.js", table.get)` に上のソースと表を渡す。
- 調査はしていない（直す役の仕事）。BUG-0048 は Python の `def` / `class` の中の import だけを直した。
