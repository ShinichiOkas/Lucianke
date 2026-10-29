-- schema_version: 4
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
