; TypeScript / TSX の定義タグ（Lucianke の symbols.py が読む）。
; @definition.<種別> が定義ノード、@name が名前トークン。
; export / declare / 単一宣言の lexical_declaration を範囲に含める処理と、
; 種別の最終決定（Method / Constructor / Property / Field / Constant など）は symbols.py が行う。

(class_declaration
  name: (type_identifier) @name) @definition.class

(abstract_class_declaration
  name: (type_identifier) @name) @definition.class

(interface_declaration
  name: (type_identifier) @name) @definition.interface

(type_alias_declaration
  name: (type_identifier) @name) @definition.type

(enum_declaration
  name: (identifier) @name) @definition.enum

; namespace Foo { } / namespace A.B { }
(internal_module
  name: [(identifier) (nested_identifier)] @name) @definition.module

; module Foo { }（文字列名の ambient module は名前を持たないので拾わない）
(module
  name: (identifier) @name) @definition.module

(function_declaration
  name: (identifier) @name) @definition.function

(generator_function_declaration
  name: (identifier) @name) @definition.function

; オーバーロード宣言・declare function（本体なし）
(function_signature
  name: (identifier) @name) @definition.function

; クラスのメソッド・アクセサ・コンストラクタ、オーバーロード宣言、抽象メソッド、インターフェースのメソッド
(method_definition
  name: [(property_identifier) (private_property_identifier)] @name) @definition.method

(method_signature
  name: (property_identifier) @name) @definition.method

(abstract_method_signature
  name: (property_identifier) @name) @definition.method

(public_field_definition
  name: [(property_identifier) (private_property_identifier)] @name) @definition.field

(property_signature
  name: (property_identifier) @name) @definition.property

; const / let / var。関数式・アロー関数を束縛するものは symbols.py が Function にする
(variable_declarator
  name: (identifier) @name) @definition.variable
