import json
from pathlib import Path


def load_notification_config(path: Path = Path("config/notifications.json")) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

