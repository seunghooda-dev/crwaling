import json
from urllib.parse import urlparse

from app.models import Article


REGIONS = [
    "서울", "경기", "인천", "부산", "대구", "광주", "대전", "울산", "세종",
    "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
    "수원", "성남", "고양", "용인", "창원", "청주", "천안", "전주", "포항",
]

BAD_TITLE_WORDS = {
    "로그인", "회원가입", "사이트맵", "개인정보", "저작권", "이메일", "바로가기",
    "메뉴", "검색", "목록", "이전", "다음", "홈페이지", "누리집",
}

CHECKLIST_KEYS = {
    "source_checked": False,
    "cross_checked": False,
    "agency_checked": False,
    "media_checked": False,
    "broadcast_ready": False,
}


def enrich_article_quality(article: Article) -> Article:
    article.region_tags = extract_regions(article.title, article.summary, article.body_text)
    score, flags = quality_score(article)
    article.quality_score = score
    article.quality_flags = flags
    article.verification_checklist = dict(CHECKLIST_KEYS)
    return article


def extract_regions(*values: str | None) -> list[str]:
    text = " ".join(value or "" for value in values)
    return [region for region in REGIONS if region in text]


def quality_score(article: Article) -> tuple[float, list[str]]:
    score = 100.0
    flags: list[str] = []
    title = article.title.strip()
    parsed = urlparse(article.url)

    if len(title) < 10:
        score -= 25
        flags.append("short_title")
    if any(word in title for word in BAD_TITLE_WORDS):
        score -= 35
        flags.append("navigation_like_title")
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


def checklist_json() -> str:
    return json.dumps(CHECKLIST_KEYS, ensure_ascii=False)
