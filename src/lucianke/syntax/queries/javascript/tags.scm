; JavaScript の定義タグ（Lucianke の symbols.py が読む）。typescript/tags.scm の JS 版。
; @definition.<種別> が定義ノード、@name が名前トークン。

(class_declaration
  name: (identifier) @name) @definition.class

(function_declaration
  name: (identifier) @name) @definition.function

(generator_function_declaration
  name: (identifier) @name) @definition.function

(method_definition
  name: [(property_identifier) (private_property_identifier)] @name) @definition.method

(field_definition
  property: [(property_identifier) (private_property_identifier)] @name) @definition.field

; const / let / var。関数式・アロー関数を束縛するものは symbols.py が Function にする
(variable_declarator
  name: (identifier) @name) @definition.variable
