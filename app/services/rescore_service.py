import json
import sqlite3

from app.content import clean_html_text, extract_media_urls
from app.models import Article
from app.repository import get_article, update_article_quality, update_article_score, update_article_summary_and_media
from app.services.alert_service import record_alert_if_needed
from app.services.quality_service import checklist_json, enrich_article_quality
from app.services.scoring import apply_newsroom_scoring


def rescore_articles(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT * FROM articles").fetchall()
    for row in rows:
        cleaned_summary = clean_html_text(row["summary"])
        image_urls, video_urls = extract_media_urls(row["summary"])
        update_article_summary_and_media(
            conn,
            row["id"],
            cleaned_summary,
            json.dumps(image_urls, ensure_ascii=False),
            json.dumps(video_urls, ensure_ascii=False),
        )
        article = Article(
            source_name=row["source_name"],
            source_type=row["source_type"],
            source_category=row["source_category"],
            title=row["title"],
            url=row["url"],
            fingerprint=row["fingerprint"],
            summary=cleaned_summary,
            body_text=row["body_text"],
            verification_status=row["verification_status"],
        )
        scored = apply_newsroom_scoring(article)
        scored = enrich_article_quality(scored)
        update_article_score(
            conn,
            row["id"],
            json.dumps(scored.keywords, ensure_ascii=False),
            scored.importance_score,
            scored.verification_status,
        )
        update_article_quality(
            conn,
            row["id"],
            json.dumps(scored.region_tags, ensure_ascii=False),
            scored.quality_score,
            json.dumps(scored.quality_flags, ensure_ascii=False),
            checklist_json(),
        )
        stored = get_article(conn, row["id"])
        if stored:
            record_alert_if_needed(conn, stored)
    conn.commit()
    return len(rows)
