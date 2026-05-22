import json
import re

from app.text import normalize_space


FIRST_PARTY_CATEGORIES = {"disaster", "fire", "police", "weather", "health", "government"}
FIRST_PARTY_SOURCE_TOKENS = (
    "safe korea",
    "national fire",
    "police",
    "kma",
    "kdca",
    "mois",
    "korea policy",
)
AUTHOR_REQUIRED_CATEGORIES = {"news"}
REGION_REQUIRED_CATEGORIES = {"disaster", "fire", "police", "weather", "health"}
QUALITY_FLAG_PENALTIES = {
    "navigation_like_title": 35,
    "long_mixed_title": 16,
    "short_title": 12,
    "invalid_url": 28,
    "no_body_yet": 10,
    "no_region": 8,
}


def article_reliability(article: dict) -> dict:
    """Return a compact newsroom reliability signal for display and sorting hints."""
    quality = float(article.get("quality_score") or 0)
    source_category = str(article.get("source_category") or "")
    source_type = str(article.get("source_type") or "")
    verification_status = str(article.get("verification_status") or "unchecked")
    newsroom_status = str(article.get("newsroom_status") or "new")
    body = normalize_space(article.get("body_text") or article.get("summary") or "")
    author = normalize_space(article.get("author") or "")
    flags = _json_list(article.get("quality_flags"))
    regions = _json_list(article.get("region_tags"))
    images = _json_list(article.get("image_urls"))
    videos = _json_list(article.get("video_urls"))
    source_count = int(article.get("cluster_source_count") or 0)
    article_count = int(article.get("cluster_article_count") or 0)
    first_party = _is_first_party(article)
    author_required = source_category in AUTHOR_REQUIRED_CATEGORIES and source_type != "api"
    region_required = source_category in REGION_REQUIRED_CATEGORIES

    score = 38.0 + min(max(quality, 0), 100) * 0.24
    reasons: list[str] = []
    missing: list[str] = []

    if first_party:
        score += 12
        reasons.append("기관/공공 출처")
    if source_count >= 2:
        score += min(14, 8 + source_count)
        reasons.append(f"복수 출처 {source_count}곳")
    elif float(article.get("importance_score") or 0) >= 4.5:
        score -= 7
        missing.append("복수 출처")

    if len(body) >= 400:
        score += 10
        reasons.append("본문 충분")
    elif len(body) >= 120:
        score += 6
        reasons.append("본문 확보")
    else:
        score -= 14
        missing.append("본문")

    if author:
        score += 7
        reasons.append("기자명 확인")
    elif author_required:
        score -= 10
        missing.append("기자명")

    if regions:
        score += 4
        reasons.append("지역 확인")
    elif region_required:
        score -= 7
        missing.append("지역")

    if images or videos:
        score += 4
        reasons.append("영상/사진 자료")

    if verification_status == "verified":
        score += 10
        reasons.append("검증 완료")
    elif verification_status == "checking":
        score += 3
        reasons.append("확인 진행 중")
    elif verification_status == "needs_review":
        score -= 5
        missing.append("검증")
    elif verification_status == "caution":
        score -= 16
        missing.append("주의 검토")

    if newsroom_status == "hold":
        score -= 20
        missing.append("사용주의")
    elif newsroom_status == "ready":
        score += 4
        reasons.append("방송 후보")

    for flag in flags:
        penalty = QUALITY_FLAG_PENALTIES.get(str(flag), 4)
        score -= penalty
        if str(flag) not in missing:
            missing.append(_flag_label(str(flag)))

    score = round(max(0, min(100, score)))
    level, label = _reliability_level(score)
    return {
        "score": score,
        "level": level,
        "label": label,
        "reasons": _dedupe(reasons)[:4],
        "missing": _dedupe(missing)[:5],
        "source_count": source_count,
        "article_count": article_count,
        "first_party": first_party,
    }


def _reliability_level(score: float) -> tuple[str, str]:
    if score >= 80:
        return "high", "방송 안정"
    if score >= 65:
        return "medium", "확인 가능"
    if score >= 45:
        return "warning", "추가 확인"
    return "danger", "사용 주의"


def _is_first_party(article: dict) -> bool:
    category = str(article.get("source_category") or "")
    if category in FIRST_PARTY_CATEGORIES:
        return True
    source = str(article.get("source_name") or "").lower()
    return any(token in source for token in FIRST_PARTY_SOURCE_TOKENS)


def _json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [normalize_space(part) for part in re.split(r"[,|]", text) if normalize_space(part)]
    return data if isinstance(data, list) else []


def _flag_label(flag: str) -> str:
    return {
        "navigation_like_title": "메뉴성 항목",
        "long_mixed_title": "제목/본문 혼합",
        "short_title": "짧은 제목",
        "invalid_url": "주소 오류",
        "no_body_yet": "본문 부족",
        "no_region": "지역 미확인",
    }.get(flag, flag)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
