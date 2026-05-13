from time import sleep
import re

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.crawlers.base import Crawler
from app.models import Article, Source
from app.text import article_fingerprint, canonicalize_url, normalize_space
from app.title_extractor import split_title_summary


KBS_SUMMARY_MARKERS = (
    "KBSLIFE",
    "[안전토크]",
    "이와 관련",
    "고령 운전자의",
    "일본 지진의",
)


class HtmlCrawler(Crawler):
    """Generic fallback crawler for simple list pages.

    실제 방송국 운영에서는 사이트별 parser adapter를 추가하는 방식으로 확장합니다.
    """

    def crawl(self, source: Source) -> list[Article]:
        headers = {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        response = self._get_with_retry(str(source.url), headers, source)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        articles: list[Article] = []
        for anchor in soup.select(self._selector_for(source)):
            raw_title = normalize_space(anchor.get_text(" "))
            title, summary = self._split_title_summary(source, raw_title)
            href = anchor.get("href")
            if not self._is_candidate(source, title, href):
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
                    summary=summary,
                )
            )
        return articles[:100]

    def _selector_for(self, source: Source) -> str:
        if "nfa.go.kr" in source.url:
            return "a[href*='mode=view'], a[href*='cntId=']"
        if "police.go.kr" in source.url:
            return "a[href*='bbs'], a[href*='BD_selectBbs']"
        if "mois.go.kr" in source.url:
            return "a[href*='bbsId'], a[href*='nttId']"
        if "weather.go.kr" in source.url:
            return "a[href], td a[href]"
        if "news.kbs.co.kr" in source.url:
            return "a[href*='view.do?ncd=']"
        if "imnews.imbc.com" in source.url:
            return "a[href*='.html']"
        if "ytn.co.kr" in source.url:
            return "a[href*='_ln/'], a[href*='news_view.php'], a[href*='/_ln/']"
        if "news.jtbc.co.kr" in source.url:
            return "a[href*='article'], a[href*='news_id=']"
        if "news.tvchosun.com" in source.url:
            return "a[href*='/site/data/html_dir/']"
        if "ichannela.com" in source.url:
            return "a[href*='/news/detail/']"
        if "mbn.co.kr" in source.url:
            return "a[href*='/news/']"
        if "yonhapnewstv.co.kr" in source.url:
            return "a[href*='/news/']"
        if "nocutnews.co.kr" in source.url:
            return "a[href^='/news/']"
        if "safekorea.go.kr" in source.url or "d.kbs.co.kr" in source.url:
            return "a[href]"
        return "a[href]"

    def _is_candidate(self, source: Source, title: str, href: str | None) -> bool:
        if not title or not href or len(title) < 8:
            return False
        bad_words = ("로그인", "회원가입", "사이트맵", "개인정보", "이메일", "바로가기", "메뉴", "검색")
        if any(word in title for word in bad_words):
            return False
        if "imnews.imbc.com" in source.url:
            return ".html" in href and not any(token in href for token in ("/more/", "/pc_main", "/m_main"))
        if "ytn.co.kr" in source.url:
            return "_ln/" in href or "news_view.php" in href
        if "news.jtbc.co.kr" in source.url:
            return "article" in href or "news_id=" in href
        if "news.kbs.co.kr" in source.url:
            return "view.do?ncd=" in href
        if "news.tvchosun.com" in source.url:
            return "/site/data/html_dir/" in href and href.endswith(".html")
        if "ichannela.com" in source.url:
            return "/news/detail/" in href and href.endswith(".do")
        if "mbn.co.kr" in source.url:
            return bool(re.search(r"/news/[^/]+/\d+", href))
        if "yonhapnewstv.co.kr" in source.url:
            return "/news/" in href and not href.rstrip("/").endswith("/news")
        if "nocutnews.co.kr" in source.url:
            return bool(re.fullmatch(r"/news/\d+", href))
        if source.source_category in {"fire", "police", "disaster"}:
            return any(token in href for token in ("view", "bbs", "nttId", "cntId", "detail", ".do", ".jsp"))
        return True

    def _split_title_summary(self, source: Source, text: str) -> tuple[str, str | None]:
        if "d.kbs.co.kr" not in source.url:
            return split_title_summary(text)

        cleaned = text
        published = None
        if len(cleaned) > 18 and cleaned[-1] == ")" and "." in cleaned[-20:]:
            maybe_date = cleaned[-18:].strip()
            if len(maybe_date) == 18 and maybe_date[4] == "." and maybe_date[7] == ".":
                published = maybe_date
                cleaned = normalize_space(cleaned[:-18])

        split_at = -1
        for marker in KBS_SUMMARY_MARKERS:
            index = cleaned.find(marker)
            if index > 18 and (split_at == -1 or index < split_at):
                split_at = index

        if split_at == -1 and len(cleaned) > 90:
            split_at = cleaned.rfind(" ", 0, 90)

        if split_at == -1:
            return text, None

        title, summary = split_title_summary(cleaned[:split_at], cleaned[split_at:])
        if published:
            summary = normalize_space(f"{summary} {published}")
        return title, summary or None

    def _get_with_retry(self, url: str, headers: dict[str, str], source: Source) -> httpx.Response:
        last_error: Exception | None = None
        retries = source.max_retries if source.max_retries is not None else 2
        timeout = source.timeout_seconds if source.timeout_seconds is not None else settings.request_timeout_seconds
        for attempt in range(retries + 1):
            try:
                return httpx.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=True,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                sleep(0.6 * (attempt + 1))
        if last_error is not None:
            raise last_error
        raise RuntimeError("HTML request failed")
