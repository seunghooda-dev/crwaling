import smtplib
from email.message import EmailMessage

import httpx

from app.notification_loader import load_notification_config
from app.text import normalize_space


def notify_alert(article: dict, reason: str) -> None:
    config = load_notification_config()
    if not config:
        return
    text = _format_alert(article, reason)
    _send_chat_notifications(config, text)
    if config.get("email_to") and config.get("smtp_host"):
        _send_email(config, text)


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
        _send_chat_notifications(config, text)
        if config.get("email_to") and config.get("smtp_host"):
            _send_email(config, text)
    return len(matched)


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
        httpx.post(
            f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage",
            json={"chat_id": telegram_chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )

    kakao_webhook_url = config.get("kakao_webhook_url")
    if kakao_webhook_url:
        httpx.post(kakao_webhook_url, json={"text": text}, timeout=10)


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
