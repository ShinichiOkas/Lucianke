# BUG-0071 型の分からない受け手のメソッド呼び出しが、参照・呼び出し階層から黙って落ちる（名前が一意のメソッドでも）

- 報告: 2026-10-31 / 見た人: 親（dogfood。Lucianke 自身のリポジトリ、LSP は indexing 中で structural の応答）
- 見たことだけを書く（調査していない。調査は直す役の仕事）

## 実測（作業ツリー = `cf4497c`）

- `find_references_and_implementations {"target": {"path": "src/lucianke/mcp/budget.py", "symbol_name": "Packer.pack"}}`:
  `references: 5 files, 38 sites (tests: 38)`。38 件すべてテスト。
- 本番の呼び出し `src/lucianke/mcp/fit.py:102` `page = packer.pack(out, ...)` と `src/lucianke/mcp/fit.py:142` `page = packer.pack(` が載っていない。
  `fit.py` の `pack_and_render` は引数 `packer: Any` で受けている。
- `pack` という名前のメソッドの定義は、ワークスペース（`src/`）に `Packer.pack`（`budget.py:218`）の 1 つだけ（`grep -rn "def pack" src/lucianke`）。
- `query_call_hierarchy {"target": {... "Packer._fit"}, "direction": "incoming", "depth": 2}`: `<- Packer.pack` の下は 13 件すべてテスト関数。`pack_and_render` が出ない。
- 応答の警告は「動的ディスパッチ（実装の取り違え・見落とし）と同名メソッドの区別が落ちます」とだけ書き、何件落としたか・どこかは言わない。

## 利用者（呼ぶ側の AI）として困ること

- 本番の呼び出し元が 0 件に見え、変更の影響を見落とす（テストだけが使っていると誤認する）。

## 原因・直し方・確認（2026-10-31）

### 原因

- 構文木の抽出器（`syntax/relations.py`）は、受け手の型が構文から読めない属性呼び出し `x.name(...)`（注釈なし引数・`self.x` の型が不明・
  呼び出しの戻り値など）の先の URN を決められず `target_urn=None` を返し、索引器（`engine/indexer.py` の `_relations`）がそれを
  捨てていた。`relations` に行が無いので、T-06 も T-07 も見えなかった。
- `fit.py` の `packer: Any` はもう一段悪かった: 注釈の `Any` を受け手の型として扱い、`lk://_ext/typing#Any.pack` という実在しない外部
  メソッドに結んでいた（修正前の T-07 outgoing に `-> Any.pack (external) @src/lucianke/mcp/fit.py:102`。Lucianke 自身の索引に
  `typing#Any.*` の外部シンボルが 100 個）。
- 警告は一般的な注意書きだけで、落とした件数・位置を言わなかった。

### 直し方（決めたこと）

- **型の分からない受け手の呼び出しを捨てない**: 抽出器の `RelationCandidate.name_only`（属性呼び出しで、先が決まらず、受け手の型も
  分からないもの）を、索引器が内部表 **`_name_calls`**（schema v5。`file_id`・`source_urn`・`name`・位置・`guard_condition`）に残す。
  受け手の型が分かっていてそのクラスに名前が無いもの（`runner: Runner` の `runner.pack()`、`Runner().pack()`）は入れない（別の
  クラスの同名に結ばない）。注釈の `Any` は「型が分からない」と同じに扱う（`_ANNOTATION_SKIP` に足した）。v4 → v5 の移行は表を
  足し、Python / TS / JS のファイルの `sha256` を空にして次の差分走査で取り込み直させる（値は取り込みでしか作れないため）。
