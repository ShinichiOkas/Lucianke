"""``config.py``（cache_root・水準・開発用ツール）と ``logging_setup.py``（stderr 1 行 + JSON Lines 日次ファイル）。"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
from pathlib import Path

import pytest

from lucianke.config import Settings, default_cache_root, load_settings, normalize_log_level
from lucianke.logging_setup import DailyJsonlHandler, setup_logging


def test_cache_root_follows_os_conventions():
    win = {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    assert default_cache_root(win, "win32") == Path(r"C:\Users\u\AppData\Local") / "lucianke" / "cache"
    home = {"HOME": "/home/u"}
    assert default_cache_root(home, "linux") == Path("/home/u/.cache/lucianke")
    assert default_cache_root(home, "darwin") == Path("/home/u/Library/Caches/lucianke")


def test_cache_dir_override_wins():
    assert default_cache_root({"LUCIANKE_CACHE_DIR": "/x/y", "LOCALAPPDATA": "/z"}, "win32") == Path("/x/y")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "info"), ("", "info"), ("DEBUG", "debug"), ("warning", "warn"), (" Error ", "error"), ("loud", "info")],
)
def test_log_level_normalization(raw, expected):
    assert normalize_log_level(raw) == expected


def test_load_settings_reads_dev_tools_flag():
    assert load_settings({"LUCIANKE_CACHE_DIR": "/c"}).dev_tools is False
    s = load_settings({"LUCIANKE_CACHE_DIR": "/c", "LUCIANKE_DEV_TOOLS": "1", "LUCIANKE_LOG_LEVEL": "debug"})
    assert s.dev_tools is True and s.log_level == "debug" and s.log_dir == Path("/c/logs")


@pytest.fixture
def names():
    """グローバルなロガー（lucianke / mcp）を汚さない専用の名前。"""
    n = "lucianke_test_logging"
    yield (n,)
    lg = logging.getLogger(n)
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()


def test_setup_writes_human_line_to_stream_and_json_lines_to_dated_file(tmp_path, names):
    stream = io.StringIO()
    log = setup_logging(Settings(cache_root=tmp_path, log_level="info"), stream=stream, names=names)
    assert log.name == names[0]
    lg = logging.getLogger(names[0])
    try:
        raise RuntimeError("inner")
    except RuntimeError:
        lg.error("failed %s", "x", exc_info=True, extra={"event": "boom", "request_id": "r1", "tool": "t", "call_args": {"a": 1},
                                                          "lsp_language": "python"})  # fmt: skip
    lg.debug("hidden at info")
    human = stream.getvalue().splitlines()
    assert len(human) == 1 and "failed x" in human[0] and "request_id=r1" in human[0] and "RuntimeError" in human[0]
    assert "Traceback" not in human[0]  # スタックはファイルだけ
    (path,) = (tmp_path / "logs").glob("lucianke-*.log")
    assert path.name == f"lucianke-{dt.date.today().isoformat()}.log"
    (doc,) = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert doc["event"] == "boom" and doc["request_id"] == "r1" and doc["tool"] == "t" and doc["args"] == {"a": 1}
    assert doc["lsp.language"] == "python" and "Traceback" in doc["exc"] and doc["level"] == "error"


def test_every_line_has_event_and_request_id_even_without_extra(tmp_path, names):
    setup_logging(Settings(cache_root=tmp_path), stream=io.StringIO(), names=names)
    logging.getLogger(names[0]).warning("plain")
    (path,) = (tmp_path / "logs").glob("lucianke-*.log")
    doc = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert doc["event"] == names[0] and doc["request_id"] is None


def test_debug_level_is_honoured(tmp_path, names):
    stream = io.StringIO()
    setup_logging(Settings(cache_root=tmp_path, log_level="debug"), stream=stream, names=names)
    logging.getLogger(names[0]).debug("visible")
    assert "visible" in stream.getvalue()


def test_setup_twice_does_not_duplicate_handlers(tmp_path, names):
    stream = io.StringIO()
    for _ in range(2):
        setup_logging(Settings(cache_root=tmp_path), stream=stream, names=names)
    logging.getLogger(names[0]).info("once")
    assert stream.getvalue().count("once") == 1


def test_unwritable_log_dir_falls_back_to_stderr_only(tmp_path, names):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")  # cache_root がファイル → logs/ を作れない
    stream = io.StringIO()
    setup_logging(Settings(cache_root=blocker), stream=stream, names=names)
    logging.getLogger(names[0]).info("still works")
    assert "still works" in stream.getvalue()


def test_old_log_files_are_pruned_to_keep_days(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    today = dt.date(2026, 10, 29)
    for back in (0, 3, 6, 7, 30):
        (logs / f"lucianke-{(today - dt.timedelta(days=back)).isoformat()}.log").write_text("{}\n", encoding="utf-8")
    (logs / "other.log").write_text("keep", encoding="utf-8")
    h = DailyJsonlHandler(logs, keep_days=7)
    h.prune(today)
    left = sorted(p.name for p in logs.iterdir())
    assert left == ["lucianke-2026-10-23.log", "lucianke-2026-10-26.log", "lucianke-2026-10-29.log", "other.log"]
    h.close()
