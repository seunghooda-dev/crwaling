from datetime import datetime

from dateutil import parser as date_parser

from app.author import clean_author_display
from app.crawlers.base import Crawler
from app.crawlers.http import crawler_headers, fetch_with_retry
from app.models import Article, Source
from app.text import article_fingerprint, canonicalize_url, normalize_space


class JtbcApiCrawler(Crawler):
    """Crawler for JTBC's current article list API."""

    def crawl(self, source: Source) -> list[Article]:
        headers = crawler_headers(str(source.url), "application/json, text/plain, */*")
        headers["Referer"] = "https://news.jtbc.co.kr/sections"
        response = fetch_with_retry(str(source.url), headers, source)
        response.raise_for_status()
        payload = response.json()

        articles: list[Article] = []
        for item in payload.get("data", {}).get("list", []):
            article = self._article_from_item(source, item)
            if article:
                articles.append(article)
        return articles[:100]

    def _article_from_item(self, source: Source, item: dict) -> Article | None:
        article_id = normalize_space(item.get("articleIdx", ""))
        title = normalize_space(item.get("articleTitle") or item.get("articleMobileTitle") or "")
        if not article_id or not title:
            return None

        url = f"https://news.jtbc.co.kr/article/{article_id}"
        body_text = normalize_space(item.get("articleInnerTextContent", ""))
        published_at = _parse_date(item.get("publicationDate"))
        published_date = published_at.date().isoformat() if published_at else None
        image_urls = [item["articleThumbnailImgUrl"]] if item.get("articleThumbnailImgUrl") else []

        return Article(
            source_name=source.name,
            source_type=source.source_type.value,
            source_category=source.source_category,
            title=title,
            url=url,
            canonical_url=canonicalize_url(url),
            author=clean_author_display(item.get("journalistName")),
            published_at=published_at,
            body_text=body_text or None,
            summary=body_text[:300] if body_text else None,
            image_urls=image_urls,
            fingerprint=article_fingerprint(title, source.name, published_date),
            duplicate_group_id=article_fingerprint(title, "global", published_date)[:16],
        )


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return date_parser.parse(value)
    except (TypeError, ValueError):
        return None
