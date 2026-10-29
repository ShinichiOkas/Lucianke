# BUG-0018 revision が WORKING_TREE 以外だと E_UNSUPPORTED（存在しない参照でも）、TOML・Markdown・拡張子なしの骨格も E_UNSUPPORTED

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状 1: 仕様書 §5.2 は `revision` に `HEAD` / `STAGED` / ブランチ名 / コミット SHA を許し、解決できなければ `E_REVISION_NOT_FOUND`。実際は `HEAD`・`STAGED`・存在しない `no-such-rev-xyz` のすべてが `E_UNSUPPORTED`（`revision 'HEAD' is not supported yet (WORKING_TREE only)`）。存在しない参照名も `E_REVISION_NOT_FOUND` にならない。
- 症状 2: 仕様書 §11 T-02 は XML/JSON/YAML/TOML と未知言語（Tier 4）も骨格を返す。実際の `get_file_skeleton` は `uv.lock`（toml）・`README.md`（markdown）・`LICENSE`（unknown）を `E_UNSUPPORTED`（`language 'toml' has no structural support in this build (supported: javascript, python, tsx, typescript)`）で返す。
- 再現手順: `get_file_skeleton {"paths":["src/lucianke/core/urn.py"],"revision":"HEAD"}`、`{"paths":["uv.lock"]}`、`{"paths":["README.md"]}`、`{"paths":["LICENSE"]}`（`bench/dogfood/run_dogfood.py 5 6`）。
- 頻度: 上記 7 呼び出し中 7。
- 備考: 作業ツリーには `syntax/adapters/json_.py` などが未コミットで存在する（結線前の可能性）。「未実装で既知」なら閉じてよい。
- 調査はしていない（直す役の仕事）。

## 原因
- 症状 1: T-02・T-05 のハンドラが `revision != WORKING_TREE` を無条件に `E_UNSUPPORTED` で返していた。Git 層（`git/repo.py` の `resolve_revision`、`git/vtree.py` の `open_file_source`）は 15a でできていたが、ツールに結線されていなかった。T-03・T-04・T-06・T-07・T-08 も同じ門（`require_working_tree`）で、存在しない参照も解決せずに `E_UNSUPPORTED` にしていた。
- 症状 2: TOML・JSON・YAML・XML・ipynb はアダプタ（7f）が T-02 に結線されていなかった（f96b2b7 で結線済み）。Markdown・reStructuredText と未知の言語（Tier 4）は、T-02 が構文木のある言語しか扱えず、項目単位で `E_UNSUPPORTED` にしていた。仕様書 §11 T-02 処理規約 7 の Tier 4 の骨格（ブロック推定）と、Tier 3 の Markdown の見出しが未実装だった。

## 直し方
- 共通の入口 `tools/_rev.py` を足した（`tools/_services.py` は触っていない）: `open_revision`（`WORKING_TREE` は Git に触れず、それ以外は dulwich を遅延 import して解決。解決できなければ `E_REVISION_NOT_FOUND`、Git が無ければ `reason: no_git_repository`）、`read_checked`（封じ込め → 断面から読む → 大きさ・秘密・バイナリの検査）、`analyze_at`（作業ツリーは従来の `analyze_file` のまま、過去断面は `FileSource` のバイト列を同じ手順で解析）、`reject_unsupported_revision`（T-03・T-04・T-06・T-07・T-08: 存在しない参照は `E_REVISION_NOT_FOUND`、解決できる参照は `E_UNSUPPORTED` + `details.reason: "revision_not_supported_until_M2"`）。
- T-02・T-05 は `open_revision` → `analyze_at` / `file_skeleton` で読む。ヘッダの `rev=` は利用者の入力のまま、`engine_used` は `tree-sitter@<短縮 SHA>`（ステージは `@staged`）、`warnings[]` に「作業ツリーに触れず Git オブジェクトから読んだ・LSP なし」を 1 行（仕様書 §8.5）。
- T-02 の振り分けを `tools/_skeletons.py` の `file_skeleton` に集めた: データ形式（`_data_files.data_skeleton`。断面対応にした）→ コード（構文木）→ Markdown・rst（見出しの入れ子を `[Section]`、`structural`）→ それ以外（Tier 4）は `syntax.heuristic.block_outline` の入れ子（`heuristic`、何も取れなければ `lexical_fallback` の空の骨格）。`! DEGRADED` はパイプラインが水準から付ける。ctags は子プロセスを増やさないため T-02 では起動しない。
- 既存テストのうち、Git でない `tests/fixtures/poly` に `revision="HEAD"` を渡して `E_UNSUPPORTED` を期待していたもの（T-02・T-03・T-04・T-05・T-06・T-08）は、`E_REVISION_NOT_FOUND`（`no_git_repository`）に直した。`legacy/calc.cob`（Tier 4）が `E_UNSUPPORTED` を期待していた 2 箇所は、推定の骨格が返ることを確かめる形に直した。

## 確認
- 新規 `tests/contract/test_revision_reads.py`（tmp に dulwich でリポジトリを作る: HEAD~1 = v1、HEAD = v2、ステージ = v3、作業ツリー = v4）: T-02・T-05 が `HEAD~1`・`HEAD`・`STAGED`・`WORKING_TREE`・ブランチ名・短縮 SHA でそれぞれの断面の内容を返す、`no-such-rev-xyz`・`HEAD~9`・`HEAD@{1}`・`HEAD:pay.py`・`main..HEAD`・`dead` が `E_REVISION_NOT_FOUND`、その断面に無いファイルは項目単位の `E_TARGET_NOT_FOUND`、読み取りの前後で作業ツリーと `.git` の全ファイルの SHA-256 が変わらない、Git の無いディレクトリは `no_git_repository`、T-03・T-04・T-06・T-07・T-08 は `HEAD` で `E_UNSUPPORTED`・存在しない参照で `E_REVISION_NOT_FOUND`。TOML・Markdown・LICENSE（拡張子なし）を `WORKING_TREE`・`HEAD~1`・`STAGED` で `get_file_skeleton` し、TOML は `[Key]`、Markdown は `[Section]` の入れ子（フェンス内の `#` は見出しにしない）、LICENSE は `lexical_fallback` + `! DEGRADED`。
- `test_revision_reads.py` + `test_T02_skeleton.py` + `test_T02_data_files.py` + `test_T05_bodies.py` + `test_T03_T04.py` + `test_T06_T07.py` + `test_T08_diagnostics.py` + `tests/unit/test_import_graph.py` の 200 件が、コミット時点のクリーンな作業ツリー（git worktree）で 2 回続けて green。`ruff check src/lucianke/tools` も通る。
- 残り: `tools/_services.py` の `analyze_file` と `_rev.analyze_bytes` が解析手順を二重に持つ（結線の役 16a が `_services.py` を終えたら、`analyze_file` を `_rev` に寄せる）。
