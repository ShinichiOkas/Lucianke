-- schema_version: 13
-- PaaD スキーマ。公開部分は仕様書 §9.2 の DDL の写し（差分は tests/unit/test_index_schema.py で検出）。
-- PRAGMA は接続を開くときに設定する（index/connection.py）。ここには書かない。

CREATE TABLE workspaces (
  workspace_id        TEXT PRIMARY KEY,
  root_path           TEXT NOT NULL UNIQUE,
  python_env          TEXT,                       -- インタープリタの絶対パス。無ければ NULL
  active_branch       TEXT,
  head_sha            TEXT,
  default_base_ref    TEXT NOT NULL DEFAULT 'HEAD',
  exclude_globs_json  TEXT NOT NULL DEFAULT '[]',
  lsp_status_json     TEXT NOT NULL DEFAULT '{}', -- {"python": {"status": "ready", "server": "pyright", "progress": null}, ...}
  indexed_at          TEXT,                       -- ISO 8601
  confidence_level    TEXT NOT NULL DEFAULT 'exact'
                      CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);

CREATE TABLE files (
  file_id              INTEGER PRIMARY KEY,
  workspace_id         TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
  path                 TEXT NOT NULL,              -- ルート相対・'/' 区切り
  language             TEXT NOT NULL,              -- 'python', 'typescript', ..., 'unknown'
  tier                 INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 4),
  line_count           INTEGER NOT NULL,
  byte_size            INTEGER NOT NULL,
  sha256               TEXT NOT NULL,
  is_test              INTEGER NOT NULL DEFAULT 0, -- パス規則（test/, tests/, *_test.*, *.spec.*, test_*.py）で判定
  pagerank_score       REAL,                       -- インポート・呼び出しグラフ上のスコア
  last_modified_commit TEXT,
  indexed_at           TEXT NOT NULL,
  confidence_level     TEXT NOT NULL
                       CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback')),
  UNIQUE (workspace_id, path)
);
CREATE INDEX idx_files_lang ON files(workspace_id, language);

