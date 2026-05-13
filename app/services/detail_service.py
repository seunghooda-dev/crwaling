import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.content import clean_html_text, extract_media_urls
from app.repository import get_article, iter_articles, update_article_details
from app.text import normalize_space


ARTICLE_SELECTORS = [
    "article",
    ".article",
    ".news-article",
    ".news_text",
    ".article-body",
    ".articleBody",
    "#articleBody",
    "#news_body_area",
    ".view_content",
    ".cont_view",
    ".body",
]

DOMAIN_LAST_REQUEST: dict[str, float] = {}
DOMAIN_DELAY_SECONDS = 1.0


def enrich_article_details(conn, article_id: int) -> bool:
    article = get_article(conn, article_id)
    if article is None:
        return False
    return _enrich_article(conn, article)


def enrich_missing_details(conn, limit: int = 50) -> int:
    count = 0
    for article in iter_articles(conn, limit=limit):
        if article.get("body_text"):
            continue
        if _enrich_article(conn, article):
            count += 1
    conn.commit()
    return count


def _enrich_article(conn, article: dict) -> bool:
    url = article.get("url")
    if not url:
        return False

    _respect_domain_delay(url)
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    response = httpx.get(url, headers=headers, timeout=settings.request_timeout_seconds, follow_redirects=True)
    response.raise_for_status()

    html = response.text
    if len(html.encode("utf-8", errors="ignore")) > settings.max_raw_html_bytes:
        html = html[: settings.max_raw_html_bytes]
    raw_path = _save_raw_html(article["id"], url, html)
    soup = BeautifulSoup(html, "html.parser")
    body_text = _extract_body_text(soup) or clean_html_text(article.get("summary"), max_length=4000)
    author = _extract_author(soup) or article.get("author")
    image_urls, video_urls = extract_media_urls(html)
    if not image_urls:
        image_urls = _extract_meta_images(soup)
    update_article_details(
        conn,
        article["id"],
        body_text,
        author,
        json.dumps(image_urls[:12], ensure_ascii=False),
        json.dumps(video_urls[:12], ensure_ascii=False),
        raw_path,
    )
    return True


def _extract_body_text(soup: BeautifulSoup) -> str | None:
    for selector in ARTICLE_SELECTORS:
        node = soup.select_one(selector)
        if not node:
            continue
        text = clean_html_text(str(node), max_length=4000)
        if text and len(text) >= 80:
            return text
    paragraphs = [normalize_space(p.get_text(" ")) for p in soup.select("p")]
    text = normalize_space(" ".join(p for p in paragraphs if len(p) >= 20))
    if len(text) >= 80:
        return text[:4000]
    return None


def _extract_author(soup: BeautifulSoup) -> str | None:
    candidates = [
        soup.select_one("[name='author']"),
        soup.select_one("[property='article:author']"),
        soup.select_one(".author"),
        soup.select_one(".reporter"),
        soup.select_one(".byline"),
    ]
    for node in candidates:
        if not node:
            continue
        value = node.get("content") or node.get_text(" ")
        value = normalize_space(value)
        if value:
            return value[:80]
    return None


def _extract_meta_images(soup: BeautifulSoup) -> list[str]:
    urls = []
    for selector in ["[property='og:image']", "[name='twitter:image']"]:
        node = soup.select_one(selector)
        if node and node.get("content"):
            urls.append(node.get("content"))
    return sorted(set(urls))


def _save_raw_html(article_id: int, url: str, html: str) -> str:
    host = urlparse(url).netloc.replace(":", "_") or "unknown"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    path = Path(settings.raw_html_dir) / f"{article_id}-{host}-{digest}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8", errors="ignore")
    return str(path)


def _respect_domain_delay(url: str) -> None:
    domain = urlparse(url).netloc
    now = time.monotonic()
    last = DOMAIN_LAST_REQUEST.get(domain)
    if last is not None:
        wait = DOMAIN_DELAY_SECONDS - (now - last)
        if wait > 0:
            time.sleep(wait)
    DOMAIN_LAST_REQUEST[domain] = time.monotonic()
