import re

from app.text import normalize_space


SITE_AUTHOR_VALUES = {
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
    "sbs",
    "mbc",
    "kbs",
    "ytn",
    "연합뉴스",
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
    "아는",
    "하는",
    "했던",
    "진을",
    "진과",
    "로벌",
    "카메라",
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
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s*\(\s*([가-힣]{2,5})\s*기자\s*\)"),
    re.compile(r"([가-힣]{2,5})\s*\([^)]*@[^)]*\)\s*기자(?![가-힣])"),
    re.compile(r"([가-힣]{2,5})\s*\[[^\]]*@[^\]]*\]\s*기자(?![가-힣])"),
    re.compile(r"(?:기자명|담당기자|취재기자|작성자|취재|글)\s*[:：]?\s*([가-힣]{2,5})(?:\s*기자)?(?![가-힣])"),
    re.compile(r"([가-힣]{2,5})\s*기자(?![가-힣])"),
    re.compile(r"([가-힣]{2,5})\s+[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
]
STRICT_BYLINE_PATTERNS = [
    re.compile(r"([가-힣]{2,5})\s*(?:기자)?\s+[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE),
    re.compile(r"([가-힣]{2,5})\s*기자\s*[<(]?\s*[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE),
    re.compile(r"(?:작성자|담당기자|취재기자|기자명)\s*[:：]?\s*([가-힣]{2,5})(?:\s*기자)?\s*(?:작성일|입력|수정|조회수|$)"),
    re.compile(r"\[[^\]]{1,36}\s+([가-힣]{2,5})\s*기자\]"),
    re.compile(r"\([^)]+=\s*연합뉴스\)\s*(?:[가-힣]{2,5}\s+)*([가-힣]{2,5})\s*기자\s*="),
    re.compile(r"(?:^|[\s.?!])([가-힣]{2,5})\s*기자(?:입니다|가\s+(?:전해|보도|취재|소개)|의\s+보도|의\s+취재|가\s+취재했습니다)"),
    re.compile(r"(?:^|[.?!]\s+)([가-힣]{2,5})\s*기자\s*$"),
    re.compile(r"(?:JTV|JTBC|YTN|KBS|MBC|SBS|TJB|KNN|KBC|TBC|G1|UBC|CJB|JIBS|전주방송|동아닷컴|스포츠동아)\s+[^\n]{0,24}?([가-힣]{2,5})\s*기자(?:\s|$|\d)"),
]


def clean_author_display(value: str | None) -> str | None:
    text = normalize_space(value or "")
    if not text:
        return None
    if _is_invalid_author_value(text):
        return None

    reporter = extract_reporter_name(text)
    if reporter:
        return reporter

    cleaned = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", " ", text)
    cleaned = re.sub(r"^(by|기자)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = normalize_space(cleaned.strip("()[]{}<>|/-· "))
    if not cleaned or _is_invalid_author_value(cleaned) or _is_site_or_noise(cleaned):
        return None
    if re.fullmatch(r"[가-힣]{2,5}", cleaned) and cleaned not in AUTHOR_NOISE_NAMES:
        return f"{cleaned} 기자"
    if len(cleaned) <= 60 and not any(fragment in cleaned for fragment in AUTHOR_NOISE_FRAGMENTS):
        return cleaned
    return None


def extract_reporter_name(value: str | None) -> str | None:
    text = normalize_space(value or "")
    for pattern in REPORTER_PATTERNS:
        for match in pattern.finditer(text):
            author = _format_reporter(match.group(1))
            if author:
                return author
    return None


def infer_reporter_from_article_text(
    source_name: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    body_text: str | None = None,
) -> str | None:
    source = normalize_space(source_name or "")
    title_text = normalize_space(title or "")
    content_text = normalize_space(" ".join(value for value in (summary or "", body_text or "") if value))
    candidates = []
    for text, base_score in ((content_text, 80), (normalize_space(f"{title_text} {content_text}"), 64)):
        if not text:
            continue
        for pattern in STRICT_BYLINE_PATTERNS:
            for match in pattern.finditer(text):
                author = _format_reporter(match.group(1))
                if not author:
                    continue
                score = base_score
                context = text[max(0, match.start() - 24) : match.end() + 36]
                if "@" in context:
                    score += 24
                if any(marker in context for marker in ("기자입니다", "기자의 보도", "취재했습니다", "동아닷컴", "스포츠동아", "연합뉴스")):
                    score += 12
                if source and source.split()[0] in context:
                    score += 6
                candidates.append((score, author))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _format_reporter(name: str) -> str | None:
    cleaned = normalize_space(name)
    cleaned = re.sub(r"[()\[\]{}<>]", " ", cleaned)
    cleaned = normalize_space(cleaned)
    if not re.fullmatch(r"[가-힣]{2,5}", cleaned):
        return None
    if cleaned in AUTHOR_NOISE_NAMES:
        return None
    if any(fragment in cleaned for fragment in AUTHOR_NOISE_FRAGMENTS):
        return None
    return f"{cleaned} 기자"


def _is_site_or_noise(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).lower()
    lowered = value.lower()
    base = normalize_space(re.sub(r"\s*기자$", "", value))
    return compact in SITE_AUTHOR_VALUES or lowered in SITE_AUTHOR_VALUES or base in AUTHOR_NOISE_NAMES


def _is_invalid_author_value(value: str) -> bool:
    compact = re.sub(r"\s+", "", normalize_space(value)).lower()
    if not compact:
        return True
    if compact.startswith("@"):
        return True
    if compact in SITE_AUTHOR_VALUES:
        return True
    if re.fullmatch(r"\d{2,}", compact):
        return True
    if re.fullmatch(r"[0-9._-]+", compact):
        return True
    if re.fullmatch(r"[a-z0-9._-]+\.[a-z]{2,}", compact):
        return True
    if compact.startswith(("http://", "https://", "www.")):
        return True
    return False