- **問い合わせのときに名前で判定する**（`tools/_name_calls.py`）: 候補は、呼び出し箇所と同じ言語族（Python / TS・JS）のワークスペース内の
  同名の `Method`。テスト以外の定義が 1 つでもあれば、テストファイルの定義（テストダブル）は数えない。
  - **候補が対象 1 つだけ**で、名前が組み込み型のメソッド名（`list.append`・`dict.get`・`Array.push` など。`syntax/builtin_names.py` の
    固定の一覧）でなければ、`structural` の答えに足す。表現は **compact が行末の `conf=heuristic`**（T-06 の見出しに `by name: N`）、
    **json が `confidence_level: "heuristic"` と `match: "name"`**（T-06 の `totals.by_name`）。`warnings` に件数と根拠
    （`Packer.pack is the only method named 'pack' in the workspace (besides 1 in test files)`）を書く。
  - **同名が 2 つ以上・組み込み型と同名**なら、どの候補にも足さない。件数・同名の定義・先頭の位置（warnings は 5 件、
    `data.unattributed_calls[].sites` は 20 件まで）を `warnings` と `data.unattributed_calls[]`（`reason: ambiguous | builtin_name`）に書く。
- **この表現にした理由**（呼ぶ側の AI にとっての有用さ）:
  - `use` に `call?` を足すと compact の文法（`use` は 7 値）と事後条件「`use` は 7 値のいずれか」が変わる。`conf=<水準>` は §6.1 規約 4 の
    個別表示で、T-06 の `include_lexical_in_other_files`（`conf=lexical_fallback`）と T-07 の `tree-line` の文法に既にある。水準は
    「構文木で位置は正確、先は名前の一意性からの推定」なので `structural` と `lexical_fallback` の間の `heuristic`（§8.3 に書き足した）。
  - 曖昧な名前を全候補に足すと、`run`・`get` のような名前で参照の一覧が推定で埋まり、既定 2000 トークンの予算を本物の参照から奪う。
    件数と先頭の位置なら、AI は必要なときだけ追える（警告に件数を必ず書くので、黙って落ちることはない）。
  - 組み込み型と同名の名前は、型不明の受け手の大半が `dict` / `list` / `str` なので、同名のメソッドが 1 つでも結ばない
    （Lucianke 自身の `_name_calls` は 9,661 行で、上位は `get` 1,277・`join` 390・`startswith` 371・`append` 332）。
  - テストダブルを数えると、Lucianke 自身でもテストに `pack` が 1 つあるため、本番の 2 件が「同名 2 つ」で結ばれなくなる。
    テストダブルは本番メソッドの写しなので、名前を曖昧にしない。
- **exact は権威のまま**: LSP（と PaaD の `exact` 行）の答えには名前で結んだ行を混ぜない。ただし「対象が名前の唯一の定義」なのに
  LSP の答えに無い位置があれば、件数と先頭を `warnings` と `data.unattributed_calls[]`（`reason: not_in_exact_answer`）に書く。
  `Any` の受け手は LSP も型を決められず、exact の答えでも同じ見落としが起きるため。同名が複数なら LSP が正なので何も書かない。
- **T-07**: `structural` の木で incoming（深さ 2 以降のノードにも）と outgoing に同じ判定を使う。ノードの水準は呼び出し箇所のうち最も
  高いもの、`match` はすべての箇所が名前だけのとき。outgoing は組み込み型と同名の未帰属を数えない（`append` などで警告が埋まるため）。
  exact の木は根の incoming だけ注記する。
- 仕様書: §8.3 の `heuristic`、T-06 処理規約 7・json・精度と降格、T-07 処理規約 9・json・精度と降格（理由を 1 行添えた）。設計書の
  内部表の一覧に `_name_calls`。

### 確認

Lucianke 自身（`08b946e` のスナップショットと修正後の作業ツリーを、同じ手順で in-process に索引。LSP なしの `structural`）:

修正前:

```
=== T-06 Packer.pack references (include_tests=false)
references: 0 files, 0 sites (tests: 0)
=== T-06 Packer.pack references
references: 5 files, 38 sites (tests: 38)
=== T-07 Packer._fit incoming depth 2 (include_tests=false)
<- Packer.pack @src/lucianke/mcp/budget.py:324
=== T-07 pack_and_render outgoing depth 1
-> Any.pack (external) @src/lucianke/mcp/fit.py:102  when: fmt not in FITTED_FORMATS
```

修正後:

