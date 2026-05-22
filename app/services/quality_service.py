import json
import re
from urllib.parse import urlparse

from app.models import Article
from app.services.region_service import extract_regions


NAVIGATION_TITLE_EXACT = {
    "로그인",
    "회원가입",
    "사이트맵",
    "검색",
    "목록",
    "이전",
    "다음",
    "홈페이지",
    "누리집",
    "푸터",
    "이메일",
    "개인정보처리방침",
    "저작권보호정책",
    "누리집 안내지도",
    "업무추진비 공개",
    "대메뉴 바로가기",
    "본문 내용 바로가기",
}
NAVIGATION_TITLE_FRAGMENTS = {
    "본문 바로가기",
    "본문 내용 바로가기",
    "푸터 내용 바로가기",
    "메뉴 바로가기",
    "페이지로 이동",
    "개인정보처리방침",
    "사전정보공표",
    "세입세출예산",
    "온라인 민원",
    "누리집 안내지도",
}

CHECKLIST_KEYS = {
    "source_checked": False,
    "cross_checked": False,
    "agency_checked": False,
    "media_checked": False,
    "broadcast_ready": False,
}


def enrich_article_quality(article: Article) -> Article:
    if not article.region_tags:
        article.region_tags = extract_regions(article.title, article.summary, article.body_text)
    score, flags = quality_score(article)
    article.quality_score = score
    article.quality_flags = flags
    article.verification_checklist = dict(CHECKLIST_KEYS)
    return article


def quality_score(article: Article) -> tuple[float, list[str]]:
    score = 100.0
    flags: list[str] = []
    title = article.title.strip()
    parsed = urlparse(article.url)

    if len(title) < 10:
        score -= 25
        flags.append("short_title")
    if is_navigation_like_title(title):
        score -= 35
        flags.append("navigation_like_title")
    if len(title) > 180:
        score -= 20
        flags.append("long_mixed_title")
    if not parsed.scheme.startswith("http") or not parsed.netloc:
        score -= 30
        flags.append("invalid_url")
    if article.source_type == "html" and not article.summary and not article.body_text:
        score -= 10
        flags.append("no_body_yet")
    if not article.region_tags and article.source_category in {"disaster", "fire", "police", "weather"}:
        score -= 8
        flags.append("no_region")

    return max(0, round(score, 1)), flags


def is_navigation_like_title(title: str) -> bool:
    text = (title or "").strip()
    compact = re.sub(r"\s+", "", text)
    exact_compact = {re.sub(r"\s+", "", value) for value in NAVIGATION_TITLE_EXACT}
    if text in NAVIGATION_TITLE_EXACT or compact in exact_compact:
        return True
    return any(fragment in text for fragment in NAVIGATION_TITLE_FRAGMENTS)


def checklist_json() -> str:
    return json.dumps(CHECKLIST_KEYS, ensure_ascii=False)
