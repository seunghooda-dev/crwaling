import json
import sqlite3
from datetime import datetime, timedelta

from app.author import clean_author_display, infer_reporter_from_article_text
from app.models import Article, Source
from app.services.region_service import region_group_aliases, region_group_label, region_group_options
from app.services.reliability_service import article_reliability
from app.text import article_fingerprint, canonicalize_url, normalize_space
from app.title_extractor import split_title_summary


URGENT_SIGNAL_KEYWORDS = (
    "속보",
    "긴급",
    "재난",
    "대피",
    "대피령",
    "통제",
    "교통통제",
    "화재",
    "산불",
    "폭발",
    "침수",
    "붕괴",
    "심정지",
    "사망",
    "실종",
    "교통사고",
    "지진",
    "기상특보",
    "호우",
    "대설",
    "태풍",
    "폭염",
    "한파",
    "화학물질",
    "유해물질",
    "집단감염",
    "미사일",
)
URGENT_CATEGORY_VALUES = ("disaster", "fire", "weather", "health")
URGENT_EXCLUDE_KEYWORDS = (
    "대회",
    "기술경연",
    "훈련",
    "캠페인",
    "예방",
    "대비책",
    "종합대책",
    "본격화",
    "추진",
    "점검",
    "논란",
    "오발송",
    "오발령",
    "실제상황이 아니",
    "실제 상황이 아니",
    "훈련상황",
    "훈련 메시지",
)
STALLED_CRAWL_RUN_SECONDS = 15 * 60
GLOBAL_IDLE_MIN_ENABLED_SOURCES = 3
GLOBAL_IDLE_STALE_RATIO = 0.6
DETAIL_BODY_MIN_LENGTH = 80
DETAIL_QUEUE_PRIORITY_SQL = """
(
    newsroom_status = 'ready'
    OR verification_status = 'needs_review'
    OR importance_score >= 4.5
)
"""
NAVIGATION_TITLE_EXACT = {
    "개인정보처리방침",
    "공공데이터 홍보",
    "사전정보공표목록",
    "누리집 안내지도",
    "업무추진비 공개",
    "대메뉴 바로가기",
    "본문 내용 바로가기",
    "온라인 민원FAQ",
    "온라인 민원신청",
    "정부포상 후보자 공개검증",
    "세입세출예산운용현황",
    "훈령·예규·고시",
    "푸터 내용 바로가기",
}
NAVIGATION_TITLE_WORDS = (
    "본문 바로가기",
    "푸터 내용 바로가기",
    "개인정보처리방침",
    "사이트맵",
    "사전정보공표",
    "세입세출예산",
    "업무추진비 공개",
    "누리집 안내지도",
    "페이지로 이동",
    "대메뉴 바로가기",
)
READY_KEYWORDS = {
    "화재",
    "산불",
    "폭발",
    "대피",
    "대피령",
    "사망",
    "실종",
    "심정지",
    "교통사고",
    "붕괴",
    "침수",
    "지진",
    "기상특보",
    "호우",
    "대설",
    "태풍",
    "한파",
    "폭염",
    "화학물질",
    "유해물질",
    "집단감염",
}


def classify_crawl_error(error_message: str | None) -> dict:
    if not error_message:
        return {
            "failure_cause": None,
            "failure_cause_label": None,
            "failure_hint": None,
            "failure_action": None,
            "failure_group": None,
            "failure_retryable": False,
            "failure_severity": "normal",
        }

    message = str(error_message)
    normalized = message.casefold()
    if any(pattern in normalized for pattern in ("winerror 10054", "connection reset", "remote host", "강제로 끊")):
        return {
            "failure_cause": "connection_reset",
            "failure_cause_label": "원격 연결 종료",
            "failure_hint": "상대 서버가 연결을 끊었습니다. 일시 차단이나 순간 접속 불안정 가능성이 큽니다.",
            "failure_action": "다음 조회에서 재시도하고 반복되면 해당 출처의 수집 간격을 늘리세요.",
            "failure_group": "network",
            "failure_retryable": True,
            "failure_severity": "warning",
        }
    if any(
        pattern in normalized
        for pattern in ("timeout", "timed out", "readtimeout", "시간 초과", "server disconnected")
    ):
        return {
            "failure_cause": "timeout",
            "failure_cause_label": "응답 시간 초과",
            "failure_hint": "서버 응답이 제한 시간 안에 도착하지 않았습니다.",
            "failure_action": "timeout 값을 늘리거나 재시도 횟수를 조정하세요.",
            "failure_group": "network",
            "failure_retryable": True,
            "failure_severity": "warning",
        }
    if any(pattern in normalized for pattern in ("429", "too many requests", "rate limit", "ratelimit")):
        return {
            "failure_cause": "rate_limited",
            "failure_cause_label": "요청 제한",
            "failure_hint": "사이트가 짧은 시간 안의 반복 요청을 제한했습니다.",
            "failure_action": "해당 출처 수집 간격을 늘리고 잠시 뒤 재시도하세요.",
            "failure_group": "access",
            "failure_retryable": True,
            "failure_severity": "warning",
        }
    if any(pattern in normalized for pattern in ("403", "forbidden", "access denied", "차단")):
        return {
            "failure_cause": "blocked",
            "failure_cause_label": "접속 차단 가능",
            "failure_hint": "사이트가 요청을 거절했습니다. 헤더, 접속 간격, 리다이렉트를 확인해야 합니다.",
            "failure_action": "User-Agent/Referer와 호출 빈도를 점검하세요.",
            "failure_group": "access",
            "failure_retryable": False,
            "failure_severity": "danger",
        }
    if any(pattern in normalized for pattern in ("404", "not found")):
        return {
            "failure_cause": "not_found",
            "failure_cause_label": "주소 변경 가능",
            "failure_hint": "목록 또는 기사 URL이 더 이상 존재하지 않을 수 있습니다.",
            "failure_action": "출처 URL과 링크 선택자를 다시 확인하세요.",
            "failure_group": "configuration",
            "failure_retryable": False,
            "failure_severity": "danger",
        }
    if any(pattern in normalized for pattern in ("ssl", "certificate", "certifi")):
        return {
            "failure_cause": "ssl",
            "failure_cause_label": "SSL 인증 오류",
            "failure_hint": "인증서 검증이나 HTTPS 연결 과정에서 문제가 발생했습니다.",
            "failure_action": "인증서 체인과 요청 라이브러리 설정을 확인하세요.",
            "failure_group": "network",
            "failure_retryable": False,
            "failure_severity": "danger",
        }
    if any(pattern in normalized for pattern in ("getaddrinfo", "name resolution", "resolve host", "dns", "nodename")):
        return {
            "failure_cause": "dns",
            "failure_cause_label": "주소 확인 실패",
            "failure_hint": "도메인 이름을 네트워크에서 해석하지 못했습니다.",
            "failure_action": "네트워크와 출처 도메인 주소를 확인하세요.",
            "failure_group": "network",
            "failure_retryable": False,
            "failure_severity": "danger",
        }
    if any(pattern in normalized for pattern in ("too many redirects", "redirect")):
        return {
            "failure_cause": "redirect",
            "failure_cause_label": "리다이렉트 문제",
            "failure_hint": "사이트가 여러 번 이동시키거나 로그인/차단 페이지로 보냈을 수 있습니다.",
            "failure_action": "최종 도착 URL과 쿠키/헤더 조건을 확인하세요.",
            "failure_group": "access",
            "failure_retryable": False,
            "failure_severity": "warning",
        }
    if any(
        pattern in normalized
        for pattern in ("selector", "parse", "parsing", "beautifulsoup", "jsondecode", "expecting value", "선택자")
    ):
        return {
            "failure_cause": "parser",
            "failure_cause_label": "구조 변경 가능",
            "failure_hint": "페이지 구조가 바뀌어 목록이나 본문 선택자가 맞지 않을 수 있습니다.",
            "failure_action": "HTML 선택자와 기사 링크 규칙을 다시 잡으세요.",
            "failure_group": "parser",
            "failure_retryable": False,
            "failure_severity": "warning",
        }
    return {
        "failure_cause": "unknown",
        "failure_cause_label": "오류 원인 확인 필요",
        "failure_hint": "분류되지 않은 수집 오류입니다. 원문 오류 메시지를 확인해야 합니다.",
        "failure_action": "최근 실행 로그의 상세 오류를 기준으로 출처별 점검을 진행하세요.",
        "failure_group": "unknown",
        "failure_retryable": False,
        "failure_severity": "warning",
    }


def upsert_source(conn: sqlite3.Connection, source: Source) -> None:
    conn.execute(
        f"""
        INSERT INTO sources (
            name, source_type, url, source_category, enabled,
            crawl_interval_seconds, timeout_seconds, max_retries
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            source_type = excluded.source_type,
            url = excluded.url,
            source_category = excluded.source_category,
            enabled = excluded.enabled,
            crawl_interval_seconds = excluded.crawl_interval_seconds,
            timeout_seconds = excluded.timeout_seconds,
            max_retries = excluded.max_retries
        """,
        (
            source.name,
            source.source_type.value,
            str(source.url),
            source.source_category,
            int(source.enabled),
            source.crawl_interval_seconds,
            source.timeout_seconds,
            source.max_retries,
        ),
    )