```
=== T-06 Packer.pack references (include_tests=false)
# find_references_and_implementations ok conf=heuristic lsp=n/a ...
! 2 call site(s) marked conf=heuristic are matched by name only: the receiver's type is unknown and Packer.pack is the only method named 'pack' in the workspace (besides 1 in test files)
references: 1 files, 2 sites (tests: 0; by name: 2)
src/lucianke/mcp/fit.py:102:23  [call] in pack_and_render  page = packer.pack(out, max_tokens=call.max_tokens, offset=offset, cursor_factory=cursor_factory)  conf=heuristic
src/lucianke/mcp/fit.py:142:23  [call] in pack_and_render  page = packer.pack(  conf=heuristic
=== T-06 Packer.pack references
references: 6 files, 40 sites (tests: 38; by name: 2)
=== T-07 Packer._fit incoming depth 2 (include_tests=false)
<- Packer.pack @src/lucianke/mcp/budget.py:324
  <- pack_and_render @src/lucianke/mcp/fit.py:102  when: fmt not in FITTED_FORMATS  conf=heuristic
=== T-07 pack_and_render outgoing depth 1
-> Packer.pack @src/lucianke/mcp/fit.py:102  when: fmt not in FITTED_FORMATS  conf=heuristic
```

（`pack_and_render` のノードの json の `call_sites` は `fit.py:102` と `:142` の 2 つ。`typing#Any.*` の外部シンボルは 100 → 0。
索引の大きさは 38.0 MB → 40.2 MB。）

テスト（決定的。フィクスチャ `tests/fixtures/untyped_calls`、バイト列のまま `-text`）:

- `tests/unit/test_name_calls.py`（11 件）: `Any`・注釈なし・`self.x`・呼び出しの戻り値の受け手は `name_only`、受け手の型が分かる
  （同じファイルのクラス・import したクラス）なら `name_only` にしない、呼び出しだけ、TS の `any`、`_name_calls` のファイル単位の
  全置換と CASCADE、v4 → v5 の移行（表を足し、コードのファイルの `sha256` だけ空にする）、表が無い DB は 0 件。
- `tests/contract/test_T06_T07_name_calls.py`（13 件）: 一意な名前は `conf=heuristic`・`match: "name"` で足す（テストダブルは数えない）、
  同名 2 つ（`run`）・組み込み型と同名（`get`）は足さず `warnings` と `data.unattributed_calls` に件数と位置、受け手の型が別のクラスと
  分かる `runner.pack()`・`Runner().pack()` は結ばない、言語族をまたがない（Python の `Mailer.send` と TS の `Sender.send`）、
  `use_kinds`・`scope_paths` が効く、exact の答えには足さず注記だけ、T-07 の incoming 深さ 2・outgoing・未帰属の注記、compact の文法。
- 既存のスキーマのテスト（内部表の許容リスト、版 5、移行の連鎖）を v5 に合わせた。
- `uv run ruff check src tests`: クリーン。T-06 / T-07 の単体・契約（`test_T06_T07*.py`・`test_T07_builtins_hidden.py`・スキーマ・抽出器・取り込み）: 全件通過。
- 全体テスト（`8536a5f` に載せ直した後）: `2 failed, 3811 passed, 29 skipped, 1 error in 1324.51s`。落ちた 3 件
  （`test_T04_lsp.py` の実 LSP の exact、`test_T06_T07_lsp.py` の setup の時間切れ、`test_index_core.py` の書き手スレッドの残り）は、
  同じ PC で他の役が並行に走って 22 分かかった回の実 LSP の時間切れとその後始末で、その 3 つだけを流すと通る（`9 passed`）。
  見たことだけの票を **BUG-0076** に書いた。`tests/contract_fixtures`（F-PY / F-TS の事後条件）: `39 passed, 5 skipped`（skip は TS の LSP が無い PC での F-TS の exact の事後条件だけ）。
- 見つけた別の穴: **BUG-0075**（基底クラスが別ファイルのとき、`self.helper()`・`s: Sub` の `s.helper()` が `relations` にも `_name_calls` にも
  入らない。受け手の型は分かっているので今回の名前で結ぶ対象ではない）。
