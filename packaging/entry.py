"""PyInstaller の入口（``packaging/lucianke.spec``）。``lucianke`` コマンド（``lucianke.__main__:main``）と同じ。"""

from lucianke.__main__ import main

raise SystemExit(main())