def disable_sources_not_in(conn: sqlite3.Connection, source_names: list[str]) -> None:
    if not source_names:
        conn.execute("UPDATE sources SET enabled = 0")
        return
    placeholders = ",".join("?" for _ in source_names)
    conn.execute(f"UPDATE sources SET enabled = 0 WHERE name NOT IN ({placeholders})", source_names)


def insert_article(conn: sqlite3.Connection, article: Article) -> bool:
    author = clean_author_display(article.author)
    canonical_url = canonicalize_url(article.canonical_url or article.url)
    if canonical_url:
        existing = conn.execute(
            """
            SELECT 1
            FROM articles
            WHERE source_name = ?
              AND canonical_url = ?
            LIMIT 1
            """,
            (article.source_name, canonical_url),
        ).fetchone()
        if existing:
            return False
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO articles (
            source_name, source_type, source_category, title, url, canonical_url, author, published_at,
            body_text, summary, category, keywords, image_urls, video_urls, fingerprint,
            duplicate_group_id, importance_score, verification_status, region_tags, quality_score,
            quality_flags, verification_checklist, raw_html_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article.source_name,
            article.source_type,
            article.source_category,
            article.title,
            article.url,
            canonical_url,
            author,
            article.published_at.isoformat() if article.published_at else None,
            article.body_text,
            article.summary,
            article.category,
            json.dumps(article.keywords, ensure_ascii=False),
            json.dumps(article.image_urls, ensure_ascii=False),
            json.dumps(article.video_urls, ensure_ascii=False),
            article.fingerprint,
            article.duplicate_group_id,
            article.importance_score,
            article.verification_status,
            json.dumps(article.region_tags, ensure_ascii=False),
            article.quality_score,
            json.dumps(article.quality_flags, ensure_ascii=False),
            json.dumps(article.verification_checklist, ensure_ascii=False),
            article.raw_html_path,
        ),
    )
    return cursor.rowcount > 0


def get_article_by_url(conn: sqlite3.Connection, url: str) -> dict | None:
    row = conn.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()
    return _article_row(row) if row else None


