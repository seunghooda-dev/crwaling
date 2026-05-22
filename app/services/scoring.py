from app.keyword_loader import load_keyword_groups
from app.models import Article


SOURCE_WEIGHTS = {
    "SBS News Latest": 1.0,
    "SBS News Issues": 1.0,
    "Yonhap English RSS": 0.9,
    "KBS World Today News": 0.8,
    "Korea Policy Press Releases": 0.8,
    "Korea Policy Fact Check": 0.9,
    "Korea Policy E-Briefing": 0.65,
    "Korea Policy News": 0.75,
    "Korea Policy Photo": 0.5,
    "Korea Policy Video": 0.55,
    "Safe Korea Disaster Messages": 1.2,
}

LOW_SIGNAL_CONTEXT_WORDS = {"영상", "브리핑", "발표", "현장", "확인", "온라인"}
TITLE_ONLY_WORDS = {"단독", "속보", "긴급", "사망", "실종", "체포", "구속", "압수수색", "미사일", "지진", "화재", "폭발"}
KEYWORD_WEIGHTS = {
    "속보": 2.4,
    "긴급": 2.6,
    "단독": 1.2,
    "재난": 2.0,
    "대피": 2.4,
    "대피령": 2.8,
    "통제": 1.2,
    "교통통제": 2.0,
    "지진": 2.6,
    "화재": 3.3,
    "산불": 3.0,
    "침수": 2.4,
    "붕괴": 3.0,
    "구조": 1.4,
    "심정지": 3.2,
    "화학물질": 2.8,
    "유해물질": 2.8,
    "교통사고": 2.6,
    "폭발": 3.2,
    "사망": 3.2,
    "실종": 2.8,
    "수색": 1.4,
    "감염병": 2.0,
    "집단감염": 2.8,
    "기상특보": 2.2,
    "호우": 2.0,
    "대설": 2.0,
    "태풍": 2.6,
    "폭염": 1.8,
    "한파": 1.8,
    "체포": 1.2,
    "구속": 1.4,
    "압수수색": 1.6,
    "북한": 0.8,
    "미사일": 2.2,
    "대통령": 0.7,
    "영상": 0.5,
    "CCTV": 1.0,
    "블랙박스": 1.0,
    "목격": 0.8,
    "제보": 0.7,
    "현장": 0.5,
    "브리핑": 0.4,
    "발표": 0.3,
    "기자회견": 0.4,
    "SNS": 0.4,
    "온라인": 0.2,
    "가짜뉴스": 1.0,
    "허위": 1.0,
    "조작": 1.0,
    "논란": 0.6,
    "확인": 0.3,
}
FALSE_OR_DRILL_TERMS = (
    "오발송",
    "오발령",
    "훈련상황",
    "훈련 상황",
    "실제상황이 아니",
    "실제 상황이 아니",
    "훈련 메시지",
)
ENDED_OR_REDUCED_TERMS = (
    "상황 종료",
    "수색 종료",
    "진화완료",
    "진화 완료",
    "통제 해제",
    "주의보 해제",
    "경보 해제",
)


def apply_newsroom_scoring(article: Article) -> Article:
    title = article.title.lower()
    context = f"{article.summary or ''} {article.body_text or ''}".lower()
    full_text = f"{article.title} {article.summary or ''} {article.body_text or ''}"
    matched: list[str] = []
    score = 0.0

    for group_name, keywords in load_keyword_groups().items():
        for keyword in keywords:
            needle = keyword.lower()
            if needle in title:
                matched.append(keyword)
                score += KEYWORD_WEIGHTS.get(keyword, 1.5 if group_name == "breaking" else 0.7)
            elif keyword not in TITLE_ONLY_WORDS and needle in context:
                matched.append(keyword)
                score += min(KEYWORD_WEIGHTS.get(keyword, 0.5) * 0.35, 1.2)
                if keyword in LOW_SIGNAL_CONTEXT_WORDS:
                    score -= 0.15

    if _contains_any(full_text, FALSE_OR_DRILL_TERMS):
        matched.append("오발송/훈련")
        score -= 8.0
    elif _contains_any(full_text, ENDED_OR_REDUCED_TERMS):
        matched.append("상황종료")
        score -= 2.0

    article.keywords = sorted(set(article.keywords + matched))
    weighted_score = max(0.0, round(score * SOURCE_WEIGHTS.get(article.source_name, 0.7), 2))
    article.importance_score = weighted_score
    if _contains_any(full_text, FALSE_OR_DRILL_TERMS):
        article.verification_status = "caution"
    elif weighted_score >= 5 and article.verification_status == "unchecked":
        article.verification_status = "needs_review"
    elif weighted_score < 5 and article.verification_status == "needs_review":
        article.verification_status = "unchecked"
    return article


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)
