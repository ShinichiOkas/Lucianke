; TypeScript / TSX の参照タグ（Lucianke の relations.py が読む）。
; 捕捉名が参照の種別: call / write / rw / base / impl / type / read / import.*
; `<種別>.recv` は同じ一致の中の「レシーバ式」（member_expression の object）。
; 同じ識別子が複数の種別で捕捉されたときの優先は relations.py が決める（base・impl > type > call > rw > write > read）。

; --- 呼び出し（new を含む） ---
(call_expression
  function: (identifier) @call)

(call_expression
  function: (member_expression
    object: (_) @call.recv
    property: (property_identifier) @call))

(new_expression
  constructor: (identifier) @call)

(new_expression
  constructor: (member_expression
    object: (_) @call.recv
    property: (property_identifier) @call))

; --- 代入の左辺（w）・複合代入と ++/--（rw） ---
(assignment_expression
  left: (identifier) @write)

(assignment_expression
  left: (member_expression
    object: (_) @write.recv
    property: (property_identifier) @write))

(augmented_assignment_expression
  left: (identifier) @rw)

(augmented_assignment_expression
  left: (member_expression
    object: (_) @rw.recv
    property: (property_identifier) @rw))

(update_expression
  argument: (identifier) @rw)

(update_expression
  argument: (member_expression
    object: (_) @rw.recv
    property: (property_identifier) @rw))

; --- 型（注釈・ジェネリクス引数・キャスト・型エイリアスの本体はすべて type_identifier） ---
(type_identifier) @type

; --- 基底クラス・継承するインターフェース・implements ---
(extends_clause
  value: (identifier) @base)

(extends_clause
  value: (member_expression
    object: (_) @base.recv
    property: (property_identifier) @base))

(extends_type_clause
  type: (type_identifier) @base)

(implements_clause
  (type_identifier) @impl)

; --- 上のどれでもない参照 ---
(identifier) @read

(shorthand_property_identifier) @read

(member_expression
  object: (_) @read.recv
  property: (property_identifier) @read.attr)

; --- import（名前ごとに 1 件。位置は名前のトークン） ---
(import_statement
  (import_clause
    (named_imports
      (import_specifier
        name: (identifier) @import.name))))

(import_statement
  (import_clause
    (named_imports
      (import_specifier
        name: (identifier) @import.name
        alias: (identifier) @import.alias))))

(import_statement
  (import_clause
    (identifier) @import.default))

(import_statement
  (import_clause
    (namespace_import
      (identifier) @import.namespace)))