def list_sources(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def _article_filters(
    source_name: str | None = None,
    source_names: list[str] | None = None,
    q: str | None = None,
    min_importance: float | None = None,
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = None,
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
    needs_work: bool = False,
    needs_review: bool = False,
    missing_author: bool = False,
    has_alert: bool = False,
    has_body: bool = False,
    needs_detail: bool = False,
    use_fts: bool = False,
) -> tuple[list[str], list[object]]:
    where = []
    params: list[object] = []
    local_article_time = _article_local_time_sql()
    source_filter = _source_filter_values(source_name, source_names)
    if source_filter:
        placeholders = ",".join("?" for _ in source_filter)
        where.append(f"source_name IN ({placeholders})")
        params.extend(source_filter)
    if q and use_fts:
        where.append("articles.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)")
        params.append(_fts_match_query(q))
    elif q:
        where.append("(title LIKE ? OR summary LIKE ? OR body_text LIKE ? OR keywords LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if min_importance is not None:
        where.append("importance_score >= ?")
        params.append(min_importance)
    if newsroom_status:
        where.append("newsroom_status = ?")
        params.append(newsroom_status)
    if source_category:
        where.append("source_category = ?")
        params.append(source_category)
    if region_group:
        aliases = region_group_aliases(region_group)
        if aliases:
            where.append("(" + " OR ".join("region_tags LIKE ?" for _ in aliases) + ")")
            params.extend([f"%{alias}%" for alias in aliases])
    if assignee:
        where.append("assignee = ?")
        params.append(assignee)
    if collected_within_days:
        cutoff = datetime.now() - timedelta(days=collected_within_days)
        where.append(f"{local_article_time} >= ?")
        params.append(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    if collected_from:
        where.append(f"{local_article_time} >= ?")
        params.append(collected_from)
    if collected_to:
        where.append(f"{local_article_time} <= ?")
        params.append(collected_to)
    if needs_work:
        condition, condition_params = _needs_work_condition()
        where.append(condition)
        params.extend(condition_params)
    if needs_review:
        where.append("verification_status IN ('needs_review', 'caution')")
    if missing_author:
        where.append(
            """
            (
                source_category = 'news'
                AND source_type != 'api'
                AND (author IS NULL OR trim(author) = '')
            )
            """
        )
    if has_alert:
        where.append("id IN (SELECT DISTINCT article_id FROM alert_events WHERE acknowledged = 0)")
    if has_body:
        where.append("body_text IS NOT NULL")
    if needs_detail:
        detail_where, detail_params = _detail_base_filters(True, None)
        where.append("(" + " AND ".join(detail_where) + ")")
        params.extend(detail_params)
    return where, params


def _needs_work_condition() -> tuple[str, list[object]]:
    return (
        """
        (
            newsroom_status NOT IN ('done', 'hold')
            AND (
                quality_score < 70
                OR verification_status IN ('needs_review', 'caution')
                OR (
                    (
                        (
                            (body_text IS NULL OR length(trim(body_text)) < ?)
                            AND (summary IS NULL OR length(trim(summary)) < ?)
                        )
                        OR (
                            source_category = 'news'
                            AND source_type != 'api'
                            AND (author IS NULL OR trim(author) = '')
                        )
                    )
                    AND (
                        newsroom_status = 'ready'
                        OR verification_status = 'needs_review'
                        OR importance_score >= 4.5
                    )
                )
                OR (
                    importance_score >= 4.5
                    AND (
                        SELECT COUNT(DISTINCT peer.source_name)
                        FROM articles peer
                        WHERE peer.duplicate_group_id = articles.duplicate_group_id
                    ) < 2
                )
            )
        )
        """,
        [DETAIL_BODY_MIN_LENGTH, DETAIL_BODY_MIN_LENGTH],
    )


def _source_filter_values(source_name: str | None = None, source_names: list[str] | None = None) -> list[str]:
    values: list[str] = []
    if source_name:
        values.append(source_name)
    values.extend(source_names or [])
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def article_fts_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'articles_fts'"
    ).fetchone()
    return row is not None


def _fts_match_query(q: str) -> str:
    tokens = [token.strip() for token in normalize_space(q).split(" ") if token.strip()]
    if not tokens:
        return '""'
    phrases = []
    for token in tokens:
        escaped = token.replace('"', '""')
        phrases.append(f'"{escaped}"')
    return " AND ".join(phrases)


def _article_row(row: sqlite3.Row) -> dict:
    article = dict(row)
    article["author"] = clean_author_display(article.get("author"))
    article["reliability"] = article_reliability(article)
    return article


def _article_time_sql() -> str:
    return _article_local_time_sql()


def _article_local_time_sql() -> str:
    published_local = """
        CASE
            WHEN published_at IS NULL OR published_at = '' THEN NULL
            WHEN instr(substr(published_at, 11), '+') > 0
              OR instr(substr(published_at, 11), '-') > 0
              OR upper(substr(published_at, -1)) = 'Z'
            THEN datetime(published_at, '+9 hours')
            ELSE datetime(published_at)
        END
    """
    return f"COALESCE({published_local}, datetime(collected_at, '+9 hours'), collected_at)"


def _article_order(sort: str | None) -> str:
    article_time = _article_time_sql()
    unknown_published = "CASE WHEN published_at IS NULL OR published_at = '' THEN 1 ELSE 0 END"
    return {
        "latest": f"{unknown_published} ASC, {article_time} DESC, importance_score DESC, id DESC",
        "oldest": f"{unknown_published} ASC, {article_time} ASC, importance_score DESC, id ASC",
        "importance": f"importance_score DESC, {article_time} DESC, id DESC",
        "source": f"source_name ASC, {unknown_published} ASC, {article_time} DESC, id DESC",
        "ready": f"CASE WHEN newsroom_status = 'ready' THEN 0 ELSE 1 END, importance_score DESC, {article_time} DESC, id DESC",
    }.get(sort or "latest", f"{unknown_published} ASC, {article_time} DESC, importance_score DESC, id DESC")


def list_articles(
    conn: sqlite3.Connection,
    limit: int = 50,
    offset: int = 0,
    source_name: str | None = None,
    source_names: list[str] | None = None,
    q: str | None = None,
    min_importance: float | None = None,
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = None,
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
    needs_work: bool = False,
    needs_review: bool = False,
    missing_author: bool = False,
    has_alert: bool = False,
    has_body: bool = False,
    needs_detail: bool = False,
    sort: str | None = None,
) -> list[dict]:
    use_fts = bool(q and article_fts_ready(conn))
    where, params = _article_filters(
        source_name=source_name,
        source_names=source_names,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
        collected_within_days=collected_within_days,
        collected_from=collected_from,
        collected_to=collected_to,
        region_group=region_group,
        needs_work=needs_work,
        needs_review=needs_review,
        missing_author=missing_author,
        has_alert=has_alert,
        has_body=has_body,
        needs_detail=needs_detail,
        use_fts=use_fts,
    )

    sql = """
        SELECT
            articles.*,
            (
                SELECT COUNT(DISTINCT peer.source_name)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_source_count,
            (
                SELECT COUNT(1)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_article_count
        FROM articles
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {_article_order(sort)} LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        if not use_fts:
            raise
        where, params = _article_filters(
            source_name=source_name,
            source_names=source_names,
            q=q,
            min_importance=min_importance,
            newsroom_status=newsroom_status,
            source_category=source_category,
            assignee=assignee,
            collected_within_days=collected_within_days,
            collected_from=collected_from,
            collected_to=collected_to,
            region_group=region_group,
            needs_work=needs_work,
            needs_review=needs_review,
            missing_author=missing_author,
            has_alert=has_alert,
            has_body=has_body,
            needs_detail=needs_detail,
            use_fts=False,
        )
        sql = """
            SELECT
                articles.*,
                (
                    SELECT COUNT(DISTINCT peer.source_name)
                    FROM articles peer
                    WHERE peer.duplicate_group_id = articles.duplicate_group_id
                ) AS cluster_source_count,
                (
                    SELECT COUNT(1)
                    FROM articles peer
                    WHERE peer.duplicate_group_id = articles.duplicate_group_id
                ) AS cluster_article_count
            FROM articles
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {_article_order(sort)} LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
    return [_article_row(row) for row in rows]


def count_articles(
    conn: sqlite3.Connection,
    source_name: str | None = None,
    source_names: list[str] | None = None,
    q: str | None = None,
    min_importance: float | None = None,
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = None,
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
    needs_work: bool = False,
    needs_review: bool = False,
    missing_author: bool = False,
    has_alert: bool = False,
    has_body: bool = False,
    needs_detail: bool = False,
) -> int:
    use_fts = bool(q and article_fts_ready(conn))
    where, params = _article_filters(
        source_name=source_name,
        source_names=source_names,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
        collected_within_days=collected_within_days,
        collected_from=collected_from,
        collected_to=collected_to,
        region_group=region_group,
        needs_work=needs_work,
        needs_review=needs_review,
        missing_author=missing_author,
        has_alert=has_alert,
        has_body=has_body,
        needs_detail=needs_detail,
        use_fts=use_fts,
    )
    sql = "SELECT COUNT(1) FROM articles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.OperationalError:
        if not use_fts:
            raise
        where, params = _article_filters(
            source_name=source_name,
            source_names=source_names,
            q=q,
            min_importance=min_importance,
            newsroom_status=newsroom_status,
            source_category=source_category,
            assignee=assignee,
            collected_within_days=collected_within_days,
            collected_from=collected_from,
            collected_to=collected_to,
            region_group=region_group,
            needs_work=needs_work,
            needs_review=needs_review,
            missing_author=missing_author,
            has_alert=has_alert,
            has_body=has_body,
            needs_detail=needs_detail,
            use_fts=False,
        )
        sql = "SELECT COUNT(1) FROM articles"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return int(conn.execute(sql, params).fetchone()[0])


def list_priority_articles(
    conn: sqlite3.Connection,
    limit: int = 50,
    source_names: list[str] | None = None,
    source_category: str | None = None,
    region_group: str | None = None,
) -> list[dict]:
    where = ["importance_score > 0"]
    params: list[object] = []
    source_filter = _source_filter_values(source_names=source_names)
    if source_filter:
        placeholders = ",".join("?" for _ in source_filter)
        where.append(f"source_name IN ({placeholders})")
        params.extend(source_filter)
    if source_category:
        where.append("source_category = ?")
        params.append(source_category)
    aliases = region_group_aliases(region_group)
    if aliases:
        where.append("(" + " OR ".join("region_tags LIKE ?" for _ in aliases) + ")")
        params.extend([f"%{alias}%" for alias in aliases])
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            articles.*,
            (
                SELECT COUNT(DISTINCT peer.source_name)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_source_count,
            (
                SELECT COUNT(1)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_article_count
        FROM articles
        WHERE {" AND ".join(where)}
        ORDER BY importance_score DESC, {_article_time_sql()} DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_article_row(row) for row in rows]


def list_alert_articles(
    conn: sqlite3.Connection,
    limit: int = 50,
    threshold: float = 4.0,
    source_names: list[str] | None = None,
    source_category: str | None = None,
    region_group: str | None = None,
    strict_urgent: bool = False,
    collected_within_hours: int | None = None,
) -> list[dict]:
    params: list[object] = [threshold]
    filters = []
    source_filter = _source_filter_values(source_names=source_names)
    if source_filter:
        placeholders = ",".join("?" for _ in source_filter)
        filters.append(f"source_name IN ({placeholders})")
        params.extend(source_filter)
    if source_category:
        filters.append("source_category = ?")
        params.append(source_category)
    aliases = region_group_aliases(region_group)
    if aliases:
        filters.append("(" + " OR ".join("region_tags LIKE ?" for _ in aliases) + ")")
        params.extend([f"%{alias}%" for alias in aliases])
    if collected_within_hours:
        cutoff = datetime.now() - timedelta(hours=collected_within_hours)
        filters.append(f"{_article_time_sql()} >= ?")
        params.append(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    if strict_urgent:
        filters.extend(
            [
                "newsroom_status NOT IN ('done', 'hold')",
                "quality_score >= 70",
                "length(title) <= 240",
                "(quality_flags IS NULL OR quality_flags NOT LIKE '%navigation_like_title%')",
            ]
        )
        term_checks = []
        for keyword in URGENT_SIGNAL_KEYWORDS:
            term_checks.append("(title LIKE ? OR summary LIKE ? OR keywords LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        category_placeholders = ",".join("?" for _ in URGENT_CATEGORY_VALUES)
        filters.append(
            f"(({ ' OR '.join(term_checks) }) OR source_category IN ({category_placeholders}))"
        )
        params.extend(URGENT_CATEGORY_VALUES)
        for keyword in URGENT_EXCLUDE_KEYWORDS:
            filters.append("(title NOT LIKE ? AND COALESCE(summary, '') NOT LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
    params.append(limit)
    filter_sql = f"AND {' AND '.join(filters)}" if filters else ""
    base_condition = "importance_score >= ?" if strict_urgent else "(importance_score >= ? OR verification_status = 'needs_review')"
    rows = conn.execute(
        f"""
        SELECT
            articles.*,
            (
                SELECT COUNT(DISTINCT peer.source_name)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_source_count,
            (
                SELECT COUNT(1)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_article_count
        FROM articles
        WHERE {base_condition}
        {filter_sql}
        ORDER BY importance_score DESC, cluster_source_count DESC, {_article_time_sql()} DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_article_row(row) for row in rows]


def list_region_counts(conn: sqlite3.Connection) -> list[dict]:
    rows = []
    for option in region_group_options():
        aliases = region_group_aliases(option["value"])
        if not aliases:
            continue
        where = " OR ".join("region_tags LIKE ?" for _ in aliases)
        count = conn.execute(
            f"SELECT COUNT(1) FROM articles WHERE {where}",
            [f"%{alias}%" for alias in aliases],
        ).fetchone()[0]
        rows.append(
            {
                "region_group": option["value"],
                "label": region_group_label(option["value"]),
                "count": count,
            }
        )
    return rows


def insert_alert_event(conn: sqlite3.Connection, article: dict, reason: str) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO alert_events (
            article_id, title, source_name, importance_score, reason
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            article["id"],
            article["title"],
            article["source_name"],
            article["importance_score"],
            reason,
        ),
    )
    return cursor.rowcount > 0


def list_alert_events(conn: sqlite3.Connection, limit: int = 50, acknowledged: bool | None = None) -> list[dict]:
    if acknowledged is None:
        rows = conn.execute(
            "SELECT * FROM alert_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM alert_events WHERE acknowledged = ? ORDER BY created_at DESC LIMIT ?",
            (int(acknowledged), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def acknowledge_alert_event(conn: sqlite3.Connection, alert_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM alert_events WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE alert_events SET acknowledged = 1 WHERE id = ?", (alert_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM alert_events WHERE id = ?", (alert_id,)).fetchone()
    return dict(row) if row else None


def acknowledge_all_alert_events(conn: sqlite3.Connection) -> int:
    count = conn.execute("UPDATE alert_events SET acknowledged = 1 WHERE acknowledged = 0").rowcount
    conn.commit()
    return count


def insert_notification_event(
    conn: sqlite3.Connection,
    *,
    article_id: int | None,
    channel: str,
    destination: str | None,
    status: str,
    message: str | None,
) -> dict:
    cursor = conn.execute(
        """
        INSERT INTO notification_events (article_id, channel, destination, status, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (article_id, channel, destination, status, message),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM notification_events WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def list_notification_events(
    conn: sqlite3.Connection,
    *,
    article_id: int | None = None,
    limit: int = 20,
) -> list[dict]:
    if article_id is not None:
        rows = conn.execute(
            """
            SELECT * FROM notification_events
            WHERE article_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (article_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM notification_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_issue_clusters(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            duplicate_group_id,
            COUNT(*) AS article_count,
            COUNT(DISTINCT source_name) AS source_count,
            MAX(importance_score) AS max_importance,
            MIN(published_at) AS first_published_at,
            MAX(collected_at) AS last_collected_at,
            MIN(title) AS sample_title
        FROM articles
        WHERE duplicate_group_id IS NOT NULL
        GROUP BY duplicate_group_id
        HAVING COUNT(*) > 1 OR MAX(importance_score) > 0
        ORDER BY max_importance DESC, article_count DESC, last_collected_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_cluster_articles(conn: sqlite3.Connection, duplicate_group_id: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            articles.*,
            (
                SELECT COUNT(DISTINCT peer.source_name)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_source_count,
            (
                SELECT COUNT(1)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_article_count
        FROM articles
        WHERE duplicate_group_id = ?
        ORDER BY importance_score DESC, collected_at DESC
        LIMIT ?
        """,
        (duplicate_group_id, limit),
    ).fetchall()
    return [_article_row(row) for row in rows]


def get_article(conn: sqlite3.Connection, article_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT
            articles.*,
            (
                SELECT COUNT(DISTINCT peer.source_name)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_source_count,
            (
                SELECT COUNT(1)
                FROM articles peer
                WHERE peer.duplicate_group_id = articles.duplicate_group_id
            ) AS cluster_article_count
        FROM articles
        WHERE id = ?
        """,
        (article_id,),
    ).fetchone()
    return _article_row(row) if row else None


def update_article_snapshot_path(conn: sqlite3.Connection, article_id: int, raw_html_path: str) -> None:
    conn.execute("UPDATE articles SET raw_html_path = ? WHERE id = ?", (raw_html_path, article_id))


def update_article_quality(
    conn: sqlite3.Connection,
    article_id: int,
    region_tags: str,
    quality_score: float,
    quality_flags: str,
    verification_checklist: str,
) -> None:
    conn.execute(
        """
        UPDATE articles
        SET region_tags = ?,
            quality_score = ?,
            quality_flags = ?,
            verification_checklist = COALESCE(verification_checklist, ?)
        WHERE id = ?
        """,
        (region_tags, quality_score, quality_flags, verification_checklist, article_id),
    )


def update_article_score(
    conn: sqlite3.Connection,
    article_id: int,
    keywords: str,
    importance_score: float,
    verification_status: str,
) -> None:
    conn.execute(
        """
        UPDATE articles
        SET keywords = ?,
            importance_score = ?,
            verification_status = ?
        WHERE id = ?
        """,
        (keywords, importance_score, verification_status, article_id),
    )


def update_article_summary_and_media(
    conn: sqlite3.Connection,
    article_id: int,
    summary: str | None,
    image_urls: str,
    video_urls: str,
) -> None:
    conn.execute(
        """
        UPDATE articles
        SET summary = ?,
            image_urls = ?,
            video_urls = ?
        WHERE id = ?
        """,
        (summary, image_urls, video_urls, article_id),
    )


def update_article_details(
    conn: sqlite3.Connection,
    article_id: int,
    body_text: str | None,
    author: str | None,
    image_urls: str,
    video_urls: str,
    raw_html_path: str | None,
) -> None:
    conn.execute(
        """
        UPDATE articles
        SET body_text = COALESCE(?, body_text),
            author = COALESCE(?, author),
            image_urls = ?,
            video_urls = ?,
            raw_html_path = COALESCE(?, raw_html_path)
        WHERE id = ?
        """,
        (body_text, author, image_urls, video_urls, raw_html_path, article_id),
    )


def update_article_cluster(conn: sqlite3.Connection, article_id: int, duplicate_group_id: str) -> None:
    conn.execute(
        "UPDATE articles SET duplicate_group_id = ? WHERE id = ?",
        (duplicate_group_id, article_id),
    )


def iter_articles(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    sql = "SELECT * FROM articles ORDER BY collected_at DESC"
    params: tuple[object, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    return [_article_row(row) for row in rows]


def update_article_workflow(
    conn: sqlite3.Connection,
    article_id: int,
    newsroom_status: str | None = None,
    verification_status: str | None = None,
    desk_notes: str | None = None,
    assignee: str | None = None,
) -> dict | None:
    current = get_article(conn, article_id)
    if current is None:
        return None

    conn.execute(
        """
        UPDATE articles
        SET newsroom_status = ?,
            verification_status = ?,
            desk_notes = ?,
            assignee = ?,
            status_updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            newsroom_status if newsroom_status is not None else current.get("newsroom_status", "new"),
            verification_status if verification_status is not None else current.get("verification_status", "unchecked"),
            desk_notes if desk_notes is not None else current.get("desk_notes"),
            assignee if assignee is not None else current.get("assignee"),
            article_id,
        ),
    )
    conn.commit()
    return get_article(conn, article_id)


def article_needs_detail(article: dict) -> bool:
    body = normalize_space(article.get("body_text") or article.get("summary") or "")
    author = normalize_space(article.get("author") or "")
    author_required = article.get("source_category") == "news" and article.get("source_type") != "api"
    return len(body) < DETAIL_BODY_MIN_LENGTH or (author_required and not author)


def _detail_missing_condition(missing: str | None = None) -> tuple[str, list[object]]:
    missing = (missing or "all").lower()
    body_sql = """
    (
        (body_text IS NULL OR length(trim(body_text)) < ?)
        AND (summary IS NULL OR length(trim(summary)) < ?)
    )
    """
    author_sql = """
    (
        source_category = 'news'
        AND source_type != 'api'
        AND (author IS NULL OR trim(author) = '')
    )
    """
    if missing == "body":
        return body_sql, [DETAIL_BODY_MIN_LENGTH, DETAIL_BODY_MIN_LENGTH]
    if missing == "author":
        return author_sql, []
    return f"({body_sql} OR {author_sql})", [DETAIL_BODY_MIN_LENGTH, DETAIL_BODY_MIN_LENGTH]


def _detail_base_filters(priority_only: bool, missing: str | None, source_name: str | None = None) -> tuple[list[str], list[object]]:
    missing_sql, params = _detail_missing_condition(missing)
    where = [
        "newsroom_status NOT IN ('done', 'hold')",
        "quality_score >= 50",
        "(quality_flags IS NULL OR quality_flags NOT LIKE '%navigation_like_title%')",
        missing_sql,
    ]
    if priority_only:
        where.append(DETAIL_QUEUE_PRIORITY_SQL)
    if source_name:
        where.append("source_name = ?")
        params.append(source_name)
    return where, params


def list_detail_enrichment_queue(
    conn: sqlite3.Connection,
    limit: int = 50,
    *,
    priority_only: bool = True,
    missing: str | None = None,
    source_name: str | None = None,
) -> list[dict]:
    where, where_params = _detail_base_filters(priority_only, missing, source_name)
    rows = conn.execute(
        f"""
        SELECT
            articles.*,
            CASE
                WHEN (body_text IS NULL OR length(trim(body_text)) < ?)
                 AND (summary IS NULL OR length(trim(summary)) < ?)
                THEN 1 ELSE 0
            END AS missing_body,
            CASE
                WHEN source_category = 'news'
                 AND source_type != 'api'
                 AND (author IS NULL OR trim(author) = '')
                THEN 1 ELSE 0
            END AS missing_author
        FROM articles
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE WHEN verification_status = 'needs_review' THEN 0 ELSE 1 END,
            CASE WHEN missing_body = 1 AND missing_author = 1 THEN 0 ELSE 1 END,
            importance_score DESC,
            CASE source_category
                WHEN 'disaster' THEN 0
                WHEN 'fire' THEN 1
                WHEN 'police' THEN 2
                WHEN 'weather' THEN 3
                WHEN 'health' THEN 4
                ELSE 5
            END,
            COALESCE(published_at, collected_at) DESC,
            id DESC
        LIMIT ?
        """,
        (DETAIL_BODY_MIN_LENGTH, DETAIL_BODY_MIN_LENGTH, *where_params, limit),
    ).fetchall()
    items = []
    for row in rows:
        item = _article_row(row)
        missing = []
        if item.get("missing_body"):
            missing.append("본문")
        if item.get("missing_author"):
            missing.append("기자명")
        item["detail_missing_fields"] = missing
        item["enrichment_reason"] = " · ".join(missing) + " 보강 필요"
        item["enrichment_priority"] = round(
            float(item.get("importance_score") or 0)
            + (2 if item.get("verification_status") == "needs_review" else 0)
            + (1 if item.get("source_category") in {"disaster", "fire", "police", "weather", "health"} else 0),
            1,
        )
        items.append(item)
    return items


def count_detail_enrichment_queue(
    conn: sqlite3.Connection,
    *,
    priority_only: bool = True,
    missing: str | None = None,
    source_name: str | None = None,
) -> int:
    where, params = _detail_base_filters(priority_only, missing, source_name)
    return int(
        conn.execute(
            f"""
            SELECT COUNT(1)
            FROM articles
            WHERE {" AND ".join(where)}
            """,
            params,
        ).fetchone()[0]
    )


def count_detail_backlog(conn: sqlite3.Connection) -> int:
    return count_detail_enrichment_queue(conn, priority_only=False)


def detail_enrichment_summary(conn: sqlite3.Connection) -> dict:
    source_where, source_params = _detail_base_filters(False, None)
    top_sources = conn.execute(
        f"""
        SELECT
            source_name,
            source_type,
            COUNT(1) AS total_count,
            SUM(
                CASE
                    WHEN source_category = 'news'
                     AND source_type != 'api'
                     AND (author IS NULL OR trim(author) = '')
                    THEN 1 ELSE 0
                END
            ) AS missing_author_count,
            SUM(
                CASE
                    WHEN (body_text IS NULL OR length(trim(body_text)) < ?)
                     AND (summary IS NULL OR length(trim(summary)) < ?)
                    THEN 1 ELSE 0
                END
            ) AS missing_body_count,
            MAX(COALESCE(published_at, collected_at)) AS latest_at
        FROM articles
        WHERE {" AND ".join(source_where)}
        GROUP BY source_name, source_type
        ORDER BY total_count DESC, missing_author_count DESC, source_name
        LIMIT 8
        """,
        (DETAIL_BODY_MIN_LENGTH, DETAIL_BODY_MIN_LENGTH, *source_params),
    ).fetchall()
    sources = []
    for row in top_sources:
        item = dict(row)
        item["recommendation"] = _detail_source_recommendation(item)
        sources.append(item)
    return {
        "priority_count": count_detail_enrichment_queue(conn),
        "backlog_count": count_detail_backlog(conn),
        "missing_author_count": count_detail_enrichment_queue(conn, priority_only=False, missing="author"),
        "missing_body_count": count_detail_enrichment_queue(conn, priority_only=False, missing="body"),
        "priority_missing_author_count": count_detail_enrichment_queue(conn, missing="author"),
        "priority_missing_body_count": count_detail_enrichment_queue(conn, missing="body"),
        "top_sources": sources,
    }


def _detail_source_recommendation(source: dict) -> str:
    source_type = source.get("source_type")
    author_count = int(source.get("missing_author_count") or 0)
    body_count = int(source.get("missing_body_count") or 0)
    if source_type == "rss" and author_count >= body_count:
        return "RSS에는 기자명이 빠지는 경우가 많아 원문 상세 수집을 우선 실행하세요."
    if source_type == "html" and author_count >= body_count:
        return "본문 상단·하단 byline 영역의 기자명 선택자를 우선 확인하세요."
    if body_count > author_count:
        return "본문 선택자 또는 기사 본문 JSON-LD 추출을 먼저 점검하세요."
    return "우선순위 기사부터 상세 보강을 실행하세요."


def repair_article_data(conn: sqlite3.Connection, limit: int = 500) -> dict:
    rows = conn.execute(
        """
        SELECT *
        FROM articles
        WHERE length(title) > 160
           OR quality_flags LIKE '%navigation_like_title%'
           OR title IN ({})
           OR {}
        ORDER BY collected_at DESC, id DESC
        LIMIT ?
        """.format(
            ",".join("?" for _ in NAVIGATION_TITLE_EXACT),
            " OR ".join("title LIKE ?" for _ in NAVIGATION_TITLE_WORDS),
        ),
        [*NAVIGATION_TITLE_EXACT, *[f"%{word}%" for word in NAVIGATION_TITLE_WORDS], limit],
    ).fetchall()
    repaired_titles = 0
    held_navigation = 0
    cleaned_authors = 0
    cleared_authors = 0
    inferred_authors = 0
    normalized_texts = 0
    normalized_urls = 0
    for row in rows:
        article = _article_row(row)
        title = normalize_space(article.get("title") or "")
        flags = set(_json_list(article.get("quality_flags")))
        if _is_navigation_like_title(title):
            flags.add("navigation_like_title")
            conn.execute(
                """
                UPDATE articles
                SET newsroom_status = 'hold',
                    verification_status = 'unchecked',
                    quality_score = 0,
                    quality_flags = ?,
                    desk_notes = COALESCE(desk_notes, '자동 보류: 메뉴/푸터성 항목으로 판단')
                WHERE id = ?
                """,
                (json.dumps(sorted(flags), ensure_ascii=False), article["id"]),
            )
            held_navigation += 1
            continue

        if len(title) > 160:
            new_title, new_summary = split_title_summary(title, article.get("summary"))
            new_title = normalize_space(new_title)
            if new_title and len(new_title) < len(title):
                summary = normalize_space(new_summary or article.get("summary") or "")
                conn.execute(
                    """
                    UPDATE articles
                    SET title = ?,
                        summary = COALESCE(NULLIF(?, ''), summary),
                        fingerprint = ?,
                        duplicate_group_id = ?
                    WHERE id = ?
                    """,
                    (
                        new_title,
                        summary,
                        article_fingerprint(new_title, article.get("source_name") or ""),
                        article_fingerprint(new_title, "global")[:16],
                        article["id"],
                    ),
                )
                repaired_titles += 1

    text_rows = conn.execute(
        """
        SELECT id, source_name, published_at, title, summary, body_text
        FROM articles
        WHERE title LIKE '%&%'
           OR title LIKE '%hellip;%'
           OR title LIKE '%ldquo;%'
           OR title LIKE '%rdquo;%'
           OR title LIKE '%lsquo;%'
           OR title LIKE '%rsquo;%'
           OR summary LIKE '%&%'
           OR summary LIKE '%hellip;%'
           OR summary LIKE '%ldquo;%'
           OR summary LIKE '%rdquo;%'
           OR summary LIKE '%lsquo;%'
           OR summary LIKE '%rsquo;%'
           OR body_text LIKE '%&%'
           OR body_text LIKE '%hellip;%'
           OR body_text LIKE '%ldquo;%'
           OR body_text LIKE '%rdquo;%'
           OR body_text LIKE '%lsquo;%'
           OR body_text LIKE '%rsquo;%'
        ORDER BY collected_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in text_rows:
        old_title = row["title"] or ""
        old_summary = row["summary"]
        old_body = row["body_text"]
        new_title = normalize_space(old_title)
        new_summary = normalize_space(old_summary) if old_summary else None
        new_body = normalize_space(old_body) if old_body else None
        if new_title == old_title and new_summary == old_summary and new_body == old_body:
            continue
        published_date = (row["published_at"] or "")[:10] or None
        conn.execute(
            """
            UPDATE articles
            SET title = ?,
                summary = ?,
                body_text = ?,
                fingerprint = ?,
                duplicate_group_id = ?
            WHERE id = ?
            """,
            (
                new_title,
                new_summary,
                new_body,
                article_fingerprint(new_title, row["source_name"] or "", published_date),
                article_fingerprint(new_title, "global", published_date)[:16],
                row["id"],
            ),
        )
        normalized_texts += 1

    url_rows = conn.execute(
        """
        SELECT id, url, canonical_url
        FROM articles
        WHERE canonical_url LIKE '%utm_%'
           OR canonical_url LIKE '%fbclid%'
           OR canonical_url LIKE '%gclid%'
           OR canonical_url LIKE '%plink=%'
           OR canonical_url LIKE '%cooper=%'
           OR canonical_url LIKE '%ref=rss%'
           OR canonical_url LIKE '%&ref=%'
        ORDER BY collected_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in url_rows:
        new_canonical = canonicalize_url(row["url"] or row["canonical_url"] or "")
        if not new_canonical or new_canonical == row["canonical_url"]:
            continue
        conn.execute("UPDATE articles SET canonical_url = ? WHERE id = ?", (new_canonical, row["id"]))
        normalized_urls += 1

    author_rows = conn.execute(
        """
        SELECT id, author
        FROM articles
        WHERE author IS NOT NULL
          AND trim(author) != ''
        ORDER BY collected_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in author_rows:
        raw_author = normalize_space(row["author"] or "")
        cleaned_author = clean_author_display(raw_author)
        if cleaned_author == raw_author:
            continue
        conn.execute("UPDATE articles SET author = ? WHERE id = ?", (cleaned_author, row["id"]))
        if cleaned_author:
            cleaned_authors += 1
        else:
            cleared_authors += 1

    missing_author_rows = conn.execute(
        """
        SELECT id, source_name, title, summary, body_text
        FROM articles
        WHERE source_category = 'news'
          AND source_type != 'api'
          AND (author IS NULL OR trim(author) = '' OR trim(author) = '기자명 없음')
          AND (
              title LIKE '%기자%'
              OR summary LIKE '%기자%'
              OR body_text LIKE '%기자%'
              OR title LIKE '%작성자%'
              OR summary LIKE '%작성자%'
              OR body_text LIKE '%작성자%'
              OR title LIKE '%담당기자%'
              OR summary LIKE '%담당기자%'
              OR body_text LIKE '%담당기자%'
              OR title LIKE '%취재기자%'
              OR summary LIKE '%취재기자%'
              OR body_text LIKE '%취재기자%'
              OR title LIKE '%기자명%'
              OR summary LIKE '%기자명%'
              OR body_text LIKE '%기자명%'
              OR summary LIKE '%@%'
              OR body_text LIKE '%@%'
          )
        ORDER BY collected_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in missing_author_rows:
        inferred = infer_reporter_from_article_text(
            row["source_name"],
            row["title"],
            row["summary"],
            row["body_text"],
        )
        if not inferred:
            continue
        conn.execute("UPDATE articles SET author = ? WHERE id = ?", (inferred, row["id"]))
        inferred_authors += 1
    conn.commit()
    return {
        "checked": len(rows),
        "repaired_titles": repaired_titles,
        "held_navigation": held_navigation,
        "cleaned_authors": cleaned_authors,
        "cleared_authors": cleared_authors,
        "inferred_authors": inferred_authors,
        "normalized_texts": normalized_texts,
        "normalized_urls": normalized_urls,
    }


def auto_triage_article(conn: sqlite3.Connection, article_id: int) -> dict | None:
    article = get_article(conn, article_id)
    if not article or article.get("newsroom_status") != "new":
        return article
    status, verification, note = _workflow_decision(article)
    if status == "new" and verification == article.get("verification_status"):
        return article
    conn.execute(
        """
        UPDATE articles
        SET newsroom_status = ?,
            verification_status = ?,
            desk_notes = COALESCE(desk_notes, ?),
            status_updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, verification, note, article_id),
    )
    return get_article(conn, article_id)


def auto_triage_articles(conn: sqlite3.Connection, limit: int = 500) -> dict:
    rows = conn.execute(
        """
        SELECT id
        FROM articles
        WHERE newsroom_status = 'new'
          AND quality_score >= 70
          AND importance_score >= 4.5
          AND (quality_flags IS NULL OR quality_flags NOT LIKE '%navigation_like_title%')
        ORDER BY importance_score DESC, COALESCE(published_at, collected_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    ready = 0
    review = 0
    for row in rows:
        before = get_article(conn, row["id"])
        after = auto_triage_article(conn, row["id"])
        if not before or not after:
            continue
        if before.get("newsroom_status") != after.get("newsroom_status") and after.get("newsroom_status") == "ready":
            ready += 1
        elif before.get("verification_status") != after.get("verification_status"):
            review += 1
    conn.commit()
    return {"checked": len(rows), "ready": ready, "needs_review": review}


def _workflow_decision(article: dict) -> tuple[str, str, str]:
    score = float(article.get("importance_score") or 0)
    quality = float(article.get("quality_score") or 0)
    keywords = set(_json_list(article.get("keywords")))
    has_ready_signal = bool(keywords & READY_KEYWORDS)
    if quality >= 70 and score >= 5.5 and has_ready_signal:
        return "ready", "needs_review", "자동 분류: 긴급성 높은 방송 후보"
    if quality >= 70 and score >= 4.5:
        return "new", "needs_review", "자동 분류: 데스크 확인 필요"
    return "new", article.get("verification_status") or "unchecked", ""


def _is_navigation_like_title(title: str) -> bool:
    normalized = normalize_space(title)
    return normalized in NAVIGATION_TITLE_EXACT or any(word in normalized for word in NAVIGATION_TITLE_WORDS)


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def update_article_checklist(conn: sqlite3.Connection, article_id: int, checklist: str) -> dict | None:
    if get_article(conn, article_id) is None:
        return None
    conn.execute(
        """
        UPDATE articles
        SET verification_checklist = ?,
            status_updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (checklist, article_id),
    )
    conn.commit()
    return get_article(conn, article_id)


def save_ai_assist(
    conn: sqlite3.Connection,
    article_id: int,
    ai_summary: str,
    check_points: str,
) -> dict | None:
    if get_article(conn, article_id) is None:
        return None
    conn.execute(
        """
        UPDATE articles
        SET ai_summary = ?,
            check_points = ?,
            status_updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (ai_summary, check_points, article_id),
    )
    conn.commit()
    return get_article(conn, article_id)


def start_crawl_run(conn: sqlite3.Connection, source_name: str) -> int:
    cursor = conn.execute(
        "INSERT INTO crawl_runs (source_name, status) VALUES (?, ?)",
        (source_name, "running"),
    )
    return int(cursor.lastrowid)


def finish_crawl_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    new_article_count: int = 0,
    fetched_article_count: int = 0,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE crawl_runs
        SET finished_at = CURRENT_TIMESTAMP,
            status = ?,
            fetched_article_count = ?,
            new_article_count = ?,
            error_message = ?
        WHERE id = ?
        """,
        (status, fetched_article_count, new_article_count, error_message, run_id),
    )


def _decorate_crawl_run(row: sqlite3.Row) -> dict:
    item = dict(row)
    item.update(classify_crawl_error(item.get("error_message")))
    status = item.get("status")
    seconds_since_started = item.get("seconds_since_started")
    has_later_run = bool(item.get("has_later_run"))
    if (
        status == "running"
        and seconds_since_started is not None
        and seconds_since_started > STALLED_CRAWL_RUN_SECONDS
        and has_later_run
    ):
        item["effective_status"] = "orphaned"
        item["status_label"] = "미종료 기록"
        item["status_hint"] = "이후 같은 출처의 실행 기록이 있어 현재 장애는 아닙니다. 과거 실행이 비정상 종료된 흔적입니다."
    elif status == "running" and seconds_since_started is not None and seconds_since_started > STALLED_CRAWL_RUN_SECONDS:
        item["effective_status"] = "stalled"
        item["status_label"] = "멈춤 의심"
        item["status_hint"] = "수집 시작 후 15분 넘게 완료 기록이 없습니다. 이전 실행이 중간에 끊겼을 가능성이 큽니다."
    elif status == "running":
        item["effective_status"] = "running"
        item["status_label"] = "실행중"
        item["status_hint"] = "현재 수집 중입니다."
    elif status == "failed":
        item["effective_status"] = "failed"
        item["status_label"] = "실패"
        item["status_hint"] = item.get("failure_hint") or "수집 실행이 실패했습니다."
    elif status == "success":
        item["effective_status"] = "success"
        item["status_label"] = "성공"
        item["status_hint"] = "정상 완료됐습니다."
    elif status == "canceled":
        item["effective_status"] = "canceled"
        item["status_label"] = "취소"
        item["status_hint"] = "사용자 요청으로 중단됐습니다."
    else:
        item["effective_status"] = status or "unknown"
        item["status_label"] = status or "알 수 없음"
        item["status_hint"] = "상태를 확인해야 합니다."
    return item


def list_crawl_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            cr.*,
            CAST((julianday('now') - julianday(cr.started_at)) * 86400 AS INTEGER) AS seconds_since_started,
            EXISTS (
                SELECT 1
                FROM crawl_runs later
                WHERE later.source_name = cr.source_name
                  AND (
                      later.started_at > cr.started_at
                      OR (later.started_at = cr.started_at AND later.id > cr.id)
                  )
            ) AS has_later_run
        FROM crawl_runs cr
        ORDER BY cr.started_at DESC, cr.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_decorate_crawl_run(row) for row in rows]


def search_crawl_runs(conn: sqlite3.Connection, q: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    where = []
    params: list[object] = []
    if q:
        where.append("(cr.source_name LIKE ? OR cr.error_message LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    if status == "stalled":
        where.append(
            """
            cr.status = 'running'
            AND cr.started_at < datetime('now', '-15 minutes')
            AND NOT EXISTS (
                SELECT 1
                FROM crawl_runs later
                WHERE later.source_name = cr.source_name
                  AND (
                      later.started_at > cr.started_at
                      OR (later.started_at = cr.started_at AND later.id > cr.id)
                  )
            )
            """
        )
    elif status == "running":
        where.append("cr.status = 'running' AND cr.started_at >= datetime('now', '-15 minutes')")
    elif status:
        where.append("cr.status = ?")
        params.append(status)
    sql = """
        SELECT
            cr.*,
            CAST((julianday('now') - julianday(cr.started_at)) * 86400 AS INTEGER) AS seconds_since_started,
            EXISTS (
                SELECT 1
                FROM crawl_runs later
                WHERE later.source_name = cr.source_name
                  AND (
                      later.started_at > cr.started_at
                      OR (later.started_at = cr.started_at AND later.id > cr.id)
                  )
            ) AS has_later_run
        FROM crawl_runs cr
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY cr.started_at DESC, cr.id DESC LIMIT ?"
    params.append(limit)
    return [_decorate_crawl_run(row) for row in conn.execute(sql, params).fetchall()]


def set_app_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_state (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """,
        (key, value),
    )


def get_app_state(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM app_state WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def scheduler_status(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
            cr.*,
            CAST((julianday('now') - julianday(cr.started_at)) * 86400 AS INTEGER) AS seconds_since_started,
            EXISTS (
                SELECT 1
                FROM crawl_runs later
                WHERE later.source_name = cr.source_name
                  AND (
                      later.started_at > cr.started_at
                      OR (later.started_at = cr.started_at AND later.id > cr.id)
                  )
            ) AS has_later_run
        FROM crawl_runs cr
        ORDER BY cr.started_at DESC, cr.id DESC
        LIMIT 1
        """
    ).fetchone()
    running_count = conn.execute(
        """
        SELECT COUNT(1)
        FROM crawl_runs cr
        WHERE cr.status = 'running'
          AND cr.started_at >= datetime('now', '-15 minutes')
          AND NOT EXISTS (
              SELECT 1
              FROM crawl_runs later
              WHERE later.source_name = cr.source_name
                AND (
                    later.started_at > cr.started_at
                    OR (later.started_at = cr.started_at AND later.id > cr.id)
                )
          )
        """
    ).fetchone()[0]
    stalled_count = conn.execute(
        """
        SELECT COUNT(1)
        FROM crawl_runs cr
        WHERE cr.status = 'running'
          AND cr.started_at < datetime('now', '-15 minutes')
          AND NOT EXISTS (
              SELECT 1
              FROM crawl_runs later
              WHERE later.source_name = cr.source_name
                AND (
                    later.started_at > cr.started_at
                    OR (later.started_at = cr.started_at AND later.id > cr.id)
                )
          )
        """
    ).fetchone()[0]
    failed_recent = conn.execute(
        """
        SELECT COUNT(1)
        FROM crawl_runs
        WHERE status = 'failed'
          AND started_at >= datetime('now', '-24 hours')
        """
    ).fetchone()[0]
    return {
        "last_run": _decorate_crawl_run(row) if row else None,
        "running_count": running_count,
        "stalled_count": stalled_count,
        "failed_24h": failed_recent,
        "heartbeat": get_app_state(conn, "auto_crawl_heartbeat"),
        "progress": {
            "status": (get_app_state(conn, "crawl_progress_status") or {}).get("value") or "idle",
            "current_source": (get_app_state(conn, "crawl_progress_current") or {}).get("value") or "",
            "index": int((get_app_state(conn, "crawl_progress_index") or {}).get("value") or 0),
            "total": int((get_app_state(conn, "crawl_progress_total") or {}).get("value") or 0),
        },
    }


def _source_stale_thresholds(item: dict) -> tuple[int, int]:
    interval = int(item.get("crawl_interval_seconds") or 300)
    return max(interval * 3, 900), max(interval * 6, 1800)


def _is_stale_after_success(item: dict) -> bool:
    if not item.get("enabled") or item.get("last_status") != "success":
        return False
    seconds_since_last_run = item.get("seconds_since_last_run")
    if seconds_since_last_run is None:
        return False
    warning_seconds, _ = _source_stale_thresholds(item)
    return int(seconds_since_last_run) > warning_seconds


def _apply_global_collection_context(items: list[dict]) -> list[dict]:
    enabled = [item for item in items if item.get("enabled")]
    stale_successes = [item for item in enabled if _is_stale_after_success(item)]
    has_active_run = any(item.get("zero_new_status") in {"running", "stalled"} for item in enabled)
    global_idle = (
        len(enabled) >= GLOBAL_IDLE_MIN_ENABLED_SOURCES
        and not has_active_run
        and len(stale_successes) / max(len(enabled), 1) >= GLOBAL_IDLE_STALE_RATIO
    )
    if not global_idle:
        return items

    idle_seconds = min(int(item.get("seconds_since_last_run") or 0) for item in stale_successes)
    for item in enabled:
        item["global_collection_state"] = "idle"
        item["global_collection_idle"] = True
        item["global_idle_seconds"] = idle_seconds
        if item in stale_successes and item.get("zero_new_status") in {"stale", "stale_watch"}:
            item["risk_level"] = "normal"
            item["zero_new_status"] = "global_idle"
            item["zero_new_label"] = "전체 수집 공백"
            item["risk_score"] = 6
            item["health_priority_label"] = "전체 대기"
    return items


def list_source_quality(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            s.id,
            s.name,
            s.source_type,
            s.source_category,
            s.enabled,
            s.crawl_interval_seconds,
            (SELECT COUNT(1) FROM articles a WHERE a.source_name = s.name) AS article_count,
            (SELECT COUNT(1) FROM crawl_runs cr WHERE cr.source_name = s.name) AS total_runs,
            (SELECT COUNT(1) FROM crawl_runs cr WHERE cr.source_name = s.name AND cr.status = 'success') AS success_runs,
            (SELECT COUNT(1) FROM crawl_runs cr WHERE cr.source_name = s.name AND cr.status = 'failed') AS failed_runs,
            (SELECT COUNT(1) FROM crawl_runs cr WHERE cr.source_name = s.name AND cr.status = 'canceled') AS canceled_runs,
            (
                SELECT cr.status
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC, cr.id DESC
                LIMIT 1
            ) AS last_status,
            (
                SELECT cr.started_at
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC, cr.id DESC
                LIMIT 1
            ) AS last_started_at,
            (
                SELECT cr.error_message
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC, cr.id DESC
                LIMIT 1
            ) AS last_error_message,
            (
                SELECT cr.new_article_count
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC, cr.id DESC
                LIMIT 1
            ) AS last_new_article_count,
            (
                SELECT cr.fetched_article_count
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC, cr.id DESC
                LIMIT 1
            ) AS last_fetched_article_count,
            (
                SELECT CAST((julianday('now') - julianday(cr.started_at)) * 86400 AS INTEGER)
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC, cr.id DESC
                LIMIT 1
            ) AS seconds_since_last_run,
            (
                SELECT COUNT(1)
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                  AND cr.status = 'success'
                  AND cr.new_article_count = 0
                  AND cr.id IN (
                      SELECT recent.id
                      FROM crawl_runs recent
                      WHERE recent.source_name = s.name
                        AND recent.status = 'success'
                      ORDER BY recent.started_at DESC, recent.id DESC
                      LIMIT 3
                  )
            ) AS zero_new_streak
            ,
            (
                SELECT COUNT(1)
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                  AND cr.status = 'success'
                  AND COALESCE(cr.fetched_article_count, 0) = 0
                  AND cr.id IN (
                      SELECT recent.id
                      FROM crawl_runs recent
                      WHERE recent.source_name = s.name
                        AND recent.status = 'success'
                      ORDER BY recent.started_at DESC, recent.id DESC
                      LIMIT 3
                  )
            ) AS empty_fetch_streak,
            (
                SELECT COUNT(1)
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                  AND cr.status = 'success'
                  AND cr.new_article_count = 0
                  AND COALESCE(cr.fetched_article_count, 0) > 0
                  AND cr.id IN (
                      SELECT recent.id
                      FROM crawl_runs recent
                      WHERE recent.source_name = s.name
                        AND recent.status = 'success'
                      ORDER BY recent.started_at DESC, recent.id DESC
                      LIMIT 3
                  )
            ) AS duplicate_only_streak,
            (
                SELECT COUNT(1)
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                  AND cr.status = 'failed'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM crawl_runs later
                      WHERE later.source_name = s.name
                        AND (
                            later.started_at > cr.started_at
                            OR (later.started_at = cr.started_at AND later.id > cr.id)
                        )
                        AND later.status != 'failed'
                  )
            ) AS failure_streak
        FROM sources s
        ORDER BY s.enabled DESC, s.source_category, s.name
        """
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item.update(classify_crawl_error(item.get("last_error_message")))
        success_runs = item.get("success_runs") or 0
        failed_runs = item.get("failed_runs") or 0
        measured_runs = success_runs + failed_runs
        item["measured_runs"] = measured_runs
        item["success_rate"] = round(success_runs / measured_runs * 100, 1) if measured_runs else None
        zero_new_streak = item.get("zero_new_streak") or 0
        empty_fetch_streak = item.get("empty_fetch_streak") or 0
        duplicate_only_streak = item.get("duplicate_only_streak") or 0
        failure_streak = item.get("failure_streak") or 0
        article_count = item.get("article_count") or 0
        interval = int(item.get("crawl_interval_seconds") or 300)
        seconds_since_last_run = item.get("seconds_since_last_run")
        stale_warning_seconds, stale_danger_seconds = _source_stale_thresholds(item)
        stalled_seconds = max(interval * 3, STALLED_CRAWL_RUN_SECONDS)
        if not item.get("enabled"):
            item["risk_level"] = "normal"
            item["zero_new_status"] = "disabled"
            item["zero_new_label"] = "비활성"
            item["risk_score"] = 0
        elif item.get("last_status") == "failed" and failure_streak >= 2:
            item["risk_level"] = "danger"
            item["zero_new_status"] = "repeated_failed"
            item["zero_new_label"] = "연속 실패"
            item["risk_score"] = 92 + min(int(failure_streak), 8)
        elif item.get("last_status") == "failed":
            item["risk_level"] = "warning"
            item["zero_new_status"] = "transient_failed"
            item["zero_new_label"] = "일시 실패"
            item["risk_score"] = 45
        elif (
            item.get("last_status") == "running"
            and seconds_since_last_run is not None
            and seconds_since_last_run > stalled_seconds
        ):
            item["risk_level"] = "danger"
            item["zero_new_status"] = "stalled"
            item["zero_new_label"] = "수집 멈춤"
            item["risk_score"] = 96
        elif item.get("last_status") == "running":
            item["risk_level"] = "normal"
            item["zero_new_status"] = "running"
            item["zero_new_label"] = "수집 중"
            item["risk_score"] = 3
        elif item.get("last_started_at") is None:
            item["risk_level"] = "danger"
            item["zero_new_status"] = "never_crawled"
            item["zero_new_label"] = "미수집"
            item["risk_score"] = 95
        elif seconds_since_last_run is not None and seconds_since_last_run > stale_danger_seconds:
            item["risk_level"] = "danger"
            item["zero_new_status"] = "stale"
            item["zero_new_label"] = "수집 지연"
            item["risk_score"] = 90
        elif empty_fetch_streak >= 3 and article_count == 0:
            item["risk_level"] = "danger"
            item["zero_new_status"] = "selector_check"
            item["zero_new_label"] = "선택자 점검 필요"
            item["risk_score"] = 80 + empty_fetch_streak
        elif seconds_since_last_run is not None and seconds_since_last_run > stale_warning_seconds:
            item["risk_level"] = "warning"
            item["zero_new_status"] = "stale_watch"
            item["zero_new_label"] = "수집 지연"
            item["risk_score"] = 65
        elif empty_fetch_streak >= 3:
            item["risk_level"] = "warning"
            item["zero_new_status"] = "empty_fetch_watch"
            item["zero_new_label"] = "빈 응답 반복"
            item["risk_score"] = 55 + empty_fetch_streak
        elif measured_runs >= 3 and item.get("success_rate") is not None and item["success_rate"] < 70:
            item["risk_level"] = "warning"
            item["zero_new_status"] = "low_success_rate"
            item["zero_new_label"] = "성공률 낮음"
            item["risk_score"] = 50
        elif int(item.get("last_new_article_count") or 0) > 0:
            item["risk_level"] = "normal"
            item["zero_new_status"] = "ok"
            item["zero_new_label"] = "정상"
            item["risk_score"] = 0
        elif duplicate_only_streak >= 3:
            item["risk_level"] = "normal"
            item["zero_new_status"] = "duplicate_only"
            item["zero_new_label"] = "중복만 확인"
            item["risk_score"] = 8
        elif zero_new_streak:
            item["risk_level"] = "normal"
            item["zero_new_status"] = "normal_zero"
            item["zero_new_label"] = "새 기사 없음"
            item["risk_score"] = 5 + zero_new_streak
        else:
            item["risk_level"] = "normal"
            item["zero_new_status"] = "ok"
            item["zero_new_label"] = "정상"
            item["risk_score"] = 0
        item["health_priority_label"] = (
            "즉시 점검"
            if item["risk_level"] == "danger"
            else "관찰"
            if item["risk_level"] == "warning"
            else "정상"
        )
        items.append(item)
    items = _apply_global_collection_context(items)
    return sorted(
        items,
        key=lambda item: (
            not bool(item.get("enabled")),
            -int(item.get("risk_score") or 0),
            item.get("success_rate") if item.get("success_rate") is not None else 101,
            item.get("source_category") or "",
            item.get("name") or "",
        ),
    )


def source_failure_diagnostics(conn: sqlite3.Connection, hours: int = 24) -> dict:
    rows = conn.execute(
        """
        SELECT
            cr.source_name,
            cr.started_at,
            cr.error_message,
            s.source_type,
            s.source_category,
            s.enabled
        FROM crawl_runs cr
        LEFT JOIN sources s ON s.name = cr.source_name
        WHERE cr.status = 'failed'
          AND cr.started_at >= datetime('now', ?)
        ORDER BY cr.started_at DESC, cr.id DESC
        LIMIT 200
        """,
        (f"-{max(1, int(hours))} hours",),
    ).fetchall()
    cause_map: dict[str, dict] = {}
    recent_failures = []
    for row in rows:
        item = dict(row)
        item.update(classify_crawl_error(item.get("error_message")))
        cause = item.get("failure_cause") or "unknown"
        if cause not in cause_map:
            cause_map[cause] = {
                "cause": cause,
                "label": item.get("failure_cause_label") or "오류 원인 확인 필요",
                "group": item.get("failure_group") or "unknown",
                "count": 0,
                "retryable": bool(item.get("failure_retryable")),
                "severity": item.get("failure_severity") or "warning",
                "hint": item.get("failure_hint"),
                "action": item.get("failure_action"),
            }
        cause_map[cause]["count"] += 1
        recent_failures.append(
            {
                "source_name": item.get("source_name"),
                "started_at": item.get("started_at"),
                "failure_cause": cause,
                "failure_cause_label": item.get("failure_cause_label"),
                "failure_group": item.get("failure_group"),
                "failure_retryable": item.get("failure_retryable"),
                "failure_action": item.get("failure_action"),
                "error_message": item.get("error_message"),
            }
        )

    current_risks = [
        source
        for source in list_source_quality(conn)
        if source.get("enabled") and source.get("risk_level") in {"danger", "warning"}
    ]
    action_items = []
    for source in current_risks[:8]:
        action_items.append(
            {
                "source_name": source.get("name"),
                "risk_level": source.get("risk_level"),
                "status": source.get("zero_new_status"),
                "label": source.get("zero_new_label"),
                "cause": source.get("failure_cause"),
                "cause_label": source.get("failure_cause_label"),
                "retryable": bool(source.get("failure_retryable")),
                "action": _source_failure_action(source),
            }
        )

    cause_counts = sorted(cause_map.values(), key=lambda item: (-int(item["count"]), item["label"]))
    return {
        "window_hours": max(1, int(hours)),
        "failed_run_count": len(rows),
        "current_risk_count": len(current_risks),
        "retryable_count": sum(1 for item in recent_failures if item.get("failure_retryable")),
        "cause_counts": cause_counts,
        "recent_failures": recent_failures[:8],
        "action_items": action_items,
    }


def _source_failure_action(source: dict) -> str:
    if source.get("global_collection_idle"):
        return "개별 출처보다 자동 수집기 실행 상태를 먼저 확인하세요."
    if source.get("zero_new_status") == "stalled":
        return "멈춘 실행 기록을 확인하고 서버 재시작 또는 새 수집을 실행하세요."
    if source.get("failure_action"):
        return source["failure_action"]
    if source.get("zero_new_status") in {"selector_check", "empty_fetch_watch"}:
        return "목록 선택자와 기사 링크 규칙을 다시 확인하세요."
    if source.get("zero_new_status") in {"stale", "stale_watch", "never_crawled"}:
        return "자동 수집 주기와 마지막 실행 시간을 확인한 뒤 수동 수집을 실행하세요."
    return "최근 실행 로그를 확인하고 동일 원인이 반복되는지 점검하세요."


def newsroom_stats(conn: sqlite3.Connection) -> dict:
    category_rows = conn.execute(
        """
        SELECT source_category, COUNT(1) AS count
        FROM articles
        GROUP BY source_category
        ORDER BY count DESC
        """
    ).fetchall()
    return {
        "article_count": conn.execute("SELECT COUNT(1) FROM articles").fetchone()[0],
        "source_count": conn.execute("SELECT COUNT(1) FROM sources WHERE enabled = 1").fetchone()[0],
        "alert_count": count_articles(conn, has_alert=True),
        "ready_count": conn.execute("SELECT COUNT(1) FROM articles WHERE newsroom_status = 'ready'").fetchone()[0],
        "with_body_count": conn.execute("SELECT COUNT(1) FROM articles WHERE body_text IS NOT NULL").fetchone()[0],
        "needs_work_count": count_articles(conn, needs_work=True),
        "needs_review_count": conn.execute(
            "SELECT COUNT(1) FROM articles WHERE verification_status IN ('needs_review', 'caution')"
        ).fetchone()[0],
        "missing_author_count": conn.execute(
            """
            SELECT COUNT(1)
            FROM articles
            WHERE source_category = 'news'
              AND source_type != 'api'
              AND (author IS NULL OR trim(author) = '')
            """
        ).fetchone()[0],
        "low_quality_count": conn.execute("SELECT COUNT(1) FROM articles WHERE quality_score < 70").fetchone()[0],
        "detail_queue_count": count_detail_enrichment_queue(conn),
        "detail_backlog_count": count_detail_backlog(conn),
        "fts_enabled": article_fts_ready(conn),
        "last_collected_at": conn.execute("SELECT MAX(collected_at) FROM articles").fetchone()[0],
        "last_crawl_status": conn.execute(
            "SELECT status FROM crawl_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()[0]
        if conn.execute("SELECT COUNT(1) FROM crawl_runs").fetchone()[0]
        else None,
        "categories": [dict(row) for row in category_rows],
        "regions": list_region_counts(conn),
    }
