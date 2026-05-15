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


def apply_newsroom_scoring(article: Article) -> Article:
    title = article.title.lower()
    context = f"{article.summary or ''} {article.body_text or ''}".lower()
    matched: list[str] = []
    score = 0.0

    for group_name, keywords in load_keyword_groups().items():
        for keyword in keywords:
            needle = keyword.lower()
            if needle in title:
                matched.append(keyword)
                if group_name == "breaking":
                    score += 3.5
                elif group_name == "broadcast":
                    score += 1.5
                else:
                    score += 1.5
            elif keyword not in TITLE_ONLY_WORDS and needle in context:
                matched.append(keyword)
                score += 0.25 if keyword in LOW_SIGNAL_CONTEXT_WORDS else 0.5

    article.keywords = sorted(set(article.keywords + matched))
    weighted_score = round(score * SOURCE_WEIGHTS.get(article.source_name, 0.7), 2)
    article.importance_score = weighted_score
    if weighted_score >= 5 and article.verification_status == "unchecked":
        article.verification_status = "needs_review"
    elif weighted_score < 5 and article.verification_status == "needs_review":
        article.verification_status = "unchecked"
    return article
