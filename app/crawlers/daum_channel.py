from datetime import datetime
from urllib.parse import parse_qs, urlparse

from app.crawlers.base import Crawler
from app.crawlers.http import crawler_headers, fetch_with_retry
from app.models import Article, Source
from app.text import article_fingerprint, canonicalize_url, normalize_space


class DaumChannelCrawler(Crawler):
    """Crawler for Daum media channel JSON feeds."""

    def crawl(self, source: Source) -> list[Article]:
        headers = crawler_headers(str(source.url), "application/json, text/plain, */*")
        cp_id = _query_value(str(source.url), "cpId")
        if cp_id:
            headers["Referer"] = f"https://v.daum.net/channel/{cp_id}/list"

        response = fetch_with_retry(str(source.url), headers, source)
        response.raise_for_status()
        payload = response.json()

        articles: list[Article] = []
        for item in payload.get("items", []):
            article = self._article_from_item(source, item)
            if article:
                articles.append(article)
        return articles[:100]

    def _article_from_item(self, source: Source, item: dict) -> Article | None:
        title = normalize_space(item.get("title", ""))
        url = item.get("pcLink") or item.get("mobileLink")
        if not title or not url:
            return None

        published_at = _parse_epoch_millis(item.get("createDt"))
        published_date = published_at.date().isoformat() if published_at else None
        image_urls = [item["thumbnail"]] if item.get("thumbnail") else []
        canonical_url = canonicalize_url(str(url))

        return Article(
            source_name=source.name,
            source_type=source.source_type.value,
            source_category=source.source_category,
            title=title,
            url=str(url),
            canonical_url=canonical_url,
            published_at=published_at,
            image_urls=image_urls,
            fingerprint=article_fingerprint(title, source.name, published_date),
            duplicate_group_id=article_fingerprint(title, "global", published_date)[:16],
        )


def _query_value(url: str, key: str) -> str | None:
    values = parse_qs(urlparse(url).query).get(key)
    return values[0] if values else None


def _parse_epoch_millis(value: int | float | str | None) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000)
    except (TypeError, ValueError, OSError):
        return None
