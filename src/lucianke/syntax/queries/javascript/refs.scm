; JavaScript の参照タグ（Lucianke の relations.py が読む）。typescript/refs.scm の JS 版（型と implements は無い）。
; 捕捉名の意味は typescript/refs.scm と同じ。

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

; --- 基底クラス ---
(class_heritage
  (identifier) @base)

(class_heritage
  (member_expression
    object: (_) @base.recv
    property: (property_identifier) @base))

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
