import json
import re
from pathlib import Path

from app.config import settings
from app.models import Article


def save_article_snapshot(stored_article: dict, article: Article) -> Path | None:
    settings.raw_html_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = settings.raw_html_dir / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    article_id = stored_article.get("id")
    if article_id is None:
        return None

    source_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", article.source_name).strip("-")[:60] or "source"
    path = snapshot_dir / f"{article_id}-{source_slug}.json"
    payload = {
        "id": article_id,
        "source_name": article.source_name,
        "source_category": article.source_category,
        "title": article.title,
        "url": article.url,
        "canonical_url": article.canonical_url,
        "published_at": article.published_at.isoformat() if article.published_at else None,
        "summary": article.summary,
        "body_text": article.body_text,
        "keywords": article.keywords,
        "image_urls": article.image_urls,
        "video_urls": article.video_urls,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) > settings.max_raw_html_bytes:
        payload["body_text"] = (payload.get("body_text") or "")[: settings.max_raw_html_bytes // 4]
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    return path
