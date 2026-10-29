# BUG-0018 revision が WORKING_TREE 以外だと E_UNSUPPORTED（存在しない参照でも）、TOML・Markdown・拡張子なしの骨格も E_UNSUPPORTED

- 報告: 2026-10-29 14:31 / 見た人: ドッグフーディング担当
- 症状 1: 仕様書 §5.2 は `revision` に `HEAD` / `STAGED` / ブランチ名 / コミット SHA を許し、解決できなければ `E_REVISION_NOT_FOUND`。実際は `HEAD`・`STAGED`・存在しない `no-such-rev-xyz` のすべてが `E_UNSUPPORTED`（`revision 'HEAD' is not supported yet (WORKING_TREE only)`）。存在しない参照名も `E_REVISION_NOT_FOUND` にならない。
- 症状 2: 仕様書 §11 T-02 は XML/JSON/YAML/TOML と未知言語（Tier 4）も骨格を返す。実際の `get_file_skeleton` は `uv.lock`（toml）・`README.md`（markdown）・`LICENSE`（unknown）を `E_UNSUPPORTED`（`language 'toml' has no structural support in this build (supported: javascript, python, tsx, typescript)`）で返す。
- 再現手順: `get_file_skeleton {"paths":["src/lucianke/core/urn.py"],"revision":"HEAD"}`、`{"paths":["uv.lock"]}`、`{"paths":["README.md"]}`、`{"paths":["LICENSE"]}`（`bench/dogfood/run_dogfood.py 5 6`）。
- 頻度: 上記 7 呼び出し中 7。
- 備考: 作業ツリーには `syntax/adapters/json_.py` などが未コミットで存在する（結線前の可能性）。「未実装で既知」なら閉じてよい。
- 調査はしていない（直す役の仕事）。
