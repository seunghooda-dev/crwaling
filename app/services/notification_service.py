import smtplib
import logging
from email.message import EmailMessage

import httpx

from app.notification_loader import load_notification_config
from app.text import normalize_space


logger = logging.getLogger("notification")
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


class NotificationConfigError(RuntimeError):
    pass


class NotificationSendError(RuntimeError):
    pass


def notify_alert(article: dict, reason: str) -> None:
    config = load_notification_config()
    if not config:
        return
    text = _format_alert(article, reason)
    try:
        _send_chat_notifications(config, text)
        if config.get("email_to") and config.get("smtp_host"):
            _send_email(config, text)
    except Exception as exc:
        logger.warning("alert notification failed: %s", exc)


def notify_search_matches(articles: list[dict], query: str | None) -> int:
    query = normalize_space(query or "")
    if not query:
        return 0
    config = load_notification_config()
    if not config:
        return 0

    matched = [article for article in articles if _article_matches(article, query)]
    for article in matched:
        text = _format_search_match(article, query)
        try:
            _send_chat_notifications(config, text)
            if config.get("email_to") and config.get("smtp_host"):
                _send_email(config, text)
        except Exception as exc:
            logger.warning("search notification failed: %s", exc)
    return len(matched)


def notification_status(config: dict | None = None) -> dict:
    config = config if config is not None else load_notification_config()
    telegram_token = normalize_space(config.get("telegram_bot_token") or "")
    telegram_chat_id = normalize_space(str(config.get("telegram_chat_id") or ""))
    return {
        "telegram": {
            "configured": bool(telegram_token and telegram_chat_id),
            "chat_id": _mask_destination(telegram_chat_id),
            "message_preview": not bool(config.get("telegram_disable_web_page_preview")),
        }
    }


def send_article_to_telegram(article: dict, note: str | None = None, config: dict | None = None) -> dict:
    config = config if config is not None else load_notification_config()
    _require_telegram_config(config)
    text = _format_article_for_telegram(article, note)
    response = _send_telegram_message(
        config,
        text,
        disable_web_page_preview=bool(config.get("telegram_disable_web_page_preview")),
    )
    return {
        "ok": True,
        "channel": "telegram",
        "destination": _mask_destination(str(config.get("telegram_chat_id") or "")),
        "message_length": len(text),
        "telegram_response": response,
    }


def send_telegram_test_message(config: dict | None = None) -> dict:
    config = config if config is not None else load_notification_config()
    _require_telegram_config(config)
    text = "[Newsroom Monitor] 텔레그램 연결 테스트\n설정이 정상입니다."
    response = _send_telegram_message(config, text, disable_web_page_preview=True)
    return {
        "ok": True,
        "channel": "telegram",
        "destination": _mask_destination(str(config.get("telegram_chat_id") or "")),
        "message_length": len(text),
        "telegram_response": response,
    }


def _format_alert(article: dict, reason: str) -> str:
    return (
        f"[Newsroom Alert] {article.get('title')}\n"
        f"Source: {article.get('source_name')}\n"
        f"Score: {article.get('importance_score')}\n"
        f"Reason: {reason}\n"
        f"URL: {article.get('url')}"
    )


def _format_search_match(article: dict, query: str) -> str:
    return (
        f"[Newsroom Search] '{query}' 새 기사 감지\n"
        f"Title: {article.get('title')}\n"
        f"Source: {article.get('source_name')}\n"
        f"Published: {article.get('published_at') or article.get('collected_at')}\n"
        f"URL: {article.get('url')}"
    )


def _article_matches(article: dict, query: str) -> bool:
    haystack = normalize_space(
        " ".join(
            str(article.get(field) or "")
            for field in ("title", "summary", "body_text", "keywords")
        )
    ).lower()
    return query.lower() in haystack


def _send_chat_notifications(config: dict, text: str) -> None:
    webhook_url = config.get("webhook_url")
    if webhook_url:
        httpx.post(webhook_url, json={"text": text}, timeout=10)

    telegram_bot_token = config.get("telegram_bot_token")
    telegram_chat_id = config.get("telegram_chat_id")
    if telegram_bot_token and telegram_chat_id:
        _send_telegram_message(config, text, disable_web_page_preview=True)

    kakao_webhook_url = config.get("kakao_webhook_url")
    if kakao_webhook_url:
        httpx.post(kakao_webhook_url, json={"text": text}, timeout=10)


def _format_article_for_telegram(article: dict, note: str | None = None) -> str:
    body = normalize_space(article.get("body_text") or article.get("summary") or "")
    if len(body) > 1200:
        body = body[:1200].rstrip() + "..."
    keywords = _json_list(article.get("keywords"))
    regions = _json_list(article.get("region_tags"))
    lines = [
        "[Newsroom Article]",
        f"제목: {article.get('title')}",
        f"출처: {article.get('source_name')}",
        f"기자: {article.get('author') or '기자명 없음'}",
        f"발행/수집: {article.get('published_at') or article.get('collected_at') or '-'}",
        f"중요도: {float(article.get('importance_score') or 0):.1f}",
        f"검증: {article.get('verification_status') or 'unchecked'}",
    ]
    if regions:
        lines.append(f"지역: {', '.join(regions[:5])}")
    if keywords:
        lines.append(f"키워드: {', '.join(keywords[:8])}")
    if note:
        lines.append(f"메모: {normalize_space(note)}")
    if body:
        lines.extend(["", body])
    lines.extend(["", f"원문: {article.get('url')}"])
    text = "\n".join(str(line) for line in lines if line is not None)
    return text[:TELEGRAM_MAX_MESSAGE_LENGTH]


def _send_telegram_message(config: dict, text: str, *, disable_web_page_preview: bool) -> dict:
    _require_telegram_config(config)
    token = normalize_space(config.get("telegram_bot_token") or "")
    chat_id = normalize_space(str(config.get("telegram_chat_id") or ""))
    payload = {
        "chat_id": chat_id,
        "text": text[:TELEGRAM_MAX_MESSAGE_LENGTH],
        "link_preview_options": {"is_disabled": bool(disable_web_page_preview)},
    }
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise NotificationSendError(f"텔레그램 전송 실패: {exc}") from exc
    except ValueError as exc:
        raise NotificationSendError("텔레그램 응답을 해석하지 못했습니다.") from exc
    if isinstance(data, dict) and data.get("ok") is False:
        description = data.get("description") or "unknown error"
        raise NotificationSendError(f"텔레그램 전송 실패: {description}")
    return data if isinstance(data, dict) else {"response": data}


def _require_telegram_config(config: dict) -> None:
    if not config.get("telegram_bot_token") or not config.get("telegram_chat_id"):
        raise NotificationConfigError(
            "텔레그램 설정이 필요합니다. config/notifications.json 또는 NEWSROOM_TELEGRAM_BOT_TOKEN, NEWSROOM_TELEGRAM_CHAT_ID를 설정하세요."
        )


def _mask_destination(value: str) -> str:
    value = normalize_space(value)
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}***{value[-2:]}"


def _json_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    try:
        import json

        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _send_email(config: dict, text: str) -> None:
    message = EmailMessage()
    message["Subject"] = "[Newsroom Alert] 중요 기사 감지"
    message["From"] = config.get("email_from") or config.get("smtp_user")
    message["To"] = config["email_to"]
    message.set_content(text)

    port = int(config.get("smtp_port") or 587)
    with smtplib.SMTP(config["smtp_host"], port, timeout=10) as smtp:
        smtp.starttls()
        if config.get("smtp_user"):
            smtp.login(config["smtp_user"], config.get("smtp_password", ""))
        smtp.send_message(message)
