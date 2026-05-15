import json
import sqlite3
from datetime import datetime, timedelta

from app.models import Article, Source
from app.services.region_service import region_group_aliases, region_group_label, region_group_options


def upsert_source(conn: sqlite3.Connection, source: Source) -> None:
    conn.execute(
        """
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
            article.canonical_url,
            article.author,
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
    return dict(row) if row else None


def list_sources(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def _article_filters(
    source_name: str | None = None,
    q: str | None = None,
    min_importance: float | None = None,
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = None,
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
) -> tuple[list[str], list[object]]:
    where = []
    params: list[object] = []
    local_article_time = _article_local_time_sql()
    if source_name:
        where.append("source_name = ?")
        params.append(source_name)
    if q:
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
    return where, params


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
    }.get(sort or "importance", f"importance_score DESC, {article_time} DESC, id DESC")


def list_articles(
    conn: sqlite3.Connection,
    limit: int = 50,
    offset: int = 0,
    source_name: str | None = None,
    q: str | None = None,
    min_importance: float | None = None,
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = None,
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
    sort: str | None = None,
) -> list[dict]:
    where, params = _article_filters(
        source_name=source_name,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
        collected_within_days=collected_within_days,
        collected_from=collected_from,
        collected_to=collected_to,
        region_group=region_group,
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
    return [dict(row) for row in rows]


def count_articles(
    conn: sqlite3.Connection,
    source_name: str | None = None,
    q: str | None = None,
    min_importance: float | None = None,
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = None,
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
) -> int:
    where, params = _article_filters(
        source_name=source_name,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
        collected_within_days=collected_within_days,
        collected_from=collected_from,
        collected_to=collected_to,
        region_group=region_group,
    )
    sql = "SELECT COUNT(1) FROM articles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, params).fetchone()[0])


def list_priority_articles(
    conn: sqlite3.Connection,
    limit: int = 50,
    source_category: str | None = None,
    region_group: str | None = None,
) -> list[dict]:
    where = ["importance_score > 0"]
    params: list[object] = []
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
    return [dict(row) for row in rows]


def list_alert_articles(
    conn: sqlite3.Connection,
    limit: int = 50,
    threshold: float = 4.0,
    source_category: str | None = None,
    region_group: str | None = None,
) -> list[dict]:
    params: list[object] = [threshold]
    filters = []
    if source_category:
        filters.append("source_category = ?")
        params.append(source_category)
    aliases = region_group_aliases(region_group)
    if aliases:
        filters.append("(" + " OR ".join("region_tags LIKE ?" for _ in aliases) + ")")
        params.extend([f"%{alias}%" for alias in aliases])
    params.append(limit)
    filter_sql = f"AND {' AND '.join(filters)}" if filters else ""
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
        WHERE (importance_score >= ?
           OR verification_status = 'needs_review')
        {filter_sql}
        ORDER BY importance_score DESC, cluster_source_count DESC, {_article_time_sql()} DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


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
    return [dict(row) for row in rows]


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
    return dict(row) if row else None


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
    return [dict(row) for row in rows]


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


def list_crawl_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM crawl_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def search_crawl_runs(conn: sqlite3.Connection, q: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    where = []
    params: list[object] = []
    if q:
        where.append("(source_name LIKE ? OR error_message LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM crawl_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


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
        SELECT started_at, finished_at, status, source_name
        FROM crawl_runs
        ORDER BY started_at DESC
        LIMIT 1
        """
    ).fetchone()
    running_count = conn.execute("SELECT COUNT(1) FROM crawl_runs WHERE status = 'running'").fetchone()[0]
    failed_recent = conn.execute(
        """
        SELECT COUNT(1)
        FROM crawl_runs
        WHERE status = 'failed'
          AND started_at >= datetime('now', '-24 hours')
        """
    ).fetchone()[0]
    return {
        "last_run": dict(row) if row else None,
        "running_count": running_count,
        "failed_24h": failed_recent,
        "heartbeat": get_app_state(conn, "auto_crawl_heartbeat"),
        "progress": {
            "status": (get_app_state(conn, "crawl_progress_status") or {}).get("value") or "idle",
            "current_source": (get_app_state(conn, "crawl_progress_current") or {}).get("value") or "",
            "index": int((get_app_state(conn, "crawl_progress_index") or {}).get("value") or 0),
            "total": int((get_app_state(conn, "crawl_progress_total") or {}).get("value") or 0),
        },
    }


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
                SELECT cr.error_message
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC
                LIMIT 1
            ) AS last_error_message,
            (
                SELECT cr.new_article_count
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC
                LIMIT 1
            ) AS last_new_article_count,
            (
                SELECT cr.fetched_article_count
                FROM crawl_runs cr
                WHERE cr.source_name = s.name
                ORDER BY cr.started_at DESC
                LIMIT 1
            ) AS last_fetched_article_count,
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
                  AND cr.new_article_count = 0
                  AND cr.id IN (
                      SELECT recent.id
                      FROM crawl_runs recent
                      WHERE recent.source_name = s.name
                        AND recent.status = 'success'
                      ORDER BY recent.started_at DESC
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
                      ORDER BY recent.started_at DESC
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
                      ORDER BY recent.started_at DESC
                      LIMIT 3
                  )
            ) AS duplicate_only_streak
        FROM sources s
        ORDER BY s.enabled DESC, s.source_category, s.name
        """
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        success_runs = item.get("success_runs") or 0
        failed_runs = item.get("failed_runs") or 0
        measured_runs = success_runs + failed_runs
        item["measured_runs"] = measured_runs
        item["success_rate"] = round(success_runs / measured_runs * 100, 1) if measured_runs else None
        zero_new_streak = item.get("zero_new_streak") or 0
        empty_fetch_streak = item.get("empty_fetch_streak") or 0
        duplicate_only_streak = item.get("duplicate_only_streak") or 0
        article_count = item.get("article_count") or 0
        interval = int(item.get("crawl_interval_seconds") or 300)
        seconds_since_last_run = item.get("seconds_since_last_run")
        stale_warning_seconds = max(interval * 3, 900)
        stale_danger_seconds = max(interval * 6, 1800)
        if not item.get("enabled"):
            item["risk_level"] = "normal"
            item["zero_new_status"] = "disabled"
            item["zero_new_label"] = "비활성"
            item["risk_score"] = 0
        elif item.get("last_status") == "failed":
            item["risk_level"] = "danger"
            item["zero_new_status"] = "failed"
            item["zero_new_label"] = "최근 실패"
            item["risk_score"] = 100
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
        items.append(item)
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
        "alert_count": conn.execute("SELECT COUNT(1) FROM alert_events WHERE acknowledged = 0").fetchone()[0],
        "ready_count": conn.execute("SELECT COUNT(1) FROM articles WHERE newsroom_status = 'ready'").fetchone()[0],
        "with_body_count": conn.execute("SELECT COUNT(1) FROM articles WHERE body_text IS NOT NULL").fetchone()[0],
        "last_collected_at": conn.execute("SELECT MAX(collected_at) FROM articles").fetchone()[0],
        "last_crawl_status": conn.execute(
            "SELECT status FROM crawl_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()[0]
        if conn.execute("SELECT COUNT(1) FROM crawl_runs").fetchone()[0]
        else None,
        "categories": [dict(row) for row in category_rows],
        "regions": list_region_counts(conn),
    }
