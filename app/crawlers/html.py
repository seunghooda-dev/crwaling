import re

import httpx
from bs4 import BeautifulSoup

from app.crawlers.base import Crawler
from app.crawlers.http import crawler_headers, fetch_with_retry
from app.models import Article, Source
from app.services.quality_service import is_navigation_like_title
from app.text import article_fingerprint, canonicalize_url, normalize_article_url, normalize_space
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
        headers = crawler_headers(str(source.url), "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        response = fetch_with_retry(str(source.url), headers, source)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        articles: list[Article] = []
        seen_urls: set[str] = set()
        for anchor in soup.select(self._selector_for(source)):
            raw_title = self._anchor_title(source, anchor)
            title, summary = self._split_title_summary(source, raw_title)
            href = self._article_href(source, anchor.get("href"))
            if not self._is_candidate(source, title, href):
                continue
            url = normalize_article_url(str(httpx.URL(str(source.url)).join(href)))
            canonical_url = canonicalize_url(url)
            if canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
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
            return "table a[href*='BD_selectBbs.do'], tbody a[href*='BD_selectBbs.do']"
        if "mois.go.kr" in source.url:
            return "a[href*='commonSelectBoardArticle.do'][href*='nttId=']"
        if "weather.go.kr" in source.url:
            return "main a[href], #contents a[href]"
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
        if "news.knn.co.kr" in source.url:
            return "a[href*='/news/article/']"
        if "news.ikbc.co.kr" in source.url:
            return "a[href*='/article/view/']"
        if "tbc.co.kr" in source.url:
            return "a[href*='/news/view']"
        if "tjb.co.kr" in source.url:
            return "a[href*='/issue/view/id/'], a[href*='/category/view/id/']"
        if "jtv.co.kr" in source.url:
            return "a[href*='uid=']"
        if "g1tv.co.kr" in source.url:
            return "a[href*='newsid=']"
        if "ubc.co.kr" in source.url:
            return "a[href*='/wp/archives/']"
        if "cjb.co.kr" in source.url:
            return "a[href*='mod=view'][href*='P_NO=']"
        if "jibs.co.kr" in source.url:
            return "a[href^='javascript:goArticlesDetailPage']"
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
        if title.lower() == "read more" or is_navigation_like_title(title):
            return False
        if "imnews.imbc.com" in source.url:
            return ".html" in href and not any(token in href for token in ("/more/", "/pc_main", "/m_main"))
        if "ytn.co.kr" in source.url:
            return "_ln/" in href or "news_view.php" in href
        if "news.jtbc.co.kr" in source.url:
            return "article" in href or "news_id=" in href
        if "news.kbs.co.kr" in source.url:
            return "view.do?ncd=" in href
        if "weather.go.kr" in source.url:
            return "/w/repositary/xml/wrn/" in href or ("special-report" in href and len(title) > 20)
        if "police.go.kr" in source.url:
            return "BD_selectBbs.do" in href and "q_bbscttSn=" in href
        if "mois.go.kr" in source.url:
            return "commonSelectBoardArticle.do" in href and "nttId=" in href and "bbsId=" in href
        if "safekorea.go.kr" in source.url:
            return "disasterMsg" in href or "emergency" in href or "detail" in href
        if "news.tvchosun.com" in source.url:
            return "/site/data/html_dir/" in href and href.endswith(".html")
        if "ichannela.com" in source.url:
            return "/news/detail/" in href and href.endswith(".do")
        if "mbn.co.kr" in source.url:
            return bool(re.search(r"/news/[^/]+/\d+", href))
        if "news.knn.co.kr" in source.url:
            return "/news/article/" in href
        if "news.ikbc.co.kr" in source.url:
            return "/article/view/" in href
        if "tbc.co.kr" in source.url:
            return "/news/view" in href and "id=" in href
        if "tjb.co.kr" in source.url:
            return bool(re.search(r"/(?:sub\d+/issue|news\d+/category)/view/id/\d+", href))
        if "jtv.co.kr" in source.url:
            return "c=3" in href and "uid=" in href
        if "g1tv.co.kr" in source.url:
            return "/news/" in href and "newsid=" in href
        if "ubc.co.kr" in source.url:
            return bool(re.search(r"/wp/archives/\d+", href))
        if "cjb.co.kr" in source.url:
            return "mod=view" in href and "P_NO=" in href
        if "jibs.co.kr" in source.url:
            return bool(re.search(r"/news/articles/articlesDetail/\d+", href))
        if "yonhapnewstv.co.kr" in source.url:
            return "/news/" in href and not href.rstrip("/").endswith("/news")
        if "nocutnews.co.kr" in source.url:
            return bool(re.fullmatch(r"/news/\d+", href))
        if source.source_category in {"fire", "police", "disaster"}:
            return any(token in href for token in ("view", "bbs", "nttId", "cntId", "detail", ".do", ".jsp"))
        return True

    def _anchor_title(self, source: Source, anchor) -> str:
        if "jibs.co.kr" in source.url:
            jibs_title = self._jibs_anchor_title(anchor)
            if jibs_title:
                return jibs_title

        candidates = [
            anchor.get("title"),
            anchor.get("aria-label"),
            anchor.get("data-title"),
            anchor.get("data-news-title"),
        ]
        image = anchor.find("img")
        if image:
            candidates.extend([image.get("alt"), image.get("title")])

        visible_text = normalize_space(anchor.get_text(" "))
        if "news.kbs.co.kr" in source.url:
            candidates.extend(self._texts_from(anchor, ".tit, .title, .news-tit, .txt"))
        if "imnews.imbc.com" in source.url:
            candidates.extend(self._texts_from(anchor, ".title, .tit, .text_area"))
        if "ytn.co.kr" in source.url:
            candidates.extend(self._texts_from(anchor, ".title, .tit, .txt"))
        if "news.tvchosun.com" in source.url:
            candidates.extend(self._texts_from(anchor, ".title, .tit"))
        if "ichannela.com" in source.url:
            candidates.extend(self._texts_from(anchor, ".tit, .title"))
        if "mbn.co.kr" in source.url:
            candidates.extend(self._texts_from(anchor, ".tit, .title, .news_txt"))
        if "news.knn.co.kr" in source.url:
            candidates.extend(self._texts_from(anchor, ".tit, .title, .news-tit, .subject"))
        if "news.ikbc.co.kr" in source.url:
            candidates.extend(self._texts_from(anchor, ".tit, .title, .news-tit, .subject"))
        if any(domain in source.url for domain in ("tbc.co.kr", "tjb.co.kr", "g1tv.co.kr", "ubc.co.kr", "cjb.co.kr")):
            candidates.extend(self._texts_from(anchor, ".tit, .title, .news-tit, .subject, .txt, .headline"))
        if "ubc.co.kr" in source.url:
            for parent in self._nearby_article_containers(anchor):
                candidates.extend(
                    self._texts_from(
                        parent,
                        ".entry-title, .entry-title a, .tit, .title, .news-tit, .subject, .txt, .headline, h1, h2, h3",
                    )
                )
        if "jibs.co.kr" in source.url:
            for parent in self._nearby_article_containers(anchor):
                candidates.extend(
                    self._texts_from(
                        parent,
                        ".newsMainImgTitle, .newsMainImageTitleDot, .articles-title, .dotdotdot, .title, h3, h4",
                    )
                )
        if "yonhapnewstv.co.kr" in source.url:
            candidates.extend(self._texts_from(anchor, ".tit, .title, .news-tit"))

        candidates.append(visible_text)
        cleaned = [self._clean_anchor_title(value) for value in candidates if value]
        cleaned = [value for value in cleaned if len(value) >= 8]
        if not cleaned:
            return visible_text
        return min(cleaned, key=lambda value: (len(value) > 130, len(value)))

    def _jibs_anchor_title(self, anchor) -> str:
        visible_text = self._clean_anchor_title(anchor.get_text(" "))
        if len(visible_text) >= 8:
            return visible_text

        selectors = (
            ".newsMainHeadLineTitle, .newsMainImgTitle, .newsMainImageTitleDot, "
            ".articles-title, .dotdotdot, .title, h3, h4"
        )
        for parent in self._nearby_article_containers(anchor):
            class_names = set(parent.get("class") or [])
            if "newsarticle-div" in class_names:
                continue
            for text in self._texts_from(parent, selectors):
                cleaned = self._clean_anchor_title(text)
                if 8 <= len(cleaned) <= 160:
                    return cleaned
        return ""

    def _article_href(self, source: Source, href: str | None) -> str | None:
        if not href:
            return href
        href = href.strip()
        if "jibs.co.kr" in source.url:
            match = re.search(r"goArticlesDetailPage\((\d+)\)", href)
            if match:
                return f"/news/articles/articlesDetail/{match.group(1)}"
        return href

    def _nearby_article_containers(self, anchor) -> list:
        containers = []
        article = anchor.find_parent("article")
        if article and article not in containers:
            containers.append(article)
        for class_name in ("item", "newsarticle-div", "articles-detail", "thumbnail"):
            parent = anchor.find_parent(class_=class_name)
            if parent and parent not in containers:
                containers.append(parent)
        if anchor.parent and anchor.parent not in containers:
            containers.append(anchor.parent)
        return containers

    def _texts_from(self, anchor, selector: str) -> list[str]:
        return [normalize_space(item.get_text(" ")) for item in anchor.select(selector)]

    def _clean_anchor_title(self, value: str) -> str:
        text = normalize_space(value)
        if text.lower() in {"read more", "more", "더보기", "자세히 보기"}:
            return ""
        if text in {"이 시각 추천 뉴스 링크", "이슈 기사 링크", "추천 뉴스 링크", "기사 링크"}:
            return ""
        text = re.sub(r"^(동영상|영상|포토|단독|속보)\s+", r"[\1] ", text)
        text = re.sub(r"\s+(재생|보기|바로가기)$", "", text)
        text = re.sub(r"\s*\|\s*[^|]{1,12}$", "", text)
        return text.strip()

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
