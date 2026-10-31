---
skill_id: no-automatic-ci
category: process
maturity: confirmed
domain_tags: [ci, github-actions, notification, verification]
created_at: 2026-10-31
proposed_by: human
approved_by: human
approved_at: 2026-10-31
source_sprint: m1-impl-20261029
---

# Lucianke では CI を自動で走らせない（手動の起動だけ）

## 師匠の言葉（原文 2026-10-31）

> ciのメールが頻繁に来て邪魔くさいので対処してほしい。
> そもそもCIしなくていいよ、なんか必須の理由があるの？

## 内容

- `.github/workflows/ci.yml` は `workflow_dispatch` だけ（push・プルリクエスト・夜間では走らせない）。検証は手元の全体テスト（`uv run pytest -q`）と `build.ps1` の smoke で行う。
- CI でしか得られないのは Linux・macOS での確認（仕様書 M1-7）だけで、配布は Windows の exe なので必須ではない。OSS 公開の前など、3 OS の確認が要るときだけ手動で起動する。
- `release.yml`（`v*` のタグのときだけ）は別。タグを打つときに exe を作って Release に上げる。

## 適用

- 新しいワークフローを足すときも、自動起動（push・schedule）にしない。通知のメールで師匠の手を煩わせない。
- 「CI で確かめる」前提の手順を書かない。確かめは手元で自動で通す。
