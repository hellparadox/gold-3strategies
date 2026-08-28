"""Core package for gold_m5_bot.

Houses the YAML settings loader (with environment-variable overrides for
secrets) and the loguru bootstrap used by every entrypoint.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv
from loguru import logger

__all__ = ["PROJECT_ROOT", "DEFAULT_CONFIG_PATH", "Settings", "setup_logging"]

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "settings.yaml"

# Load .env (if present) BEFORE any Settings instance is created, so
# TELEGRAM_TOKEN / MT5_PASSWORD / … overrides are available everywhere.
load_dotenv(PROJECT_ROOT / ".env")

_ENV_OVERRIDES: Dict[str, str] = {
    "MT5_LOGIN": "mt5.login",
    "MT5_PASSWORD": "mt5.password",
    "MT5_SERVER": "mt5.server",
    "MT5_PATH": "mt5.terminal_path",
    "TELEGRAM_TOKEN": "telegram.token",
}


class Settings:
    """Thin, dotted-path read-only view over the parsed YAML tree."""

    __slots__ = ("_data", "path")

    def __init__(self, data: Dict[str, Any], path: Optional[Path] = None) -> None:
        self._data: Dict[str, Any] = data or {}
        self.path: Optional[Path] = path

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: Optional[os.PathLike[str] | str] = None) -> "Settings":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(f"settings file not found: {cfg_path}")
        with cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError("settings.yaml must deserialize to a mapping")
        instance = cls(data, cfg_path)
        instance._apply_env_overrides()
        return instance

    def _apply_env_overrides(self) -> None:
        for env_key, dotted in _ENV_OVERRIDES.items():
            raw = os.environ.get(env_key)
            if raw is None or raw == "":
                continue
            value: Any = raw
            if dotted == "mt5.login":
                try:
                    value = int(raw)
                except ValueError:
                    logger.warning("MT5_LOGIN='{}' is not an int, ignored", raw)
                    continue
            self.set(dotted, value)

        # FIX: token_env (نام متغیر اختصاصی هر نمونه) اولویت بالاتری از
        # TELEGRAM_TOKEN عمومی دارد. قبلاً اگر TELEGRAM_TOKEN در محیط بود،
        # نمونه ۲ هرگز TELEGRAM_TOKEN_ICHIMOKU را نمی‌گرفت و با توکن ربات
        # نمونه ۱ بالا می‌آمد؛ حالا یک .env مشترک برای هر دو نمونه کافی است.
        token_env = self.get("telegram.token_env", "")
        if token_env:
            t = os.environ.get(token_env, "")
            if t:
                self.set("telegram.token", t)

        admin_env = os.environ.get("ADMIN_IDS", "").strip()
        if admin_env:
            ids: List[int] = []
            for chunk in admin_env.replace(";", ",").split(","):
                chunk = chunk.strip()
                if chunk.lstrip("-").isdigit():
                    ids.append(int(chunk))
            if ids:
                self.set("telegram.admin_ids", ids)

    # ------------------------------------------------------------- accessors
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node: Dict[str, Any] = self._data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def section(self, name: str) -> Dict[str, Any]:
        value = self.get(name, {})
        return dict(value) if isinstance(value, dict) else {}

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, None)
        if value in (None, "", [], {}):
            raise KeyError(f"missing required setting: {dotted}")
        return value

    def as_dict(self) -> Dict[str, Any]:
        return self._data

    def __getitem__(self, dotted: str) -> Any:
        return self.require(dotted)

    def __contains__(self, dotted: str) -> bool:
        return self.get(dotted, None) is not None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Settings(path={self.path}, sections={list(self._data)})"


def setup_logging(settings: Optional[Settings] = None) -> None:
    """Configure loguru with a coloured console sink and a rotating file sink."""
    level = "INFO"
    log_path = "logs/bot_{time:YYYY-MM-DD}.log"
    rotation = "10 MB"
    retention = "21 days"
    backtrace = True
    diagnose = False

    if settings is not None:
        level = str(settings.get("logging.level", level)).upper()
        log_path = str(settings.get("logging.path", log_path))
        rotation = str(settings.get("logging.rotation", rotation))
        retention = str(settings.get("logging.retention", retention))
        backtrace = bool(settings.get("logging.backtrace", backtrace))
        diagnose = bool(settings.get("logging.diagnose", diagnose))

    resolved = Path(log_path)
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
        ),
    )
    logger.add(
        str(resolved),
        level=level,
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True,          # thread-safe: required, we log from 2+ threads
        backtrace=backtrace,
        diagnose=diagnose,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {thread.name} | {name}:{function}:{line} - {message}",
    )
    logger.debug("logging initialised (level={}, file={})", level, resolved)
