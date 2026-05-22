import hashlib
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.author import clean_author_display, infer_reporter_from_article_text
from app.config import settings
from app.content import clean_html_text, extract_media_urls
from app.models import Article
from app.repository import (
    article_needs_detail,
    get_article,
    list_detail_enrichment_queue,
    update_article_details,
    update_article_quality,
    update_article_score,
)
from app.services.alert_service import record_alert_if_needed
from app.services.quality_service import checklist_json, enrich_article_quality
from app.services.scoring import apply_newsroom_scoring
from app.text import normalize_space


ARTICLE_SELECTORS = [
    ".item-main .contents .text-box",
    ".contents .text-box",
    "article",
    ".article",
    ".news-article",
    "[itemprop='articleBody']",
    "#body_wrap .content",
    ".body-wrap .content",
    ".contents_body",
    ".articles-detail",
    ".article_view",
    ".article-view",
    ".article_text",
    ".article-text",
    ".article_txt",
    ".article_cont",
    ".article-content",
    ".article_body",
    ".news_text",
    ".news_view",
    ".news-content",
    ".newsct_article",
    ".article-body",
    ".article-body__content",
    ".articleBody",
    "#articleBody",
    "#article_body",
    "#news_body_area",
    ".view_content",
    ".view_con",
    ".cont_view",
    ".entry-content",
    ".board-view",
    ".cont-body",
    ".body",
]

TRUSTED_BODY_SELECTORS = [
    "[itemprop='articleBody']",
    ".main_text .text_area",
    ".main_text",
    "#articleBody",
    "#article_body",
    "#news_body_area",
    ".article-body",
    ".article_body",
]

BODY_MIN_LENGTH = 80
BODY_MAX_LENGTH = 4000
BODY_NOISE_SELECTORS = [
    "script",
    "style",
    "noscript",
    "iframe",
    "nav",
    "aside",
    "footer",
    "form",
    "button",
    ".view-title",
    ".editor",
    ".copyrights",
    ".copyrightsbs",
    ".item-side",
    ".news-list",
    ".ad",
    ".ads",
    ".advertisement",
    ".banner",
    ".share",
    ".sns",
    ".social",
    ".reply",
    ".comment",
    ".comments",
    ".related",
    ".recommend",
    ".popular",
    ".copyright",
    ".tag",
    ".tags",
    ".pagination",
    ".article-relation",
    ".relation",
    ".reporter_info",
    ".reporter_wrap",
    ".byline",
    "[data-nosnippet]",
]
BODY_NOISE_WORDS = (
    "로그인",
    "회원가입",
    "구독",
    "공유",
    "댓글",
    "목록",
    "이전 기사",
    "다음 기사",
    "관련기사",
    "추천기사",
    "인기기사",
    "많이 본 기사",
    "최근 24시간",
    "매너봇",
    "저작권",
    "무단전재",
)

DOMAIN_LAST_REQUEST: dict[str, float] = {}
DOMAIN_DELAY_SECONDS = 1.0

