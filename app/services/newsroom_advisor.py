import json
import re
import sqlite3
from collections import Counter


EMERGENCY_TERMS = {
    "사망": 3.0,
    "심정지": 2.8,
    "실종": 2.5,
    "화재": 2.2,
    "폭발": 2.2,
    "침수": 2.0,
    "산불": 2.0,
    "교통사고": 1.8,
    "압수수색": 1.6,
    "구속": 1.4,
    "체포": 1.4,
    "기상특보": 1.6,
    "대피": 1.8,
    "단독": 1.2,
    "속보": 2.0,
    "긴급": 2.0,
}

FIRST_PARTY_CATEGORIES = {"disaster", "fire", "police", "weather", "health", "government"}
FIRST_PARTY_SOURCES = (
    "Safe Korea",
    "National Fire",
    "Police",
    "KMA",
    "KDCA",
    "MOIS",
    "Korea Policy",
)

MISSION_CATEGORIES = ["disaster", "fire", "police", "weather", "health", "government", "news"]
MISSION_REGIONS = ["capital", "gangwon", "chungcheong", "honam", "yeongnam", "jeju"]
REGION_ALIASES = {
    "capital": ["서울", "경기", "인천", "수도권"],
    "gangwon": ["강원"],
    "chungcheong": ["충북", "충남", "대전", "세종", "충청"],
    "honam": ["광주", "전북", "전남", "호남"],
    "yeongnam": ["부산", "대구", "울산", "경북", "경남", "영남"],
    "jeju": ["제주"],
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


def reporter_advice_for_article(article: dict) -> dict:
    text = _article_text(article)
    keywords = _json_list(article.get("keywords"))
    regions = _json_list(article.get("region_tags"))
    quality_flags = _json_list(article.get("quality_flags"))
    reasons: list[str] = []
    score_details: list[dict] = []
    score = float(article.get("importance_score") or 0)
    if score:
        score_details.append({"label": "기존 중요도", "value": round(score, 1)})

    for term, weight in EMERGENCY_TERMS.items():
        if term in text:
            score += weight
            reasons.append(term)
            score_details.append({"label": term, "value": weight})
    if article.get("source_category") in FIRST_PARTY_CATEGORIES:
        score += 1.5
        reasons.append("기관/공공 출처")
        score_details.append({"label": "기관/공공 출처", "value": 1.5})
    if _is_first_party_source(article.get("source_name", "")):
        score += 1.0
        reasons.append("1차 확인 가능")
        score_details.append({"label": "1차 확인 가능", "value": 1.0})
    if regions:
        score += 0.7
        reasons.append("지역 특정")
        score_details.append({"label": "지역 특정", "value": 0.7})
    if int(article.get("cluster_source_count") or 0) >= 2:
        score += 0.8
        reasons.append("복수 출처")
        score_details.append({"label": "복수 출처", "value": 0.8})
    if "no_region" in quality_flags:
        score -= 0.5
        reasons.append("지역 미확인")
        score_details.append({"label": "지역 미확인", "value": -0.5})
    false_signal = _contains_any(text, FALSE_OR_DRILL_TERMS)
    ended_signal = _contains_any(text, ENDED_OR_REDUCED_TERMS)
    if false_signal:
        score -= 8.0
        reasons.append("오발송/훈련 신호")
        score_details.append({"label": "오발송/훈련 신호", "value": -8.0})
    elif ended_signal:
        score -= 2.0
        reasons.append("상황 종료 신호")
        score_details.append({"label": "상황 종료 신호", "value": -2.0})

    decision = "참고"
    if false_signal:
        decision = "보류"
    elif score >= 9:
        decision = "즉시 취재"
    elif score >= 6:
        decision = "전화 확인"
    elif score >= 3:
        decision = "모니터링"
    if ended_signal and decision in {"즉시 취재", "전화 확인"}:
        decision = "모니터링"

    checklist = {
        "원출처 확인": _is_first_party_source(article.get("source_name", "")) or article.get("source_category") in FIRST_PARTY_CATEGORIES,
        "복수 출처 확인": int(article.get("cluster_source_count") or 0) >= 2,
        "시간 확인": bool(article.get("published_at") or article.get("collected_at")),
        "장소 확인": bool(regions),
        "피해 규모 확인": bool(re.search(r"\d+\s*(명|곳|대|건)", text)),
        "영상/사진 확인": bool(_json_list(article.get("image_urls")) or _json_list(article.get("video_urls"))),
    }
    verification_score = sum(1 for ok in checklist.values() if ok)
    verification_risk = "낮음"
    if verification_score <= 2:
        verification_risk = "높음"
    elif verification_score <= 4:
        verification_risk = "중간"
    if false_signal:
        verification_risk = "높음"
    return {
        "article_id": article.get("id"),
        "decision": decision,
        "reporter_score": round(score, 1),
        "score_details": score_details,
        "reasons": sorted(set(reasons or keywords[:3])),
        "regions": regions,
        "checklist": checklist,
        "verification_score": verification_score,
        "verification_total": len(checklist),
        "verification_risk": verification_risk,
        "truth_flags": _truth_flags(false_signal, ended_signal),
        "cuesheet": build_cuesheet_item(article, decision, checklist),
    }


def build_cuesheet_item(article: dict, decision: str, checklist: dict[str, bool]) -> dict:
    title = str(article.get("title") or "").strip()
    summary = str(article.get("summary") or article.get("body_text") or "").strip()
    source = str(article.get("source_name") or "").strip()
    regions = ", ".join(_json_list(article.get("region_tags"))) or "지역 확인 필요"
    missing = [key for key, ok in checklist.items() if not ok]
    anchor = title
    if summary:
        anchor = f"{title}. {summary[:90]}"
    return {
        "headline": title[:90],
        "anchor_line": anchor[:160],
        "assignment_hint": decision,
        "contact_hint": _contact_hint(article),
        "verification_gap": ", ".join(missing[:4]) if missing else "핵심 확인 항목 충족",
        "region": regions,
        "source": source,
    }


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _truth_flags(false_signal: bool, ended_signal: bool) -> list[str]:
    flags = []
    if false_signal:
        flags.append("오발송/훈련 가능")
    if ended_signal:
        flags.append("상황 종료/해제")
    return flags


def build_reporter_dashboard(conn: sqlite3.Connection, limit: int = 12) -> dict:
    rows = conn.execute(
        """
        SELECT
            a.*,
            (
                SELECT COUNT(DISTINCT peer.source_name)
                FROM articles peer
                WHERE peer.duplicate_group_id = a.duplicate_group_id
            ) AS cluster_source_count,
            (
                SELECT COUNT(1)
                FROM articles peer
                WHERE peer.duplicate_group_id = a.duplicate_group_id
            ) AS cluster_article_count
        FROM articles a
        ORDER BY importance_score DESC, collected_at DESC
        LIMIT ?
        """,
        (limit * 4,),
    ).fetchall()
    advised = [dict(row) | {"advice": reporter_advice_for_article(dict(row))} for row in rows]
    advised.sort(key=lambda item: item["advice"]["reporter_score"], reverse=True)
    top = advised[:limit]
    return {
        "top": [
            {
                "id": item["id"],
                "title": item["title"],
                "source_name": item["source_name"],
                "source_category": item["source_category"],
                "published_at": item["published_at"],
                "collected_at": item["collected_at"],
                "url": item["url"],
                **item["advice"],
            }
            for item in top
        ],
        "beat_modes": _beat_modes(conn),
        "source_health": _source_speed_summary(conn),
        "alert_rules": suggested_alert_rules(conn),
        "today_budget": today_budget(conn),
        "ready_queue": suggested_ready_queue(conn, limit=limit),
        "risk_dashboard": false_risk_dashboard(conn, limit=6),
    }


def false_risk_dashboard(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    clusters = conn.execute(
        """
        SELECT duplicate_group_id
        FROM articles
        WHERE duplicate_group_id IS NOT NULL
        GROUP BY duplicate_group_id
        HAVING COUNT(1) > 1
        ORDER BY MAX(importance_score) DESC, MAX(collected_at) DESC
        LIMIT ?
        """,
        (limit * 4,),
    ).fetchall()
    risks = []
    for row in clusters:
        item = compare_cluster_for_reporter(conn, row["duplicate_group_id"])
        if item["conflict_count"]:
            sample = item["timeline"][-1] if item["timeline"] else {}
            risks.append(
                {
                    "duplicate_group_id": item["duplicate_group_id"],
                    "title": sample.get("title") or "이슈 묶음",
                    "source_count": len(item["sources"]),
                    "conflict_count": item["conflict_count"],
                    "conflict_summary": item["conflict_summary"],
                }
            )
    return risks[:limit]


def build_command_center(conn: sqlite3.Connection) -> dict:
    source_health = _source_health_for_command(conn)
    freshness = _freshness_score(source_health)
    success = _success_score(source_health)
    coverage = _coverage_matrix(conn)
    blind_spots = _blind_spots(coverage, source_health)
    anomaly_signals = _anomaly_signals(conn)
    verification = _verification_pressure(conn)
    operational_score = round(
        max(0, min(100, freshness * 0.35 + success * 0.25 + coverage["coverage_score"] * 0.2 + verification["score"] * 0.2)),
        1,
    )
    return {
        "operational_score": operational_score,
        "grade": _grade(operational_score),
        "sla": {
            "freshness_score": round(freshness, 1),
            "success_score": round(success, 1),
            "coverage_score": coverage["coverage_score"],
            "verification_score": verification["score"],
        },
        "blind_spots": blind_spots,
        "anomaly_signals": anomaly_signals,
        "coverage_matrix": coverage["matrix"],
        "playbooks": _watch_playbooks(conn),
        "source_recommendations": _source_recommendations(source_health),
        "verification_pressure": verification,
    }


def suggested_ready_queue(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            a.*,
            (
                SELECT COUNT(DISTINCT peer.source_name)
                FROM articles peer
                WHERE peer.duplicate_group_id = a.duplicate_group_id
            ) AS cluster_source_count,
            (
                SELECT COUNT(1)
                FROM articles peer
                WHERE peer.duplicate_group_id = a.duplicate_group_id
            ) AS cluster_article_count
        FROM articles a
        WHERE newsroom_status != 'ready'
        ORDER BY importance_score DESC, collected_at DESC
        LIMIT ?
        """,
        (limit * 3,),
    ).fetchall()
    candidates = []
    for row in rows:
        article = dict(row)
        advice = reporter_advice_for_article(article)
        if advice["decision"] in {"즉시 취재", "전화 확인"} and advice["verification_score"] >= 3:
            candidates.append(
                {
                    "id": article["id"],
                    "title": article["title"],
                    "source_name": article["source_name"],
                    "source_category": article["source_category"],
                    "url": article["url"],
                    **advice,
                }
            )
    candidates.sort(key=lambda item: (item["reporter_score"], item["verification_score"]), reverse=True)
    return candidates[:limit]


def _source_health_for_command(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            s.name,
            s.source_category,
            s.enabled,
            s.crawl_interval_seconds,
            (
                SELECT cr.status
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC
                LIMIT 1
            ) AS last_status,
            (
                SELECT cr.started_at
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC
                LIMIT 1
            ) AS last_started_at,
            (
                SELECT CAST((julianday('now') - julianday(cr.started_at)) * 86400 AS INTEGER)
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC
                LIMIT 1
            ) AS seconds_since_last_run,
            (
                SELECT COUNT(1)
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                  AND cr.status = 'success'
                  AND cr.started_at >= datetime('now', '-24 hours')
            ) AS success_24h,
            (
                SELECT COUNT(1)
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                  AND cr.status = 'failed'
                  AND cr.started_at >= datetime('now', '-24 hours')
            ) AS failed_24h,
            (
                SELECT SUM(cr.new_article_count)
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                  AND cr.started_at >= datetime('now', '-24 hours')
            ) AS new_24h
        FROM sources s
        WHERE s.enabled = 1
        ORDER BY s.source_category, s.name
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _freshness_score(sources: list[dict]) -> float:
    if not sources:
        return 0
    scores = []
    for source in sources:
        interval = int(source.get("crawl_interval_seconds") or 300)
        elapsed = source.get("seconds_since_last_run")
        if elapsed is None:
            scores.append(0)
        elif elapsed <= interval * 2:
            scores.append(100)
        elif elapsed <= interval * 6:
            scores.append(65)
        else:
            scores.append(20)
    return sum(scores) / len(scores)


def _success_score(sources: list[dict]) -> float:
    measured = [(source.get("success_24h") or 0, source.get("failed_24h") or 0) for source in sources]
    total_success = sum(success for success, _ in measured)
    total_failed = sum(failed for _, failed in measured)
    if total_success + total_failed == 0:
        return 50
    return total_success / (total_success + total_failed) * 100


def _coverage_matrix(conn: sqlite3.Connection) -> dict:
    matrix = []
    covered = 0
    total = 0
    for category in MISSION_CATEGORIES:
        for region, aliases in REGION_ALIASES.items():
            where = " OR ".join("region_tags LIKE ?" for _ in aliases)
            count = conn.execute(
                f"""
                SELECT COUNT(1)
                FROM articles
                WHERE source_category = ?
                  AND collected_at >= datetime('now', '-24 hours')
                  AND ({where})
                """,
                [category, *[f"%{alias}%" for alias in aliases]],
            ).fetchone()[0]
            total += 1
            if count:
                covered += 1
            matrix.append({"category": category, "region": region, "count": count, "covered": bool(count)})
    return {"coverage_score": round((covered / total * 100) if total else 0, 1), "matrix": matrix}


def _blind_spots(coverage: dict, sources: list[dict]) -> list[dict]:
    spots = []
    for cell in coverage["matrix"]:
        if not cell["covered"] and cell["category"] in FIRST_PARTY_CATEGORIES:
            spots.append(
                {
                    "type": "coverage",
                    "severity": "warning",
                    "title": f"{_category_label(cell['category'])}/{_region_label(cell['region'])} 24시간 공백",
                    "action": "지역 키워드 또는 기관 출처 추가 점검",
                }
            )
    stale_sources = [
        source
        for source in sources
        if source.get("seconds_since_last_run") is None
        or source.get("seconds_since_last_run") > max(int(source.get("crawl_interval_seconds") or 300) * 6, 1800)
    ]
    if len(sources) >= 3 and len(stale_sources) / max(len(sources), 1) >= 0.6:
        spots.append(
            {
                "type": "source",
                "severity": "warning",
                "title": "전체 자동 수집 공백",
                "action": "개별 출처보다 자동 수집기 실행 상태를 먼저 확인",
            }
        )
        return spots[:10]
    for source in stale_sources[:6]:
        spots.append(
            {
                "type": "source",
                "severity": "danger",
                "title": f"{source['name']} 수집 지연",
                "action": "자동 수집기 상태, 네트워크, 사이트 구조 변경 확인",
            }
        )
    return spots[:10]


def _anomaly_signals(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            source_category,
            SUM(CASE WHEN collected_at >= datetime('now', '-1 hour') THEN 1 ELSE 0 END) AS last_hour,
            SUM(CASE WHEN collected_at >= datetime('now', '-24 hours') THEN 1 ELSE 0 END) AS last_day
        FROM articles
        GROUP BY source_category
        """
    ).fetchall()
    signals = []
    for row in rows:
        day = row["last_day"] or 0
        hour = row["last_hour"] or 0
        hourly_baseline = max(day / 24, 1)
        ratio = hour / hourly_baseline
        if hour >= 5 and ratio >= 2:
            signals.append(
                {
                    "category": row["source_category"],
                    "title": f"{_category_label(row['source_category'])} 기사 급증",
                    "last_hour": hour,
                    "baseline": round(hourly_baseline, 1),
                    "ratio": round(ratio, 1),
                }
            )
    return sorted(signals, key=lambda item: item["ratio"], reverse=True)[:6]


def _verification_pressure(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(1) AS total,
            SUM(CASE WHEN verification_status = 'needs_review' THEN 1 ELSE 0 END) AS needs_review,
            SUM(CASE WHEN newsroom_status = 'ready' THEN 1 ELSE 0 END) AS ready
        FROM articles
        WHERE collected_at >= datetime('now', '-24 hours')
        """
    ).fetchone()
    total = row["total"] or 0
    needs_review = row["needs_review"] or 0
    pressure = needs_review / total * 100 if total else 0
    score = max(20, 100 - pressure)
    return {
        "total_24h": total,
        "needs_review_24h": needs_review,
        "ready_24h": row["ready"] or 0,
        "pressure_percent": round(pressure, 1),
        "score": round(score, 1),
    }


def _watch_playbooks(conn: sqlite3.Connection) -> list[dict]:
    rules = suggested_alert_rules(conn)
    playbooks = [
        {
            "name": "재난 속보",
            "trigger": "재난문자·소방·기상 출처에서 사망/대피/특보 신호",
            "query": "사망 대피 기상특보 화재 폭발",
            "action": "기관 발표 확인 후 현장/지역 기자 배정",
        },
        {
            "name": "수사 이슈",
            "trigger": "압수수색·체포·구속·단독 키워드",
            "query": "압수수색 체포 구속 단독",
            "action": "관할기관과 피의사실 공표 위험 확인",
        },
        {
            "name": "지역 블라인드 스팟",
            "trigger": "지역별 24시간 공백 또는 출처 지연",
            "query": "지역명 화재 사고 대피",
            "action": "지자체·소방·경찰 보도자료 수동 확인",
        },
    ]
    for rule in rules[:3]:
        playbooks.append(
            {
                "name": rule["name"],
                "trigger": f"기준 {rule['threshold']} 이상",
                "query": rule["query"],
                "action": "현재 검색 조건으로 조용히 검색 후 필요 시 새로 수집",
            }
        )
    return playbooks


def _source_recommendations(sources: list[dict]) -> list[dict]:
    recommendations = []
    for source in sources:
        if source.get("failed_24h"):
            recommendations.append(
                {
                    "source_name": source["name"],
                    "priority": "높음",
                    "reason": f"24시간 실패 {source['failed_24h']}회",
                    "action": "timeout/retry 조정 또는 선택자 점검",
                }
            )
        elif (source.get("new_24h") or 0) == 0 and source.get("success_24h"):
            recommendations.append(
                {
                    "source_name": source["name"],
                    "priority": "보통",
                    "reason": "성공했지만 신규 기사 없음",
                    "action": "중복 정상인지, 목록 URL이 최신인지 확인",
                }
            )
    return recommendations[:8]


def compare_cluster_for_reporter(conn: sqlite3.Connection, duplicate_group_id: str) -> dict:
    rows = conn.execute(
        """
        SELECT *
        FROM articles
        WHERE duplicate_group_id = ?
        ORDER BY importance_score DESC, collected_at DESC
        LIMIT 30
        """,
        (duplicate_group_id,),
    ).fetchall()
    articles = [dict(row) for row in rows]
    conflicts = _detect_conflicts(articles)
    return {
        "duplicate_group_id": duplicate_group_id,
        "article_count": len(articles),
        "sources": sorted({item["source_name"] for item in articles}),
        "conflict_count": len(conflicts),
        "conflict_summary": _conflict_summary(conflicts),
        "timeline": [
            {
                "id": item["id"],
                "time": item.get("published_at") or item.get("collected_at"),
                "source_name": item.get("source_name"),
                "title": item.get("title"),
                "url": item.get("url"),
            }
            for item in sorted(articles, key=lambda item: item.get("published_at") or item.get("collected_at") or "")
        ],
        "conflicts": conflicts,
    }


def suggested_alert_rules(conn: sqlite3.Connection) -> list[dict]:
    region_rows = conn.execute(
        """
        SELECT region_tags, COUNT(1) AS count
        FROM articles
        WHERE region_tags IS NOT NULL AND region_tags != ''
        GROUP BY region_tags
        ORDER BY count DESC
        LIMIT 5
        """
    ).fetchall()
    regions = []
    for row in region_rows:
        regions.extend(_json_list(row["region_tags"]))
    top_regions = [name for name, _ in Counter(regions).most_common(4)]
    base_rules = [
        {"name": "긴급 재난", "query": "사망 OR 심정지 OR 대피", "category": "disaster", "threshold": 6},
        {"name": "화재/폭발", "query": "화재 OR 폭발 OR 산불", "category": "fire", "threshold": 5},
        {"name": "수사/압수수색", "query": "압수수색 OR 구속 OR 체포", "category": "news", "threshold": 5},
        {"name": "기상특보", "query": "기상특보 OR 호우 OR 폭염 OR 태풍", "category": "weather", "threshold": 4},
        {"name": "감염병/보건", "query": "감염병 OR 확진 OR 백신", "category": "health", "threshold": 4},
    ]
    for region in top_regions[:3]:
        base_rules.append({"name": f"{region} 지역 긴급", "query": f"{region} + 화재/사고/대피", "category": "", "threshold": 5})
    return base_rules


def today_budget(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT source_category, newsroom_status, COUNT(1) AS count, MAX(importance_score) AS max_score
        FROM articles
        WHERE collected_at >= datetime('now', '-24 hours')
        GROUP BY source_category, newsroom_status
        ORDER BY max_score DESC, count DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _beat_modes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT source_category, COUNT(1) AS count, MAX(importance_score) AS max_score
        FROM articles
        GROUP BY source_category
        ORDER BY max_score DESC, count DESC
        """
    ).fetchall()
    return [
        {
            "category": row["source_category"],
            "count": row["count"],
            "max_score": row["max_score"],
            "default_filter": f"source_category={row['source_category']}",
        }
        for row in rows
    ]


def _source_speed_summary(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            source_name,
            COUNT(1) AS runs,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_runs,
            SUM(new_article_count) AS new_articles,
            AVG(CASE
                WHEN finished_at IS NOT NULL
                THEN (julianday(finished_at) - julianday(started_at)) * 86400
                ELSE NULL
            END) AS avg_seconds
        FROM crawl_runs
        GROUP BY source_name
        HAVING runs > 0
        ORDER BY new_articles DESC, success_runs DESC
        LIMIT 10
        """
    ).fetchall()
    return [
        {
            "source_name": row["source_name"],
            "runs": row["runs"],
            "success_rate": round((row["success_runs"] or 0) / row["runs"] * 100, 1),
            "new_articles": row["new_articles"] or 0,
            "avg_seconds": round(row["avg_seconds"] or 0, 1),
        }
        for row in rows
    ]


def _detect_conflicts(articles: list[dict]) -> list[dict]:
    patterns = {
        "인명/건수": r"\d+\s*(?:명|건|곳|대)",
        "시각": r"\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?",
        "날짜": r"\d{1,2}\s*월\s*\d{1,2}\s*일",
    }
    conflicts = []
    for label, pattern in patterns.items():
        values = {}
        for article in articles:
            found = sorted(set(re.findall(pattern, _article_text(article))))
            if found:
                values[article["source_name"]] = found[:5]
        unique = {tuple(value) for value in values.values()}
        if len(unique) > 1:
            conflicts.append({"field": label, "values": values})
    return conflicts


def _conflict_summary(conflicts: list[dict]) -> str:
    if not conflicts:
        return "충돌 신호 없음"
    labels = ", ".join(conflict["field"] for conflict in conflicts[:3])
    return f"확인 필요: {labels}"


def _contact_hint(article: dict) -> str:
    category = article.get("source_category")
    if category == "fire":
        return "소방 상황실/관할 소방서 확인"
    if category == "police":
        return "관할 경찰서 형사/교통과 확인"
    if category == "weather":
        return "기상청 특보·예보관 브리핑 확인"
    if category == "disaster":
        return "지자체 재난안전상황실 확인"
    if category == "health":
        return "질병관리청/보건소 확인"
    if category == "government":
        return "담당 부처 공보실 확인"
    return "원문 기자/기관 공보실 교차 확인"


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def _category_label(value: str) -> str:
    return {
        "disaster": "재난",
        "fire": "소방",
        "police": "경찰",
        "weather": "기상",
        "health": "보건",
        "government": "정부",
        "news": "뉴스",
    }.get(value, value or "기타")


def _region_label(value: str) -> str:
    return {
        "capital": "수도권",
        "gangwon": "강원",
        "chungcheong": "충청",
        "honam": "호남",
        "yeongnam": "영남",
        "jeju": "제주",
    }.get(value, value or "전국")


def _is_first_party_source(source_name: str) -> bool:
    return any(token.lower() in source_name.lower() for token in FIRST_PARTY_SOURCES)


def _article_text(article: dict) -> str:
    return " ".join(
        str(article.get(key) or "")
        for key in ("title", "summary", "body_text", "keywords", "region_tags", "source_name")
    )


def _json_list(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]
