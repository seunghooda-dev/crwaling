import smtplib
from email.message import EmailMessage

import httpx

from app.notification_loader import load_notification_config


def notify_alert(article: dict, reason: str) -> None:
    config = load_notification_config()
    if not config:
        return
    text = _format_alert(article, reason)
    webhook_url = config.get("webhook_url")
    if webhook_url:
        httpx.post(webhook_url, json={"text": text}, timeout=10)
    if config.get("email_to") and config.get("smtp_host"):
        _send_email(config, text)


def _format_alert(article: dict, reason: str) -> str:
    return (
        f"[Newsroom Alert] {article.get('title')}\n"
        f"Source: {article.get('source_name')}\n"
        f"Score: {article.get('importance_score')}\n"
        f"Reason: {reason}\n"
        f"URL: {article.get('url')}"
    )


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
