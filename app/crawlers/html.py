from time import sleep

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.crawlers.base import Crawler
from app.models import Article, Source
from app.text import article_fingerprint, canonicalize_url, normalize_space


class HtmlCrawler(Crawler):
    """Generic fallback crawler for simple list pages.

    실제 방송국 운영에서는 사이트별 parser adapter를 추가하는 방식으로 확장합니다.
    """

    def crawl(self, source: Source) -> list[Article]:
        headers = {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        response = self._get_with_retry(str(source.url), headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        articles: list[Article] = []
        for anchor in soup.select("a[href]"):
            title = normalize_space(anchor.get_text(" "))
            href = anchor.get("href")
            if not title or not href or len(title) < 8:
                continue
            url = str(httpx.URL(str(source.url)).join(href))
            canonical_url = canonicalize_url(url)
            articles.append(
                Article(
                    source_name=source.name,
                    source_type=source.source_type.value,
                    source_category=source.source_category,
                    title=title,
                    url=url,
                    canonical_url=canonical_url,
                    fingerprint=article_fingerprint(title, source.name),
                    duplicate_group_id=article_fingerprint(title, "global")[:16],
                )
            )
        return articles[:100]

    def _get_with_retry(self, url: str, headers: dict[str, str]) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return httpx.get(
                    url,
                    headers=headers,
                    timeout=settings.request_timeout_seconds,
                    follow_redirects=True,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                sleep(0.6 * (attempt + 1))
        if last_error is not None:
            raise last_error
        raise RuntimeError("HTML request failed")
