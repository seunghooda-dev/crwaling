import json


def build_ai_assist(article: dict) -> dict[str, str]:
    """Create a deterministic newsroom-assist note without calling an external AI API."""
    title = article.get("title") or ""
    source = article.get("source_name") or "unknown source"
    summary = article.get("body_text") or article.get("summary") or ""
    keywords = _load_keywords(article.get("keywords"))

    short_summary = summary.strip()
    if len(short_summary) > 220:
        short_summary = short_summary[:217].rstrip() + "..."
    if not short_summary:
        short_summary = "RSS 단계에서 상세 요약이 제공되지 않았습니다. 원문 확인이 필요합니다."

    keyword_text = ", ".join(keywords[:8]) if keywords else "감지 키워드 없음"
    anchor_line = _anchor_line(title, keywords)
    report_plan = _report_plan(keywords, article)
    caution = _caution_line(keywords)

    ai_summary = (
        f"핵심: {title}\n"
        f"출처: {source}\n"
        f"요약: {short_summary}\n"
        f"키워드: {keyword_text}\n\n"
        f"앵커 멘트 초안: {anchor_line}\n\n"
        f"리포트 구성안:\n{report_plan}\n\n"
        f"민감 표현/검증 주의: {caution}"
    )

    check_points = "\n".join(
        [
            "1. 원문 링크와 발행 시각을 확인합니다.",
            "2. 인명 피해, 수치, 기관 발표는 2개 이상 출처로 교차 확인합니다.",
            "3. 영상/CCTV/SNS 자료가 있으면 촬영 시각, 위치, 원게시자를 확인합니다.",
            "4. 방송 사용 전 저작권과 이용 조건을 확인합니다.",
        ]
    )
    return {"ai_summary": ai_summary, "check_points": check_points}


def _anchor_line(title: str, keywords: list[str]) -> str:
    if {"사망", "실종", "화재", "폭발"} & set(keywords):
        return f"{title}. 피해 규모와 현장 상황을 중심으로 추가 확인이 필요합니다."
    if {"가짜뉴스", "허위", "조작"} & set(keywords):
        return f"{title}. 사실관계와 공식 해명을 함께 짚어보겠습니다."
    return f"{title}. 관련 내용과 후속 파장을 정리합니다."


def _report_plan(keywords: list[str], article: dict) -> str:
    visual_hint = "현장 영상/CCTV/브리핑 화면 확인" if {"영상", "CCTV", "현장", "브리핑"} & set(keywords) else "원문 화면과 자료화면 중심 구성"
    return "\n".join(
        [
            f"1. 사건/발표의 핵심 사실: {article.get('title')}",
            "2. 현재까지 확인된 수치, 시간, 장소 정리",
            f"3. 화면 요소: {visual_hint}",
            "4. 관계기관 추가 확인 또는 반론 여부 점검",
        ]
    )


def _caution_line(keywords: list[str]) -> str:
    cautions = []
    if {"SNS", "가짜뉴스", "허위", "조작", "논란"} & set(keywords):
        cautions.append("온라인 출처와 원게시자 확인 필요")
    if {"사망", "실종", "체포", "구속"} & set(keywords):
        cautions.append("피의사실 공표, 개인정보, 피해자 보호 표현 주의")
    if not cautions:
        cautions.append("원문과 공식 발표 간 표현 차이 확인")
    return "; ".join(cautions)


def _load_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in payload if item]
