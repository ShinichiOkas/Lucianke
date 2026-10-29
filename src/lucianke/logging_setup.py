"""ログ（設計書 §9.2、仕様書 §12.6）。

* stderr: 人が読む 1 行（スタックは出さない）。stdout には何も出さない（NFR-OPS-1）。**書き込みで止まらない**: ホストが stderr のパイプを
  読まないと（Windows のパイプは約 4 KB）、同期の書き込みがバッファ満杯でイベントループごと止まる（BUG-0020）。stderr へは別スレッドの
  書き手（``NonBlockingLineHandler``）が書き、詰まったら溢れた行を捨てる（ファイルのログには全行が残る）。
* ファイル: ``<cache_root>/logs/lucianke-<日付>.log``（JSON Lines、日次、既定 7 日保持）。各行は ``event`` と ``request_id`` を持つ。
  スタックはここにだけ出す。ファイル本文・``new_code`` は載せない（呼び出し側の責任）。
* ログ先を作れなくても落ちない（stderr のみで続行）。

標準ライブラリだけを import する。``bootstrap_io()`` の後に呼ぶこと（stderr は fd 2 のまま）。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from lucianke.config import Settings

_LEVELS = {"error": logging.ERROR, "warn": logging.WARNING, "info": logging.INFO, "debug": logging.DEBUG}
_LOG_NAME_RE = re.compile(r"^lucianke-(\d{4}-\d{2}-\d{2})\.log$")
_ATTACHED = ("lucianke", "mcp")  # 自前と SDK のロガーに同じハンドラを付ける

# extra= で渡されたらそのまま JSON に載せる項目（設計書 §9.2「必ず残すフィールド」。lsp は lsp_language -> "lsp.language"）
_FIELDS = ("tool", "workspace_id", "timing_ms", "confidence", "error_code", "edit_id", "log_ref")
_DOTTED = {"lsp_language": "lsp.language", "lsp_state": "lsp.state", "call_args": "args"}  # LogRecord.args と衝突するため call_args


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        doc: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(record.created, _dt.UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": getattr(record, "event", None) or record.name,
            "request_id": getattr(record, "request_id", None),
            "logger": record.name,
            "msg": message,
        }
        for key in _FIELDS:
            if (value := getattr(record, key, None)) is not None:
                doc[key] = value
        for attr, key in _DOTTED.items():
            if (value := getattr(record, attr, None)) is not None:
                doc[key] = value
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """``HH:MM:SS LEVEL logger: message [tool=… request_id=…]``。1 行（スタックは出さない）。"""

    def format(self, record: logging.LogRecord) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(record.created))
        parts = [f"{t} {record.levelname:<5} {record.name}: {record.getMessage()}"]
        for key in ("tool", "error_code", "timing_ms", "request_id", "log_ref"):
            if (value := getattr(record, key, None)) is not None:
                parts.append(f"{key}={value}")
        line = " ".join(parts)
        if record.exc_info and record.exc_info[1] is not None:
            line += f" ({type(record.exc_info[1]).__name__})"
        return line.replace("\r", " ").replace("\n", " ")


STDERR_QUEUE_MAX = 512  # stderr の書き手が詰まっている間に溜める行数の上限（約 60 KB）。超えた行は捨てる


class NonBlockingLineHandler(logging.Handler):
    """整形した 1 行を、別スレッドの書き手（``write``）に渡す。``emit`` は書き込みを待たない。

    ``write`` が止まっても（読まれない stderr のパイプが満杯など）呼び出し元は止まらない。書き手が戻れなかった間に ``queue_max`` 行を
    超えた分は捨て、書き手が戻ったら「N 行捨てた」と 1 行出す。書き手のスレッドは daemon（プロセスの終了を待たせない）。
    ``flush`` / ``close`` は溜まった行が書かれるのを ``drain_s`` 秒だけ待つ。
    """

    def __init__(self, write: Callable[[str], None], *, queue_max: int = STDERR_QUEUE_MAX, drain_s: float = 0.5) -> None:
        super().__init__()
        self._write = write
        self._queue_max = queue_max
        self._drain_s = drain_s
        self._lines: deque[str] = deque()
        self._dropped = 0
        self._writing = False
        self._closed = False
        self._cv = threading.Condition()
        self._thread = threading.Thread(target=self._pump, name="lucianke-stderr", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)
            return
        with self._cv:
            if self._closed:
                return
            if len(self._lines) >= self._queue_max:
                self._dropped += 1
                return
            self._lines.append(line)
            self._cv.notify_all()

    def _pump(self) -> None:
        while True:
            with self._cv:
                while not self._lines and not self._closed:
                    self._cv.wait()
                if not self._lines:
                    return
                batch = list(self._lines)
                self._lines.clear()
                dropped, self._dropped = self._dropped, 0
                self._writing = True
            try:
                if dropped:
                    batch.append(f"{time.strftime('%H:%M:%S')} WARN  lucianke.logging: {dropped} log lines dropped (stderr is not being read)")
                self._write("\n".join(batch) + "\n")
            except Exception:  # noqa: BLE001  stderr が壊れても本処理を落とさない（書けない行は捨てる）
                pass
            finally:
                with self._cv:
                    self._writing = False
                    self._cv.notify_all()

    def flush(self) -> None:
        deadline = time.monotonic() + self._drain_s
        with self._cv:
            while (self._lines or self._writing) and (left := deadline - time.monotonic()) > 0:
                self._cv.wait(left)

    def close(self) -> None:
        self.flush()
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        super().close()


class _FdWriter:
    """fd へ直接書く（``sys.stderr`` の内部ロックを使わない）。書き手が詰まったままでも、終了時の ``sys.stderr.flush()`` が巻き込まれない。"""

    def __init__(self, fd: int, encoding: str) -> None:
        self._fd = fd
        self._encoding = encoding

    def __call__(self, text: str) -> None:
        data = memoryview(text.encode(self._encoding, "replace"))
        while data:
            data = data[os.write(self._fd, data) :]


def _stderr_handler() -> logging.Handler:
    """本物の stderr 用。fd が取れれば非ブロッキングの書き手、取れなければ同期の ``StreamHandler``。"""
    try:
        fd = sys.stderr.fileno()
        os.fstat(fd)
    except (AttributeError, OSError, ValueError):
        return logging.StreamHandler(sys.stderr)
    return NonBlockingLineHandler(_FdWriter(fd, getattr(sys.stderr, "encoding", None) or "utf-8"))


class DailyJsonlHandler(logging.Handler):
    """``lucianke-YYYY-MM-DD.log`` に追記する。日付が変わったら次のファイルへ切り替え、古いものを消す。"""

    def __init__(self, log_dir: Path, keep_days: int) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.keep_days = keep_days
        self._date: str | None = None
        self._stream: TextIO | None = None
        self.setFormatter(JsonLinesFormatter())

    def _path_for(self, date: str) -> Path:
        return self.log_dir / f"lucianke-{date}.log"

    def prune(self, today: _dt.date) -> None:
        cutoff = today - _dt.timedelta(days=self.keep_days - 1)
        for path in self.log_dir.glob("lucianke-*.log"):
            m = _LOG_NAME_RE.match(path.name)
            if not m:
                continue
            try:
                if _dt.date.fromisoformat(m.group(1)) < cutoff:
                    path.unlink()
            except (ValueError, OSError):
                continue

    def _open(self, date: str) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stream = self._path_for(date).open("a", encoding="utf-8", newline="\n")
        self._date = date
        self.prune(_dt.date.fromisoformat(date))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            date = _dt.date.fromtimestamp(record.created).isoformat()
            if self._stream is None or date != self._date:
                self._open(date)
            assert self._stream is not None
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:  # noqa: BLE001  ログの失敗で本処理を落とさない
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def setup_logging(
    settings: Settings, *, stream: TextIO | None = None, names: tuple[str, ...] = _ATTACHED
) -> logging.Logger:
    """stderr + JSON Lines ファイルのハンドラを ``names`` のロガーに付ける。何度呼んでも付け直す（重複しない）。"""
    level = _LEVELS.get(settings.log_level, logging.INFO)
    handlers: list[logging.Handler] = []
    human = logging.StreamHandler(stream) if stream is not None else _stderr_handler()
    human.setFormatter(HumanFormatter())
    handlers.append(human)
    file_error: OSError | None = None
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(DailyJsonlHandler(settings.log_dir, settings.log_keep_days))
    except OSError as exc:  # ログ先を作れない（読み取り専用など）。stderr のみで続行
        file_error = exc
    for i, name in enumerate(names):  # 先頭が自前のロガー。残り（SDK など）は警告以上だけ（debug のときは debug）
        logger = logging.getLogger(name)
        for old in list(logger.handlers):
            logger.removeHandler(old)
            old.close()
        for h in handlers:
            logger.addHandler(h)
        logger.setLevel(level if i == 0 or level == logging.DEBUG else max(level, logging.WARNING))
        logger.propagate = False
    log = logging.getLogger(names[0])
    if file_error is not None:
        log.warning("log file disabled: %s", file_error, extra={"event": "log_file_disabled"})
    return log
