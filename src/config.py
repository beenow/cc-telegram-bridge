"""
config.py — Load and validate all configuration from .env
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        print(f"ERROR: Required environment variable '{key}' is not set.", file=sys.stderr)
        print(f"       Copy .env.example to .env and fill in your values.", file=sys.stderr)
        sys.exit(1)
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _optional_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("true", "1", "yes")


def _optional_int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, str(default)).strip())
    except ValueError:
        return default


@dataclass
class Config:
    # Required
    telegram_bot_token: str
    allowed_user_ids: set[int]

    # Claude CLI
    default_model: str = "sonnet"

    # Trading system integration (optional — disable before open-source publish)
    trading_system_enabled: bool = False
    trading_system_log_dir: str = ""
    trading_system_config_path: str = ""

    # Paths
    data_dir: str = "./data"
    log_dir: str = "./logs"
    downloads_dir: str = "./downloads"
    log_level: str = "INFO"

    # System prompt appended to Claude's default
    system_prompt: str = ""


def load_config() -> Config:
    telegram_bot_token = _require("TELEGRAM_BOT_TOKEN")

    raw_ids = _require("ALLOWED_USER_IDS")
    try:
        allowed_user_ids = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip()}
    except ValueError:
        print("ERROR: ALLOWED_USER_IDS must be a comma-separated list of integers.", file=sys.stderr)
        sys.exit(1)

    default_model = _optional("DEFAULT_MODEL", "sonnet")
    data_dir = _optional("DATA_DIR", "./data")
    log_dir = _optional("LOG_DIR", "./logs")
    downloads_dir = _optional("DOWNLOADS_DIR", "./downloads")
    log_level = _optional("LOG_LEVEL", "INFO").upper()

    # Trading system integration
    trading_enabled = _optional_bool("TRADING_SYSTEM_ENABLED", False)
    trading_log_dir = _optional("TRADING_SYSTEM_LOG_DIR", "")
    trading_config = _optional("TRADING_SYSTEM_CONFIG_PATH", "")

    # Load soul.md — personality and identity layer
    soul_path = Path(__file__).parent.parent / "soul.md"
    soul = soul_path.read_text(encoding="utf-8").strip() if soul_path.exists() else ""

    # Build system prompt: soul first, then any runtime context
    prompt_parts = [soul] if soul else []
    if trading_enabled:
        prompt_parts.append(
            "You also have access to the user's live trading engine. "
            "When asked about trading system status, check the relevant log files."
        )
    system_prompt = "\n\n".join(prompt_parts)

    return Config(
        telegram_bot_token=telegram_bot_token,
        allowed_user_ids=allowed_user_ids,
        default_model=default_model,
        trading_system_enabled=trading_enabled,
        trading_system_log_dir=trading_log_dir,
        trading_system_config_path=trading_config,
        data_dir=data_dir,
        log_dir=log_dir,
        downloads_dir=downloads_dir,
        log_level=log_level,
        system_prompt=system_prompt,
    )
