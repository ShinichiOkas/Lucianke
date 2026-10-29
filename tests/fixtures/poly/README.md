# F-POLY（合成フィクスチャ）

仕様書 §14.2 の F-POLY。Tier 1〜4 のファイルと、意図的な型エラー・循環呼び出し・同名の再定義・非 ASCII 識別子・
CRLF / BOM / 末尾改行なしのファイルを含む。**バイト列そのものが検査対象なので、エディタで保存し直さない**
（`.gitattributes` で `tests/fixtures/** -text`）。生成元は `tests/fixtures/make_fpoly.py`。

| ファイル | 目的 |
|---|---|
| src/shop/services/payment.py | クラス・メソッド・デコレータ・docstring・ガード節・意図的な型エラー・未使用 import |
| src/shop/services/limits.py | 別ファイルからの参照、循環呼び出し（retry と schedule） |
| src/shop/api/routes.py | ガード条件付きの呼び出し（if / try） |
| src/shop/models/receipt.py | 同名の再定義（条件分岐内）、非 ASCII 識別子・絵文字 |
| src/shop/models/crlf_bom.py | CRLF・BOM あり |
| src/shop/models/no_final_newline.py | 末尾改行なし |
| tests/test_payment.py | テストからの参照 |
| notebooks/eda.ipynb | 出力（base64 画像・大きな stream。文字列 OUTPUT_SENTINEL を含む）を持つ Notebook |
| reports/junit.xml | 40 件中 2 件が失敗の JUnit XML |
| db/schema.sql, proto/order.proto, docs/flow.dot, docs/flow.mmd | Tier 2 |
| conf/settings.json, conf/app.yaml, pyproject.toml, templates/receipt.j2, docs/api.rst | Tier 3 |
| legacy/calc.cob, conf/rules.custom | Tier 4（文法なし） |
| web/src/*.ts, web/tsconfig.json | TypeScript（Tier 1）。意図的な型エラーとオーバーロード宣言 |
| conf/.env | 秘密ファイル（本文を索引しない。仕様書 §13.5） |