CREATE TABLE symbols (
  urn              TEXT PRIMARY KEY,               -- 第 7 章
  file_id          INTEGER REFERENCES files(file_id) ON DELETE CASCADE,  -- 外部シンボルは NULL
  is_external      INTEGER NOT NULL DEFAULT 0,     -- ワークスペース外（標準ライブラリ・依存パッケージ）
  parent_urn       TEXT REFERENCES symbols(urn) ON DELETE CASCADE,
  name             TEXT NOT NULL,
  qualified_name   TEXT NOT NULL,                  -- 'PaymentProcessor.process_charge'
  kind             TEXT NOT NULL,                  -- §6.1 の kind 語彙
  modifiers_json   TEXT NOT NULL DEFAULT '[]',
  start_line       INTEGER NOT NULL,
  end_line         INTEGER NOT NULL,
  sel_line         INTEGER,                        -- 名前トークンの位置
  sel_start_col    INTEGER,
  sel_end_col      INTEGER,
  signature        TEXT,
  return_type      TEXT,
  doc_summary      TEXT,
  doc_full         TEXT,
  declared_in      TEXT NOT NULL DEFAULT 'source'
                   CHECK (declared_in IN ('source','stub','cython','pyo3','proto','generated')),
  complexity       INTEGER,                        -- 循環的複雑度（MAY）
  scip_symbol      TEXT,                           -- SCIP 標準シンボル文字列（あれば）
  body_sha256      TEXT,                           -- 本体テキストのハッシュ（意味的差分の判定に使う）
  confidence_level TEXT NOT NULL
                   CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_symbols_file   ON symbols(file_id, start_line);
CREATE INDEX idx_symbols_qname  ON symbols(qualified_name);
CREATE INDEX idx_symbols_name   ON symbols(name);
CREATE INDEX idx_symbols_kind   ON symbols(kind);
CREATE VIRTUAL TABLE symbols_fts USING fts5(
  name, qualified_name, signature, doc_summary,
  content = 'symbols', content_rowid = 'rowid'
);

CREATE TABLE relations (
  relation_id      INTEGER PRIMARY KEY,
  source_urn       TEXT NOT NULL,                  -- symbols.urn（外部は is_external 行）
  target_urn       TEXT NOT NULL,
  relation_kind    TEXT NOT NULL CHECK (relation_kind IN (
                     'CALLS','IMPLEMENTS','INHERITS','REFERENCES','IMPORTS','TYPE_USES',
                     'READS_TABLE','WRITES_TABLE','RENDERS_TEMPLATE','EXPOSES_ENDPOINT','BINDS_NATIVE')),
  source_file_id   INTEGER REFERENCES files(file_id) ON DELETE CASCADE,
  target_file_id   INTEGER REFERENCES files(file_id) ON DELETE SET NULL,
  call_site_line   INTEGER,
  call_site_col    INTEGER,
  use_kind         TEXT CHECK (use_kind IN ('r','w','rw','call','type','import','impl')),
  guard_condition  TEXT,                           -- 呼び出し箇所を囲む条件式の要約
  confidence_level TEXT NOT NULL
                   CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_rel_target ON relations(target_urn, relation_kind);
CREATE INDEX idx_rel_source ON relations(source_urn, relation_kind);
CREATE INDEX idx_rel_sfile  ON relations(source_file_id);

CREATE TABLE dataflows (
  flow_id          INTEGER PRIMARY KEY,
  source_urn       TEXT NOT NULL,
  source_var       TEXT,
  sink_urn         TEXT NOT NULL,
  sink_var         TEXT,
  hop_count        INTEGER NOT NULL,
  passes_sanitizer INTEGER,                        -- 1/0。判定できなければ NULL
  path_json        TEXT NOT NULL,                  -- [{"urn":..., "line":..., "var":..., "guard":...}, ...]
  computed_at      TEXT NOT NULL,
  confidence_level TEXT NOT NULL
                   CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_flows_src  ON dataflows(source_urn);
CREATE INDEX idx_flows_sink ON dataflows(sink_urn);

CREATE TABLE diagnostics (
  diag_id            INTEGER PRIMARY KEY,
  file_id            INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  enclosing_urn      TEXT,                         -- 指摘位置を含む最内のシンボル
  severity           TEXT NOT NULL CHECK (severity IN ('error','warning','information','hint')),
  source             TEXT NOT NULL,                -- 'pyright', 'ruff', 'tsc', 'tree-sitter', 'lucianke-sql', ...
  code               TEXT,
  start_line         INTEGER NOT NULL,
  start_col          INTEGER NOT NULL,
  end_line           INTEGER,
  end_col            INTEGER,
  message            TEXT NOT NULL,
  quickfix_action_id TEXT,                         -- セッション内で有効な不透明 ID
  observed_at        TEXT NOT NULL,
  confidence_level   TEXT NOT NULL
                     CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_diag_file ON diagnostics(file_id, severity);
CREATE INDEX idx_diag_urn  ON diagnostics(enclosing_urn);

CREATE TABLE contracts (
  contract_id        TEXT PRIMARY KEY,             -- SHA-256(target_urn + contract_type + source_kind + 正規化 expression) の先頭 16 桁
  target_urn         TEXT NOT NULL,
  contract_type      TEXT NOT NULL CHECK (contract_type IN ('PRE','POST','INVARIANT','THROWS')),
  source_kind        TEXT NOT NULL CHECK (source_kind IN ('TYPE','GUARD','DOCSTRING','OPENAPI','GHERKIN')),
  expression         TEXT NOT NULL,                -- 人間可読（§11 T-16 の制約式文法）
  parsed_from_text   INTEGER NOT NULL DEFAULT 0,   -- 自然文の定型パターンから式を得たとき 1
  smt_lib            TEXT,                         -- SMT-LIB v2。変換不能なら NULL
  smt_reason         TEXT,                         -- smt_lib が NULL の理由（unparsed_text, unsupported_type, nonlinear, include_smt_false）
  origin_path        TEXT,
  origin_line        INTEGER,
  last_verify_status TEXT CHECK (last_verify_status IN
                       ('VERIFIED','VIOLATED','BOUNDED_VERIFIED','UNKNOWN_OR_TIMEOUT')),
  last_verified_at   TEXT,
  confidence_level   TEXT NOT NULL
                     CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_contracts_target ON contracts(target_urn);

CREATE TABLE git_commits (
  commit_sha        TEXT PRIMARY KEY,
  workspace_id      TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
  author            TEXT,
  author_email      TEXT,
  committed_at      TEXT NOT NULL,                 -- ISO 8601
  message_summary   TEXT NOT NULL,                 -- 1 行目
  is_merge          INTEGER NOT NULL DEFAULT 0,
  parent_shas_json  TEXT NOT NULL,
  confidence_level  TEXT NOT NULL DEFAULT 'exact'
                    CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback'))
);
CREATE INDEX idx_commits_time ON git_commits(workspace_id, committed_at DESC);

CREATE TABLE git_symbol_changes (
  commit_sha       TEXT NOT NULL REFERENCES git_commits(commit_sha) ON DELETE CASCADE,
  urn              TEXT NOT NULL,
  path             TEXT NOT NULL,
  file_id          INTEGER REFERENCES files(file_id) ON DELETE SET NULL,  -- 現存しなければ NULL
  change_type      TEXT NOT NULL CHECK (change_type IN ('ADDED','MODIFIED_BODY','MODIFIED_SIG','DELETED','RENAMED')),
  previous_urn     TEXT,                           -- RENAMED のとき旧 URN
  lines_added      INTEGER NOT NULL DEFAULT 0,
  lines_deleted    INTEGER NOT NULL DEFAULT 0,
  confidence_level TEXT NOT NULL
                   CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback')),
  PRIMARY KEY (commit_sha, urn)
);
CREATE INDEX idx_gsc_urn ON git_symbol_changes(urn, commit_sha);

CREATE TABLE git_co_changes (
  entity_a            TEXT NOT NULL,               -- URN（ファイルはセレクタ無しの URN）
  entity_b            TEXT NOT NULL,
  entity_kind         TEXT NOT NULL CHECK (entity_kind IN ('file','symbol')),
  co_change_count     INTEGER NOT NULL,
  a_change_count      INTEGER NOT NULL,
  coupling_ratio      REAL NOT NULL,               -- co_change_count / a_change_count（a が変わったとき b も変わる割合）
  last_co_changed_sha TEXT,
  window_commits      INTEGER NOT NULL,            -- 集計に用いた直近コミット数
  confidence_level    TEXT NOT NULL
                      CHECK (confidence_level IN ('exact','structural','heuristic','lexical_fallback')),
  PRIMARY KEY (entity_a, entity_b)
);
CREATE INDEX idx_cochange_ratio ON git_co_changes(entity_a, coupling_ratio DESC);

-- ===== 内部テーブル（公開しない。名前が _ で始まる。設計書 §6.2） =====
CREATE TABLE _meta (
  schema_version   INTEGER NOT NULL,
  data_version     INTEGER NOT NULL DEFAULT 0,
  edit_seq         INTEGER NOT NULL DEFAULT 0,
  lucianke_version TEXT,
  created_at       TEXT NOT NULL
);

CREATE TABLE _hover_cache (
  urn        TEXT NOT NULL,
  sha256     TEXT NOT NULL,
  hover_text TEXT NOT NULL,
  cached_at  TEXT NOT NULL,
  PRIMARY KEY (urn, sha256)
);

-- シンボル単位の重要度（index/rank.py。T-01 get_repo_map が読む。公開 DDL に列を足さないための内部表）
CREATE TABLE _symbol_rank (
  urn          TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  score        REAL NOT NULL,                 -- PageRank を最大 1.0 に正規化した値
  fan_in       INTEGER NOT NULL DEFAULT 0     -- 別ファイルからの辺の本数
);

-- import の解決が当たった／外れた場所の語幹（engine/reresolve.py。後から作られた・消えたファイルを import している側を引くための内部表）
CREATE TABLE _import_probes (
  file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  stem    TEXT NOT NULL,                 -- 解決で調べたパスの語幹（拡張子と __init__ / index を除いたもの）
  PRIMARY KEY (file_id, stem)
) WITHOUT ROWID;
CREATE INDEX _import_probes_stem ON _import_probes(stem);

-- 「LSP から取って relations に exact で書いた集合は、いまも完全」の目印（index/exact_marks.py。tools/_lsp_exact.py が読み書きする）。
-- (ファイルの指紋, exact 行の指紋) が一致するときだけ、LSP なしで exact に答える。再起動しても残る（ADR-16）。
CREATE TABLE _exact_marks (
  workspace_id TEXT NOT NULL,
  kind         TEXT NOT NULL,            -- refs / calls_in / calls_out
  urn          TEXT NOT NULL,            -- 問い合わせの対象のシンボル
  files_token  TEXT NOT NULL,            -- queries.files_fingerprint（ワークスペースの全ファイルの (path, sha256)）
  rows_fp      TEXT NOT NULL,            -- 対象の exact 行の指紋
  PRIMARY KEY (workspace_id, kind, urn)
) WITHOUT ROWID;

-- 受け手の型が構文から分からない属性呼び出し（`x.name(...)`）と属性の読み書き（`x.name`・`x.name = v`・`x.name += 1`。BUG-0095）の
-- 位置と名前（BUG-0071。engine/indexer.py が書き、tools/_name_calls.py が読む）。関係を整える段（index/inherited.py）も、型の分からない
-- import した変数の `settings.reload()` をここに書く（BUG-0094）。relations には先の URN が決まらないので入らない。T-06 / T-07 が
-- 問い合わせのときに名前で対象に結ぶ（同名の候補が 1 つだけなら heuristic で）。T-07 は use_kind = 'call' だけを読む。
CREATE TABLE _name_calls (
  file_id         INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  source_urn      TEXT NOT NULL,             -- 呼び出し箇所を含む最内の定義（モジュール直下ならファイルの URN）
  name            TEXT NOT NULL,             -- 呼んだ・読み書きしたメンバーの名前
  call_site_line  INTEGER NOT NULL,          -- 1 始まり（relations と同じ外部表現）
  call_site_col   INTEGER NOT NULL,          -- 1 始まり
  guard_condition TEXT,
  use_kind        TEXT NOT NULL DEFAULT 'call' -- call / r / w / rw（schema v9）
);
CREATE INDEX _name_calls_name   ON _name_calls(name);
CREATE INDEX _name_calls_source ON _name_calls(source_urn);
CREATE INDEX _name_calls_file   ON _name_calls(file_id);

-- インスタンス属性の定義（クラスのメソッドの中の `self.x = ...` / `this.x = ...`。シンボルにはならない。BUG-0095）。
-- 型の分からない受け手の属性の参照（`mon.x`）を名前で結ぶときの候補に数える（tools/_name_calls.py）。クラス自身か同じファイルの
-- 基底に同名のシンボル・インスタンス属性があるものは入れない（別ファイルの基底は問い合わせのときに C3 でたどってまとめる）。
CREATE TABLE _attr_defs (
  file_id   INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  class_urn TEXT NOT NULL,                   -- 属性を持つクラス
  name      TEXT NOT NULL,                   -- 属性の名前
  line      INTEGER NOT NULL,                -- 1 始まり（最初の代入の名前のトークン）
  col       INTEGER NOT NULL
);
CREATE INDEX _attr_defs_name ON _attr_defs(name);
CREATE INDEX _attr_defs_file ON _attr_defs(file_id);

-- モジュール直下の変数の構文から分かる型（`settings = Settings()`・`settings: Settings = ...`・TS の `const s = new S()`。BUG-0094）。
-- 関係を整える段（index/inherited.py）が、import した変数の `settings.reload()` をこの型のメンバーに結ぶ。
CREATE TABLE _var_types (
  file_id  INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  var_urn  TEXT NOT NULL PRIMARY KEY,        -- 変数のシンボル
  type_urn TEXT NOT NULL                     -- ワークスペースのクラスか外部の型
);
CREATE INDEX _var_types_type ON _var_types(type_urn);
CREATE INDEX _var_types_file ON _var_types(file_id);

-- Python のモジュール直下の import が束ねた名前（BUG-0100。index/reexports.py）。`from pkg import flush` の `pkg/__init__.py#flush`
-- （import はシンボルにならない）を、関係を整える段が束ねた先（`from .helpers import flush` の `pkg/helpers.py#flush`）までたどる。
-- kind: import（name → target_urn。モジュールは `#` なし、外部は lk://_ext/...）/ star（`from x import *` の x。name は '*'）/
-- all（`__all__` の名前。target_urn は空）/ dynamic_all（`__all__` がリテラルでない印。name は '*'。BUG-0105）。
-- TS / JS のモジュールが公開する名前も書く（v11。BUG-0104）: import（`export { a as b } from` などの別の定義を公開する名前）/
-- star（`export * from` のワークスペースの先）/ export（公開する名前。既定の export は入れない。target_urn は空）。
-- 取り込みがファイルごとに全置換する。
CREATE TABLE _reexports (
  file_id    INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  target_urn TEXT NOT NULL,
  kind       TEXT NOT NULL,                  -- import / star / all / dynamic_all / export
  ord        INTEGER NOT NULL                -- 書いた順（同じ名前は後のものが勝つ）
);
CREATE INDEX _reexports_name ON _reexports(file_id, name);

-- 再び公開した名前をたどるときに引いた (ファイル, 名前)（見つからなかったものも。BUG-0100。index/reexports.py）。
-- そのファイルの「公開する名前」が変わったら、依っていたファイル（file_id）を取り込み直す（engine/reresolve.py）。
CREATE TABLE _reexport_deps (
  file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,  -- 整えたファイル（たどった側）
  path    TEXT NOT NULL,                     -- 束ねた名前を引いたファイルのパス（同じワークスペース）
  name    TEXT NOT NULL,                     -- 引いた名前
  PRIMARY KEY (file_id, path, name)
) WITHOUT ROWID;
CREATE INDEX _reexport_deps_path ON _reexport_deps(path, name);

-- TS の型の別名（`type Options = Base & { extra: string }`）の構成（v12。BUG-0108。index/inherited.py）。受け手の型が別名のとき、
-- 関係を整える段がメンバーを構成から探す: and（別名そのもの・交差。書いた順に最初に持つ構成）/ or（合併。持つ構成がちょうど 1 つの
-- ときだけ）。kind: type（構成の型の URN）/ member（別名自身の型リテラルのメンバー `<別名>.<名前>`）/ opaque（中身の分からない構成。
-- target_urn は空）。null / undefined / void / never は書かない。取り込みがファイルごとに全置換する。
-- v13（BUG-0113・BUG-0114）: 型注釈つきのメンバー（インターフェース・型リテラルのプロパティ、クラスのフィールド）の型も同じ形で書く
-- （alias_urn がメンバー。`r: Opts['retry']`・`o.retry.limit` の受け手の型）。型リテラルのメンバーはシンボル（`KyOptions.retry`・
-- `KyOptions.retry.limit`）で、member の先はそれを指す。配列は標準の Array、添字の型 `B['k']` は type の `<B>.k`。中身の分からない
-- 構成しか無いメンバーは書かない。
CREATE TABLE _type_aliases (
  file_id    INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  alias_urn  TEXT NOT NULL,                  -- 別名・型注釈つきのメンバーのシンボル
  op         TEXT NOT NULL,                  -- and / or
  ord        INTEGER NOT NULL,               -- 構成の順（同じ型リテラルのメンバーは同じ ord）
  kind       TEXT NOT NULL,                  -- type / member / opaque
  target_urn TEXT NOT NULL
);
CREATE INDEX _type_aliases_alias ON _type_aliases(alias_urn);
CREATE INDEX _type_aliases_target ON _type_aliases(target_urn);
CREATE INDEX _type_aliases_file ON _type_aliases(file_id);

-- 受け手のクラスに無いメンバーへの参照（`self.helper()`・`s: Sub` の `s.helper()` で helper が基底クラスにある。BUG-0075。index/inherited.py）。
-- 関係を整える段が relations から移し、受け手のクラスの C3 線形化（INHERITS 行）で最初に見つかったメンバーを先にした relations の行を作る。
-- 基底のメンバー・基底の並びが変わったら、そのクラスのファイルを整えるときに結び直す（resolved_urn は前に結んだ先）。
-- `super().save()`（BUG-0081）は取り込みが via_super = 1 で直接入れ、class_urn の C3 線形化の次のクラスから探す。
-- import した変数の `settings.reload()`（BUG-0094）は class_urn が変数で、_var_types の型の線形化で探す（型が分からなければ _name_calls）。
-- TS のメンバーの型の受け手（`r: Opts['retry']` の `r.limit`・`o.retry.limit`。BUG-0113）は class_urn がメンバー（`Opts.retry`）で、
-- その型の構成（_type_aliases）で探す。メンバーがシンボルでない（基底から継承した）ときは class_urn をその持ち主にし、name を点つきの
-- 道（`retry.limit`）にする。決まらなければ _name_calls（名前は道の最後）。
CREATE TABLE _member_refs (
  file_id          INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,  -- 参照箇所のファイル（relations.source_file_id）
  source_urn       TEXT NOT NULL,
  class_urn        TEXT NOT NULL,             -- 受け手のクラス（`Sub`）。モジュール直下の変数のこともある（BUG-0094）
  name             TEXT NOT NULL,             -- メンバー名（`helper`）
  relation_kind    TEXT NOT NULL,             -- CALLS / REFERENCES
  use_kind         TEXT,
  call_site_line   INTEGER NOT NULL,          -- 1 始まり（relations と同じ）
  call_site_col    INTEGER NOT NULL,
  guard_condition  TEXT,
  confidence_level TEXT NOT NULL,
  resolved_urn     TEXT,                      -- いま relations に作ってある行の先（結べなければ NULL）
  via_super        INTEGER NOT NULL DEFAULT 0 -- 1: super() 経由（class_urn 自身は探さない。schema v7）
);
CREATE INDEX _member_refs_class ON _member_refs(class_urn);
CREATE INDEX _member_refs_file  ON _member_refs(file_id);

-- ===== symbols_fts を symbols に追従させるトリガ（外部コンテンツ FTS5） =====
CREATE TRIGGER symbols_ai AFTER INSERT ON symbols BEGIN
  INSERT INTO symbols_fts(rowid, name, qualified_name, signature, doc_summary)
  VALUES (new.rowid, new.name, new.qualified_name, new.signature, new.doc_summary);
END;
CREATE TRIGGER symbols_ad AFTER DELETE ON symbols BEGIN
  INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, doc_summary)
  VALUES ('delete', old.rowid, old.name, old.qualified_name, old.signature, old.doc_summary);
END;
CREATE TRIGGER symbols_au AFTER UPDATE ON symbols BEGIN
  INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, doc_summary)
  VALUES ('delete', old.rowid, old.name, old.qualified_name, old.signature, old.doc_summary);
  INSERT INTO symbols_fts(rowid, name, qualified_name, signature, doc_summary)
  VALUES (new.rowid, new.name, new.qualified_name, new.signature, new.doc_summary);
END;
