import json
import os
from pathlib import Path


def load_notification_config(path: Path = Path("config/notifications.json")) -> dict:
    config = {}
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
    env_config = {
        "telegram_bot_token": os.getenv("NEWSROOM_TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("NEWSROOM_TELEGRAM_CHAT_ID", ""),
    }
    for key, value in env_config.items():
        if value:
            config[key] = value
    if os.getenv("NEWSROOM_TELEGRAM_DISABLE_PREVIEW"):
        config["telegram_disable_web_page_preview"] = _env_bool("NEWSROOM_TELEGRAM_DISABLE_PREVIEW")
    return config


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
