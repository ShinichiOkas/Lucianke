"""実プロセスの ``lucianke`` サーバーを起こす共通の起動コマンド（BUG-0030）。

テスト（e2e・不変条件・契約の実プロセス）はこれで子プロセスを起こす。``uv run ... lucianke`` や ``.venv/Scripts/lucianke.exe``
（コンソールスクリプト）では起こさない:

* ``uv run`` は起動のたびに環境を同期する。``pyproject.toml`` が変わっていると、editable の ``lucianke`` を入れ直すために
  ``Scripts/lucianke.exe`` を消そうとするが、**動いている exe は Windows では消せない**（os error 32）。誰かがサーバーを
  動かしている間は、全員の ``uv run`` がテストの前に落ちる。
* ``sys.executable -m lucianke`` なら、``lucianke.exe`` を握らない（握るのは ``python.exe``。同期は python のバージョンが
  変わらない限り触らない）。子は親（pytest）と同じ環境で動くので、環境は親の ``uv run`` が同期した直後のもの。

``-P`` は ``python -m`` が cwd を ``sys.path`` に足さないようにする。サーバーの cwd はワークスペース（F-POLY など任意の
Python ファイルを含む）なので、足すとそこの ``mcp.py`` などが SDK を隠しうる。コンソールスクリプトは元々足さない。

bench 側の同等物は ``bench/_stdio.py`` の ``server_argv``（同じ形に保つ。``tests/unit/test_server_launch_guard.py`` が見張る）。
"""

from __future__ import annotations

import sys


def server_argv(*extra: str) -> list[str]:
    """サーバーを stdio で起こす argv。``extra`` はサブコマンド（``doctor`` など）。"""
    return [sys.executable, "-P", "-m", "lucianke", *extra]