AUTHOR_BLOCK_SELECTORS = [
    ".reporter_info .name",
    ".reporter_info",
    ".reporter_wrap",
    ".view-title .editor .reporter",
    ".view-title .editor .name-box",
    ".editor .name-box .reporter",
    ".editor .reporter",
    ".reporter-name",
    ".reporter_name",
    ".reporter_nm",
    ".articles-detail-attach-right",
    ".article-reporter",
    ".article_reporter",
    ".news-reporter",
    ".news-byline",
    ".news_writer",
    ".article_writer",
    ".view_writer",
    ".writer_name",
    ".writer-name",
    ".journalist",
    ".journalist-name",
    ".by_line",
    ".byline_name",
    ".article-author",
    ".author-name",
    ".view-info",
    ".view_info",
    ".view_head",
    ".article_info",
    ".article-info",
    ".news_info",
    ".news-info",
    ".reporter",
    ".byline",
    ".author",
    ".writer",
    "[itemprop='author']",
]
AUTHOR_CONTEXT_SELECTORS = [
    ".entry-content",
    ".board-view",
    ".cont-body",
    "#scontainer",
    ".contents_body",
    ".articles-detail",
    ".article-body",
    ".articleBody",
    "#articleBody",
    "#news_body_area",
    ".view_content",
    ".cont_view",
    "article",
]
AUTHOR_META_SELECTORS = [
    "[name='author']",
    "[name='Author']",
    "[name='byl']",
    "[name='dable:author']",
    "[name='article:author']",
    "[property='article:author']",
    "[property='og:article:author']",
    "[property='og:author']",
    "[property='dable:author']",
    "[name='twitter:creator']",
]
AUTHOR_SITE_VALUES = {
    "admin",
    "by admin",
    "kbc광주방송",
    "kbc",
    "knn",
    "tbc",
    "tjb",
    "jtv",
    "g1",
    "g1tv",
    "ubc",
    "cjb",
    "jibs",
    "www.jtv.co.kr",
}
AUTHOR_NOISE_NAMES = {
    "관리자",
    "보도국",
    "뉴스팀",
    "편집부",
    "취재팀",
    "영상취재",
    "사진",
    "영상",
    "자료",
    "뉴스",
    "방송",
    "지역",
    "현장",
    "앵커",
    "제보",
    "댓글",
    "기사",
    "작성",
    "입력",
    "출력",
    "관련",
    "해당",
    "대통령",
    "위원장",
    "경찰",
    "소방",
    "기상",
    "사건",
    "사고",
    "오늘",
    "내일",
    "남성",
    "여성",
}
AUTHOR_NOISE_FRAGMENTS = (
    "방송",
    "보도국",
    "뉴스",
    "취재",
    "기자협회",
    "기자회견",
    "기자별",
    "추천기사",
    "인기기사",
    "많이 본 기사",
    "취재윤리",
    "독자권익",
    "로그인",
    "회원가입",
)
REPORTER_PATTERNS = [
    re.compile(r"([가-힣]{2,5})\s*\([^)]*@[^)]*\)\s*기자(?![가-힣])"),
    re.compile(r"([가-힣]{2,5})\s*\[[^\]]*@[^\]]*\]\s*기자(?![가-힣])"),
    re.compile(r"([가-힣]{2,5})\s*기자(?![가-힣])"),
    re.compile(r"([가-힣]{2,5})\s+[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
]
WRITER_PATTERNS = [
    re.compile(r"(?:작성자|담당기자|취재기자|취재|글|기자명)\s*[:：]?\s*([가-힣]{2,5})(?:\s*기자)?(?=\s|작성일|입력|수정|$)"),
    re.compile(r"(?:입력|수정)\s*[:：]?\s*\d{4}[^가-힣]{0,30}([가-힣]{2,5})\s*기자"),
    re.compile(r"([가-힣]{2,5})\s*기자의\s*기사\s*더보기"),
    re.compile(r"^([가-힣]{2,5})\s*입력\s*[:：]"),
]
SCRIPT_AUTHOR_PATTERNS = [
    re.compile(
        r"""["']?(?:reporterName|reporter_name|reporterNm|reporter_nm|reporter|writerName|writer_name|writer|authorName|author_name|author|byline|journalist|mngName|userNm|ARTI_REPORTER_NM|ARTI_AUTHOR)["']?\s*[:=]\s*["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    re.compile(r"""["']reporterName["']\s*:\s*["']([^"']+)["']"""),
    re.compile(r"""["']reporter_name["']\s*:\s*["']([^"']+)["']"""),
    re.compile(r"""["']authorName["']\s*:\s*["']([^"']+)["']"""),
    re.compile(r"""["']author["']\s*:\s*\{[^{}]*["']name["']\s*:\s*["']([^"']+)["']"""),
]


def enrich_article_details(conn, article_id: int) -> bool:
    article = get_article(conn, article_id)
    if article is None:
        return False
    return _enrich_article(conn, article)


def enrich_missing_detail_report(
    conn,
    limit: int = 50,
    *,
    priority_only: bool = True,
    missing: str | None = None,
    source_name: str | None = None,
) -> dict:
    report = {
        "requested": limit,
        "priority_only": priority_only,
        "missing": missing or "all",
        "source_name": source_name,
        "candidates": 0,
        "processed": 0,
        "enriched": 0,
        "body_added": 0,
        "author_added": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
    }
    logger = logging.getLogger("detail_crawler")
    queue = list_detail_enrichment_queue(
        conn,
        limit=limit,
        priority_only=priority_only,
        missing=missing,
        source_name=source_name,
    )
    report["candidates"] = len(queue)
    for article in queue:
        if not article_needs_detail(article):
            report["skipped"] += 1
            continue
        before_body = normalize_space(article.get("body_text") or "")
        before_author = clean_author_display(article.get("author"))
        try:
            if _enrich_article(conn, article):
                report["processed"] += 1
                updated = get_article(conn, article["id"]) or {}
                after_body = normalize_space(updated.get("body_text") or "")
                after_author = clean_author_display(updated.get("author"))
                body_added = len(before_body) < BODY_MIN_LENGTH and len(after_body) >= BODY_MIN_LENGTH
                author_added = not before_author and bool(after_author)
                if body_added:
                    report["body_added"] += 1
                if author_added:
                    report["author_added"] += 1
                if body_added or author_added:
                    report["enriched"] += 1
                else:
                    report["unchanged"] += 1
        except Exception as exc:
            report["failed"] += 1
            if len(report["failures"]) < 5:
                report["failures"].append(
                    {
                        "id": article.get("id"),
                        "source_name": article.get("source_name"),
                        "title": article.get("title"),
                        "error": str(exc)[:240],
                    }
                )
            logger.exception("detail enrich failed article_id=%s url=%s", article.get("id"), article.get("url"))
    conn.commit()
    return report


def enrich_missing_details(conn, limit: int = 50) -> int:
    report = enrich_missing_detail_report(conn, limit=limit)
    return int(report.get("enriched") or 0)


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
    author = (
        _extract_author(soup, url)
        or clean_author_display(article.get("author"))
        or infer_reporter_from_article_text(
            article.get("source_name"),
            article.get("title"),
            article.get("summary"),
            body_text,
        )
    )
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
    _refresh_article_assessment(conn, article, body_text, author)
    return True


def _refresh_article_assessment(
    conn,
    article: dict,
    body_text: str | None,
    author: str | None,
) -> None:
    scored = Article(
        source_name=article["source_name"],
        source_type=article["source_type"],
        source_category=article.get("source_category") or "news",
        title=article["title"],
        url=article["url"],
        canonical_url=article.get("canonical_url"),
        author=author or article.get("author"),
        fingerprint=article["fingerprint"],
        summary=article.get("summary"),
        body_text=body_text or article.get("body_text"),
        keywords=_json_list(article.get("keywords")),
        region_tags=_json_list(article.get("region_tags")),
        verification_status=article.get("verification_status") or "unchecked",
    )
    scored = apply_newsroom_scoring(scored)
    scored = enrich_article_quality(scored)
    update_article_score(
        conn,
        article["id"],
        json.dumps(scored.keywords, ensure_ascii=False),
        scored.importance_score,
        scored.verification_status,
    )
    update_article_quality(
        conn,
        article["id"],
        json.dumps(scored.region_tags, ensure_ascii=False),
        scored.quality_score,
        json.dumps(scored.quality_flags, ensure_ascii=False),
        checklist_json(),
    )
    refreshed = get_article(conn, article["id"])
    if refreshed:
        record_alert_if_needed(conn, refreshed)


def _json_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _extract_body_text(soup: BeautifulSoup) -> str | None:
    candidates: list[tuple[float, str, str]] = []
    seen: set[str] = set()

    daum_body = _normalize_body_candidate(_extract_daum_body_text(soup))
    if daum_body and len(daum_body) >= BODY_MIN_LENGTH:
        return daum_body[:BODY_MAX_LENGTH]

    trusted_body = _normalize_body_candidate(_extract_trusted_body_text(soup))
    if trusted_body and len(trusted_body) >= BODY_MIN_LENGTH:
        return trusted_body[:BODY_MAX_LENGTH]

    for text in _extract_body_from_structured_data(soup):
        _add_body_candidate(candidates, seen, text, "jsonld", bonus=16)

    for selector in ARTICLE_SELECTORS:
        for node in soup.select(selector):
            _add_body_candidate(candidates, seen, _clean_body_node_text(node), selector, bonus=8)

    paragraphs = [normalize_space(p.get_text(" ")) for p in soup.select("p")]
    paragraph_text = normalize_space(" ".join(p for p in paragraphs if len(p) >= 20))
    _add_body_candidate(candidates, seen, paragraph_text, "paragraphs", bonus=4)

    for text in _extract_meta_descriptions(soup):
        _add_body_candidate(candidates, seen, text, "meta", bonus=-8)

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _add_body_candidate(
    candidates: list[tuple[float, str, str]],
    seen: set[str],
    text: str | None,
    source: str,
    *,
    bonus: float = 0,
) -> None:
    body = _normalize_body_candidate(text)
    if not body or len(body) < BODY_MIN_LENGTH:
        return
    key = body[:240]
    if key in seen:
        return
    seen.add(key)
    score = _body_candidate_score(body) + bonus
    if score <= 0:
        return
    candidates.append((score, body[:BODY_MAX_LENGTH], source))


def _clean_body_node_text(node) -> str | None:
    clone = BeautifulSoup(str(node), "html.parser")
    for selector in BODY_NOISE_SELECTORS:
        for noise in clone.select(selector):
            noise.decompose()
    return clean_html_text(str(clone), max_length=BODY_MAX_LENGTH)


def _extract_trusted_body_text(soup: BeautifulSoup) -> str | None:
    candidates: list[tuple[float, str, str]] = []
    seen: set[str] = set()
    for selector in TRUSTED_BODY_SELECTORS:
        for node in soup.select(selector):
            body = _normalize_body_candidate(_clean_body_node_text(node))
            if not body or len(body) < BODY_MIN_LENGTH:
                continue
            key = body[:240]
            if key in seen:
                continue
            seen.add(key)
            if _has_blocking_body_noise(body):
                continue
            score = _body_candidate_score(body) + 18
            candidates.append((score, body[:BODY_MAX_LENGTH], selector))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _extract_daum_body_text(soup: BeautifulSoup) -> str | None:
    container = (
        soup.select_one(".article_view section[dmcf-sid]")
        or soup.select_one("section[dmcf-sid]")
    )
    if not container:
        return None

    paragraphs = []
    seen = set()
    for node in container.select("[dmcf-pid], p"):
        text = normalize_space(node.get_text(" "))
        if not text or text in seen:
            continue
        seen.add(text)
        if _is_daum_body_noise(text):
            continue
        paragraphs.append(text)
    return normalize_space(" ".join(paragraphs))


def _is_daum_body_noise(text: str) -> bool:
    compact = normalize_space(text)
    if not compact:
        return True
    if compact.lower().startswith("copyright"):
        return True
    if len(compact) <= 90 and "@" in compact and re.search(r"[가-힣]{2,5}\s*(?:취재\s*)?기자", compact):
        return True
    if len(compact) <= 60 and re.fullmatch(r"[가-힣]{2,5}\s*(?:취재\s*)?기자\s*\|?.*", compact):
        return True
    return False


def _normalize_body_candidate(text: str | None) -> str | None:
    body = normalize_space(text or "")
    if not body:
        return None
    body = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", " ", body)
    body = re.sub(r"\s*(무단전재|재배포)\s*금지.*$", "", body)
    body = re.sub(r"\s*Copyright\s*(?:[Ⓒ©]|\(c\))?.*$", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\s*All rights reserved.*$", "", body, flags=re.IGNORECASE)
    return normalize_space(body)


def _has_blocking_body_noise(text: str) -> bool:
    if text.count("최근 24시간") >= 1 and text.count("댓글") >= 1:
        return True
    noise_hits = sum(text.count(word) for word in BODY_NOISE_WORDS)
    if noise_hits >= 4 and len(text) < 800:
        return True
    return False


def _body_candidate_score(text: str) -> float:
    korean_chars = len(re.findall(r"[가-힣]", text))
    sentence_count = len(re.findall(r"(?:다|요|죠|니다|습니다|했다|밝혔다|전했다|말했다)[.!?\"'”’)]?\s", text))
    punctuation_count = len(re.findall(r"[.!?。]", text))
    noise_hits = sum(text.count(word) for word in BODY_NOISE_WORDS)
    menu_density = len(re.findall(r"(로그인|회원가입|검색|메뉴|목록|공유|댓글|구독)", text[:800]))

    score = min(len(text), BODY_MAX_LENGTH) / 90
    score += min(sentence_count + punctuation_count, 24) * 1.8
    if korean_chars >= max(30, len(text) * 0.25):
        score += 8
    if len(text) >= 600:
        score += 8
    if len(text) < 140:
        score -= 6
    score -= noise_hits * 4
    score -= menu_density * 3
    return score


def _extract_meta_descriptions(soup: BeautifulSoup) -> list[str]:
    descriptions = []
    for selector in ["[property='og:description']", "[name='description']", "[name='twitter:description']"]:
        for node in soup.select(selector):
            value = node.get("content")
            if value:
                descriptions.append(value)
    return descriptions


def _extract_body_from_structured_data(soup: BeautifulSoup) -> list[str]:
    bodies: list[str] = []
    for node in soup.select("script[type='application/ld+json']"):
        raw = node.string or node.get_text(" ")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        bodies.extend(_body_from_jsonld(data))
    return bodies


def _body_from_jsonld(value) -> list[str]:
    bodies: list[str] = []
    if isinstance(value, list):
        for item in value:
            bodies.extend(_body_from_jsonld(item))
        return bodies
    if not isinstance(value, dict):
        return bodies

    for key in ("articleBody", "text", "description"):
        item = value.get(key)
        if isinstance(item, str):
            bodies.append(item)
        elif isinstance(item, dict) and isinstance(item.get("@value"), str):
            bodies.append(item["@value"])

    graph = value.get("@graph")
    if graph:
        bodies.extend(_body_from_jsonld(graph))
    return bodies


def _extract_author(soup: BeautifulSoup, url: str | None = None) -> str | None:
    host = urlparse(url or "").netloc.lower()
    block_selectors = list(AUTHOR_BLOCK_SELECTORS)
    if "knn.co.kr" in host:
        block_selectors = [".info"] + block_selectors + [".name"]

    candidates: list[tuple[float, str, str]] = []
    if "news.tvchosun.com" in host:
        author = _extract_tv_chosun_author(soup)
        if author:
            candidates.append((98, author, "tvchosun"))

    for selector in block_selectors:
        allow_name_only = selector in {".reporter_info .name", ".name", "[itemprop='author']"}
        for node in soup.select(selector):
            value = node.get("content") or node.get_text(" ")
            _add_author_candidate(
                candidates,
                value,
                selector,
                score=92 if "reporter" in selector or "writer" in selector else 82,
                allow_name_only=allow_name_only,
                allow_writer=True,
            )

    author = _extract_author_from_structured_data(soup)
    if author:
        candidates.append((88, author, "jsonld"))

    author = _extract_author_from_scripts(soup)
    if author:
        candidates.append((78, author, "script"))

    for selector in AUTHOR_META_SELECTORS:
        for node in soup.select(selector):
            _add_author_candidate(candidates, node.get("content"), selector, score=72, allow_name_only=True)

    for selector in AUTHOR_CONTEXT_SELECTORS:
        for node in soup.select(selector):
            _add_author_candidate(candidates, _author_context_text(node), selector, score=48, allow_writer=True)
    return _best_author_candidate(candidates)


def _extract_tv_chosun_author(soup: BeautifulSoup) -> str | None:
    candidates: list[tuple[float, str, str]] = []
    for selector in (
        "[property='dable:author']",
        "[name='dable:author']",
        ".view-title .editor .reporter",
        ".view-title .editor .name-box",
        ".editor .name-box .reporter",
        ".editor .reporter",
    ):
        for node in soup.select(selector):
            value = node.get("content") or node.get_text(" ")
            _add_author_candidate(candidates, value, selector, score=94, allow_name_only=True, allow_writer=True)

    for node in soup.select(".view-title .editor img[alt], .editor img[alt]"):
        _add_author_candidate(candidates, node.get("alt"), "tvchosun-img-alt", score=90, allow_name_only=True)

    for node in soup.select("script"):
        raw = node.string or node.get_text(" ")
        if not raw or "_author_info" not in raw:
            continue
        for match in re.finditer(r"""["']name["']\s*:\s*["']([^"']+)["']""", raw):
            _add_author_candidate(candidates, match.group(1), "tvchosun-author-info", score=96, allow_name_only=True)
    return _best_author_candidate(candidates)


def _add_author_candidate(
    candidates: list[tuple[float, str, str]],
    value: str | None,
    source: str,
    *,
    score: float,
    allow_name_only: bool = False,
    allow_writer: bool = False,
) -> None:
    author = _author_from_text(value, allow_name_only=allow_name_only, allow_writer=allow_writer)
    if author:
        candidates.append((score, author, source))


def _best_author_candidate(candidates: list[tuple[float, str, str]]) -> str | None:
    if not candidates:
        return None
    counts: dict[str, int] = {}
    for _, author, _ in candidates:
        counts[author] = counts.get(author, 0) + 1
    ranked = sorted(candidates, key=lambda item: (item[0] + counts[item[1]] * 3, item[1]), reverse=True)
    return ranked[0][1]


def _author_context_text(node) -> str:
    text = normalize_space(node.get_text(" "))
    if len(text) <= 2200:
        return text
    return normalize_space(f"{text[:1400]} {text[-700:]}")


def _author_from_text(
    value: str | None,
    *,
    allow_name_only: bool = False,
    allow_writer: bool = False,
) -> str | None:
    text = normalize_space(value or "")
    if not text:
        return None

    if len(text) > 260:
        return infer_reporter_from_article_text(body_text=text)

    normalized_value = re.sub(r"\s+", "", text).lower()
    if normalized_value in AUTHOR_SITE_VALUES:
        return None
    if text.lower() in AUTHOR_SITE_VALUES:
        return None

    if allow_name_only:
        author = clean_author_display(text)
        if author:
            return author

    for pattern in REPORTER_PATTERNS:
        for match in pattern.finditer(text):
            author = _format_author(match.group(1))
            if author:
                return author

    if allow_writer:
        for pattern in WRITER_PATTERNS:
            match = pattern.search(text)
            if match:
                author = _format_author(match.group(1))
                if author:
                    return author

    if allow_name_only and _is_plausible_name_only(text):
        return _format_author(text)
    return None


def _extract_author_from_structured_data(soup: BeautifulSoup) -> str | None:
    for node in soup.select("script[type='application/ld+json']"):
        raw = node.string or node.get_text(" ")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        author = _author_from_jsonld(data)
        if author:
            return author
    return None


def _author_from_jsonld(value) -> str | None:
    if isinstance(value, list):
        for item in value:
            author = _author_from_jsonld(item)
            if author:
                return author
        return None
    if not isinstance(value, dict):
        return None

    for key in (
        "reporterName",
        "reporter_name",
        "reporterNm",
        "reporter_nm",
        "authorName",
        "author_name",
        "writerName",
        "writer_name",
        "writer",
        "journalist",
        "byline",
    ):
        if key in value:
            author = clean_author_display(str(value.get(key) or ""))
            if author:
                return author

    for key in ("author", "creator", "editor", "contributor", "accountablePerson"):
        if key in value:
            author = _author_from_jsonld_author(value[key])
            if author:
                return author

    graph = value.get("@graph")
    if graph:
        author = _author_from_jsonld(graph)
        if author:
            return author
    return None


def _author_from_jsonld_author(value) -> str | None:
    if isinstance(value, list):
        for item in value:
            author = _author_from_jsonld_author(item)
            if author:
                return author
        return None
    if isinstance(value, dict):
        for key in ("name", "alternateName"):
            if key in value:
                author = clean_author_display(str(value.get(key) or ""))
                if author:
                    return author
        return _author_from_jsonld(value)
    if isinstance(value, str):
        return clean_author_display(value)
    return None


def _extract_author_from_scripts(soup: BeautifulSoup) -> str | None:
    for node in soup.select("script"):
        raw = node.string or node.get_text(" ")
        raw_lower = raw.lower() if raw else ""
        if not raw or not any(
            marker in raw_lower or marker in raw
            for marker in ("reporter", "author", "byline", "writer", "journalist", "기자", "작성자", "취재")
        ):
            continue
        for pattern in SCRIPT_AUTHOR_PATTERNS:
            match = pattern.search(raw)
            if not match:
                continue
            author = clean_author_display(match.group(1))
            if author:
                return author
    return None


def _format_author(name: str) -> str | None:
    cleaned = normalize_space(name)
    cleaned = re.sub(r"기자의\s*기사\s*더보기", "", cleaned)
    cleaned = re.sub(r"[()\[\]{}<>]", " ", cleaned)
    cleaned = normalize_space(cleaned)
    if not re.fullmatch(r"[가-힣]{2,5}", cleaned):
        return None
    if cleaned in AUTHOR_NOISE_NAMES:
        return None
    if any(fragment in cleaned for fragment in AUTHOR_NOISE_FRAGMENTS):
        return None
    return f"{cleaned} 기자"


def _is_plausible_name_only(text: str) -> bool:
    cleaned = normalize_space(re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text))
    cleaned = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", " ", cleaned)
    cleaned = normalize_space(cleaned)
    if any(fragment in cleaned for fragment in AUTHOR_NOISE_FRAGMENTS):
        return False
    if re.search(r"(입력|수정|작성일|조회수|뉴스|방송|제보|기사)", cleaned):
        return False
    return re.fullmatch(r"[가-힣]{2,5}", cleaned) is not None and cleaned not in AUTHOR_NOISE_NAMES


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
