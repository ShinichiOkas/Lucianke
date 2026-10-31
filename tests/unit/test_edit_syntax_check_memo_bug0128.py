"""構文の確かめ（``edit/syntax_check``）は同じ内容・言語の解析を覚えて使い回す（BUG-0128）。

``check_after`` は編集の前と後の両方を解析する。前の内容は同じファイルの前の編集の後の内容（続けて編集する）や、前の呼び出しの前の内容
（``dry_run`` の繰り返し）であることが多く、T-10 は呼び出しのたびに解析し直していた。期限で打ち切った解析は覚えない。
"""

from __future__ import annotations

from typing import Any

import pytest

from lucianke.edit import syntax_check as sc

BEFORE = b"def f():\n    return 1\n\n\ndef g():\n    return 2\n"
AFTER = b"def f():\n    return 10\n\n\ndef g():\n    return 2\n"


class Counting:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, timed_out: bool = False) -> None:
        self.sources: list[bytes] = []
        real = sc.parse_source

        def counted(source: bytes, language_id: str, sha: str, **kw: Any) -> Any:
            self.sources.append(source)
            parsed, stopped = real(source, language_id, sha, **kw)
            return parsed, (True if timed_out else stopped)

        monkeypatch.setattr(sc, "parse_source", counted)
        monkeypatch.setattr(sc, "_ANALYSIS_MEMO", sc.BoundedMemo(max_entries=32, max_weight=1 << 24))


def test_a_repeated_edit_parses_each_content_once(monkeypatch: pytest.MonkeyPatch) -> None:
    count = Counting(monkeypatch)
    first = sc.check_after("python", BEFORE, AFTER)
    assert first.status == "ok"
    assert sorted(count.sources) == sorted([BEFORE, AFTER])
    again = sc.check_after("python", BEFORE, AFTER)  # 同じ dry_run の繰り返し
    assert again == first and len(count.sources) == 2
    following = AFTER.replace(b"return 2", b"return 20")
    assert sc.check_after("python", AFTER, following).status == "ok"  # 続けて編集: 前の内容は前の後の内容
    assert count.sources[2:] == [following]


def test_a_broken_edit_still_fails_from_the_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    count = Counting(monkeypatch)
    broken = b"def f(:\n    return 1\n"
    for _ in range(2):
        result = sc.check_after("python", BEFORE, broken)
        assert result.status == "failed" and result.new_errors
    assert len(count.sources) == 2


def test_a_parse_cut_by_the_deadline_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    count = Counting(monkeypatch, timed_out=True)
    for _ in range(2):
        assert sc.check_after("python", BEFORE, AFTER).status == "failed"
    assert len(count.sources) == 4  # 前と後を 2 回とも解析し直す
