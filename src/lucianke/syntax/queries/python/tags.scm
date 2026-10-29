; Python の定義タグ（Lucianke の symbols.py が読む）。
; @definition.<種別> が定義ノード、@name が名前トークン。
; 種別の最終決定（Method / Constructor / Property / Field / Constant など）と、
; ローカル変数の除外は symbols.py が囲みのスコープを見て行う。
; デコレータ付き定義は decorated_definition ごと範囲に含める（symbols.py が親を見る）。

(class_definition
  name: (identifier) @name) @definition.class

(function_definition
  name: (identifier) @name) @definition.function

; PEP 695: type Alias = ...
(type_alias_statement
  left: (type (identifier) @name)) @definition.type

; モジュール変数・定数・クラス属性（x = 1 / x: int = 1 / x: int）
(expression_statement
  (assignment
    left: (identifier) @name)) @definition.variable
