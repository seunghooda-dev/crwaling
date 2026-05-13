import json
import sqlite3

from app.models import Article, Source


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
) -> tuple[list[str], list[object]]:
    where = []
    params: list[object] = []
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
    if assignee:
        where.append("assignee = ?")
        params.append(assignee)
    return where, params


def list_articles(
    conn: sqlite3.Connection,
    limit: int = 50,
    source_name: str | None = None,
    q: str | None = None,
    min_importance: float | None = None,
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
) -> list[dict]:
    where, params = _article_filters(
        source_name=source_name,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
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
    sql += " ORDER BY importance_score DESC, collected_at DESC LIMIT ?"
    params.append(limit)
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
) -> int:
    where, params = _article_filters(
        source_name=source_name,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
    )
    sql = "SELECT COUNT(1) FROM articles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, params).fetchone()[0])


def list_priority_articles(conn: sqlite3.Connection, limit: int = 50, source_category: str | None = None) -> list[dict]:
    if source_category:
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
            WHERE importance_score > 0 AND source_category = ?
            ORDER BY importance_score DESC, collected_at DESC
            LIMIT ?
            """,
            (source_category, limit),
        ).fetchall()
    else:
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
            WHERE importance_score > 0
            ORDER BY importance_score DESC, collected_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_alert_articles(
    conn: sqlite3.Connection,
    limit: int = 50,
    threshold: float = 4.0,
    source_category: str | None = None,
) -> list[dict]:
    category_sql = "AND source_category = ?" if source_category else ""
    params: list[object] = [threshold]
    if source_category:
        params.append(source_category)
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
        WHERE (importance_score >= ?
           OR verification_status = 'needs_review')
        {category_sql}
        ORDER BY importance_score DESC, collected_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


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
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE crawl_runs
        SET finished_at = CURRENT_TIMESTAMP,
            status = ?,
            new_article_count = ?,
            error_message = ?
        WHERE id = ?
        """,
        (status, new_article_count, error_message, run_id),
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
            ) AS last_new_article_count
        FROM sources s
        ORDER BY s.enabled DESC, s.source_category, s.name
        """
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        total_runs = item.get("total_runs") or 0
        success_runs = item.get("success_runs") or 0
        item["success_rate"] = round(success_runs / total_runs * 100, 1) if total_runs else None
        items.append(item)
    return items


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
    }
