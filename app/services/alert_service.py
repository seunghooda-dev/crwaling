import sqlite3

from app.repository import insert_alert_event
from app.services.notification_service import notify_alert


def record_alert_if_needed(conn: sqlite3.Connection, article: dict, threshold: float = 4.5) -> bool:
    score = float(article.get("importance_score") or 0)
    verification_status = article.get("verification_status")
    if score >= threshold:
        reason = f"importance_score >= {threshold}"
        inserted = insert_alert_event(conn, article, reason)
        if inserted:
            notify_alert(article, reason)
        return inserted
    if verification_status == "needs_review":
        reason = "verification needs review"
        inserted = insert_alert_event(conn, article, reason)
        if inserted:
            notify_alert(article, reason)
        return inserted
    return False
