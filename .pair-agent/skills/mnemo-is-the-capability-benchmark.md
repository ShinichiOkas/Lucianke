---
skill_id: mnemo-is-the-capability-benchmark
category: domain
maturity: confirmed
domain_tags: [benchmark, evaluation, dogfood, capability, mnemo]
created_at: 2026-10-31
proposed_by: human
approved_by: human
approved_at: 2026-10-31
source_sprint: m1-impl-20261029
---

# Lucianke の能力測定の基準は「Mnemo の分析」

## 師匠の言葉（原文 2026-10-31）

> 折角なので../Mnemo/を実験対象にしてMnemo 側にのPROJECT_STRUCTURE.md の末尾に分析情報を加えておいて
> それを能力測定の基準にするといいと思う。

## 内容

- Lucianke の能力（呼ぶ側の AI にとっての使い物度）は、**実プロジェクト Mnemo（`../Mnemo`）を分析させた結果**で測る。
  合成のフィクスチャや F-PY/F-TS の事後条件だけでなく、AI が実際にやる調べもの（地図・検索・骨格・定義・本体・参照・呼び出し階層・診断・名前変更の試し）を固定の台本で流し、
  **正解（grep と人手で確かめた期待値）との一致・速さ・トークン**を採点する。
- Mnemo は固定のコミットに対して測る（版ごとに比べられるように）。Mnemo のパスはリポジトリに焼き込まない（`LUCIANKE_BENCH_MNEMO`、既定は `../Mnemo`）。
- 分析の結果は Mnemo 側の `PROJECT_STRUCTURE.md` の末尾に「Lucianke による構造分析」として残す（Mnemo の利用者にも役に立つ形で）。
- **置き場所（師匠の判断 2026-10-31）**: Mnemo の中身（パス・行番号・正解・結果）を含むものは **Mnemo のリポジトリの `bench/lucianke/`** に置く。
  Lucianke のリポジトリ（先々 OSS）には Mnemo の中身を含まない**汎用の実行器と adapter だけ**を置く（別のプロジェクトの中身を持ち込まない）。
- **競合**: Python は Serena（oraios/serena。MCP で LSP ベースの記号ツール）と比べる（師匠の承認 2026-10-31「導入してよい」）。
  隔離した uv の環境に版を固定して入れ、資格情報は渡さない。基準の列として Grep/Read の素の方法も並べる。

## 適用

- 使い勝手に効く変更（参照の解決・予算・出力の形・LSP の扱い）を入れたら、この bench を流して点数の上下を見る。下がったら票にする。
- 新しい調べものの種類を足すときは、Mnemo の上で正解を確かめてから台本に入れる。

関連: ペア固有 Skill `the-tools-primary-user-decides-its-spec`
