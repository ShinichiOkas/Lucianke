"""実行時設定（設計書 §9.3、仕様書 §4.5・§12.7）。

ここは環境変数と OS の慣例から ``Settings`` を作るだけ。``.lucianke.toml`` の読み込みはワークスペース層（後続）の仕事。
標準ライブラリだけを import する（起動の初期に使うため）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LOG_LEVELS = ("error", "warn", "info", "debug")
DEFAULT_LOG_LEVEL = "info"
LOG_KEEP_DAYS = 7  # 仕様書 §12.6: 既定 7 日保持


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    cache_root: Path
    log_level: str = DEFAULT_LOG_LEVEL  # "error" | "warn" | "info" | "debug"
    dev_tools: bool = False  # LUCIANKE_DEV_TOOLS=1: 開発用ツール（ping）を tools/list に載せる
    log_keep_days: int = LOG_KEEP_DAYS

    @property
    def log_dir(self) -> Path:
        return self.cache_root / "logs"


def default_cache_root(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """仕様書 §4.5: ``%LOCALAPPDATA%\\lucianke\\cache`` / ``~/Library/Caches/lucianke`` / ``~/.cache/lucianke``。

    ``LUCIANKE_CACHE_DIR`` があればそれを優先する。リポジトリ内には何も書かない（MUST）。
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    override = env.get("LUCIANKE_CACHE_DIR")
    if override:
        return Path(override)
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "lucianke" / "cache"
    if platform == "darwin":
        return home / "Library" / "Caches" / "lucianke"
    return home / ".cache" / "lucianke"


def normalize_log_level(raw: str | None) -> str:
    """``LUCIANKE_LOG_LEVEL`` を 4 水準のどれかにする。解釈できなければ既定（info）。"""
    if not raw:
        return DEFAULT_LOG_LEVEL
    value = raw.strip().lower()
    if value == "warning":
        value = "warn"
    return value if value in LOG_LEVELS else DEFAULT_LOG_LEVEL


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env: Mapping[str, str] | None = None, platform: str | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        cache_root=default_cache_root(env, platform),
        log_level=normalize_log_level(env.get("LUCIANKE_LOG_LEVEL")),
        dev_tools=_truthy(env.get("LUCIANKE_DEV_TOOLS")),
    )
