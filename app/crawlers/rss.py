from datetime import datetime

import feedparser
from dateutil import parser as date_parser

from app.content import clean_html_text, extract_media_urls
from app.crawlers.base import Crawler
from app.crawlers.http import crawler_headers, fetch_with_retry
from app.models import Article, Source
from app.text import article_fingerprint, canonicalize_url, normalize_space
from app.title_extractor import split_title_summary


class RssCrawler(Crawler):
    def crawl(self, source: Source) -> list[Article]:
        headers = crawler_headers(str(source.url), "application/rss+xml, application/xml, text/xml, */*")
        response = fetch_with_retry(str(source.url), headers, source)
        response.raise_for_status()
        feed = feedparser.parse(response.text)

        articles: list[Article] = []
        for entry in feed.entries:
            raw_title = normalize_space(getattr(entry, "title", ""))
            link = getattr(entry, "link", "")
            if not raw_title or not link:
                continue

            published_at = self._parse_date(getattr(entry, "published", None) or getattr(entry, "updated", None))
            canonical_url = canonicalize_url(link)
            published_date = published_at.date().isoformat() if published_at else None
            raw_summary = getattr(entry, "summary", "")
            image_urls, video_urls = extract_media_urls(raw_summary)
            title, summary = split_title_summary(raw_title, clean_html_text(raw_summary))
            articles.append(
                Article(
                    source_name=source.name,
                    source_type=source.source_type.value,
                    source_category=source.source_category,
                    title=title,
                    url=link,
                    canonical_url=canonical_url,
                    author=getattr(entry, "author", None),
                    published_at=published_at,
                    summary=summary,
                    image_urls=image_urls,
                    video_urls=video_urls,
                    fingerprint=article_fingerprint(title, source.name, published_date),
                    duplicate_group_id=article_fingerprint(title, "global", published_date)[:16],
                )
            )
        return articles

    def _parse_date(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return date_parser.parse(value)
        except (TypeError, ValueError):
            return None
