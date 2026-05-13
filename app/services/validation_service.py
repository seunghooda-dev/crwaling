import json
from pathlib import Path
from urllib.parse import urlparse

from app.models import SourceType


def validate_all() -> list[str]:
    errors: list[str] = []
    errors.extend(validate_sources())
    errors.extend(validate_keywords())
    errors.extend(validate_notifications())
    return errors


def validate_sources(path: Path = Path("config/sources.json")) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"{path} not found"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{path}: invalid JSON: {exc}"]
    if not isinstance(payload, list):
        return [f"{path}: root must be a list"]
    names = set()
    for index, item in enumerate(payload):
        prefix = f"{path}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        name = item.get("name")
        if not name:
            errors.append(f"{prefix}: missing name")
        elif name in names:
            errors.append(f"{prefix}: duplicate source name {name}")
        names.add(name)
        source_type = item.get("source_type")
        if source_type not in {member.value for member in SourceType}:
            errors.append(f"{prefix}: invalid source_type {source_type}")
        source_category = item.get("source_category", "news")
        if not isinstance(source_category, str) or not source_category:
            errors.append(f"{prefix}: invalid source_category {source_category}")
        url = item.get("url")
        if not urlparse(str(url)).scheme.startswith("http"):
            errors.append(f"{prefix}: invalid url {url}")
        timeout_seconds = item.get("timeout_seconds")
        if timeout_seconds is not None:
            try:
                if float(timeout_seconds) <= 0:
                    errors.append(f"{prefix}: timeout_seconds must be positive")
            except (TypeError, ValueError):
                errors.append(f"{prefix}: timeout_seconds must be a number")
        max_retries = item.get("max_retries")
        if max_retries is not None:
            try:
                if int(max_retries) < 0:
                    errors.append(f"{prefix}: max_retries must be 0 or greater")
            except (TypeError, ValueError):
                errors.append(f"{prefix}: max_retries must be an integer")
    return errors


def validate_keywords(path: Path = Path("config/keywords.json")) -> list[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{path}: invalid JSON: {exc}"]
    errors = []
    if not isinstance(payload, dict):
        return [f"{path}: root must be an object"]
    for group, keywords in payload.items():
        if not isinstance(keywords, list) or not all(isinstance(item, str) and item for item in keywords):
            errors.append(f"{path}.{group}: must be a list of non-empty strings")
    return errors


def validate_notifications(path: Path = Path("config/notifications.json")) -> list[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{path}: invalid JSON: {exc}"]
    errors = []
    if payload.get("webhook_url") and not urlparse(str(payload["webhook_url"])).scheme.startswith("http"):
        errors.append(f"{path}: invalid webhook_url")
    if payload.get("smtp_host") and not payload.get("email_to"):
        errors.append(f"{path}: email_to is required when smtp_host is set")
    return errors
