; Python の参照タグ（Lucianke の relations.py が読む）。
; 捕捉名が参照の種別: call（呼び出しの関数位置）/ write / rw / base（基底クラス）/ type / read / import.*
; `<種別>.recv` は同じ一致の中の「レシーバ式」（属性アクセスの object）。
; 同じ識別子が複数の種別で捕捉されたときの優先は relations.py が決める（base > type > call > rw > write > read）。

; --- 呼び出し ---
(call
  function: (identifier) @call)

(call
  function: (attribute
    object: (_) @call.recv
    attribute: (identifier) @call))

; --- 代入の左辺（w）と複合代入（rw） ---
(assignment
  left: (identifier) @write)

(assignment
  left: (attribute
    object: (_) @write.recv
    attribute: (identifier) @write))

(augmented_assignment
  left: (identifier) @rw)

(augmented_assignment
  left: (attribute
    object: (_) @rw.recv
    attribute: (identifier) @rw))

; --- 型注釈（中の識別子は relations.py が辿る） ---
(type) @type.annotation

; --- 基底クラス（class A(B)、class A(m.B)、class A(Generic[T])） ---
(class_definition
  superclasses: (argument_list
    (identifier) @base))

(class_definition
  superclasses: (argument_list
    (attribute
      object: (_) @base.recv
      attribute: (identifier) @base)))

(class_definition
  superclasses: (argument_list
    (subscript
      value: (identifier) @base)))

; --- 上のどれでもない参照 ---
(identifier) @read

(attribute
  object: (_) @read.recv
  attribute: (identifier) @read.attr)

; --- import（名前ごとに 1 件。位置は名前のトークン） ---
(import_statement
  name: (dotted_name) @import.module)

(import_statement
  name: (aliased_import
    name: (dotted_name) @import.module
    alias: (identifier) @import.alias))

(import_from_statement
  name: (dotted_name) @import.name)

(import_from_statement
  name: (aliased_import
    name: (dotted_name) @import.name
    alias: (identifier) @import.alias))
