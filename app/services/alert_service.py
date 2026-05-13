import sqlite3

from app.repository import insert_alert_event
from app.services.notification_service import notify_alert


CATEGORY_THRESHOLDS = {
    "disaster": 3.5,
    "fire": 3.5,
    "police": 4.0,
    "weather": 3.0,
    "health": 4.0,
    "government": 4.5,
    "news": 4.5,
}


def record_alert_if_needed(conn: sqlite3.Connection, article: dict, threshold: float | None = None) -> bool:
    score = float(article.get("importance_score") or 0)
    verification_status = article.get("verification_status")
    category = article.get("source_category") or "news"
    threshold = threshold if threshold is not None else CATEGORY_THRESHOLDS.get(category, 4.5)
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
