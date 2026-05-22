import sqlite3
from collections.abc import Iterator
from pathlib import Path

from app.config import settings
from app.logging_config import configure_logging


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    url TEXT NOT NULL,
    source_category TEXT NOT NULL DEFAULT 'news',
    enabled INTEGER NOT NULL DEFAULT 1,
    crawl_interval_seconds INTEGER NOT NULL DEFAULT 300,
    timeout_seconds REAL,
    max_retries INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_category TEXT NOT NULL DEFAULT 'news',
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    canonical_url TEXT,
    author TEXT,
    published_at TEXT,
    collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    body_text TEXT,
    summary TEXT,
    category TEXT,
    keywords TEXT,
    image_urls TEXT,
    video_urls TEXT,
    fingerprint TEXT NOT NULL,
    duplicate_group_id TEXT,
    importance_score REAL NOT NULL DEFAULT 0,
    verification_status TEXT NOT NULL DEFAULT 'unchecked',
    newsroom_status TEXT NOT NULL DEFAULT 'new',
    desk_notes TEXT,
    assignee TEXT,
    status_updated_at TEXT,
    ai_summary TEXT,
    check_points TEXT,
    region_tags TEXT,
    quality_score REAL NOT NULL DEFAULT 0,
    quality_flags TEXT,
    verification_checklist TEXT,
    raw_html_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_source_name ON articles(source_name);
CREATE INDEX IF NOT EXISTS idx_articles_fingerprint ON articles(fingerprint);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    status TEXT NOT NULL,
    fetched_article_count INTEGER NOT NULL DEFAULT 0,
    new_article_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_source_name ON crawl_runs(source_name);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_started_at ON crawl_runs(started_at);

CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    source_name TEXT NOT NULL,
    importance_score REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    UNIQUE(article_id, reason)
);

CREATE INDEX IF NOT EXISTS idx_alert_events_created_at ON alert_events(created_at);
CREATE INDEX IF NOT EXISTS idx_alert_events_acknowledged ON alert_events(acknowledged);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contact_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    contact_target TEXT NOT NULL,
    response_note TEXT NOT NULL,
    next_check_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contact_logs_article_id ON contact_logs(article_id);

CREATE TABLE IF NOT EXISTS notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    channel TEXT NOT NULL,
    destination TEXT,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_notification_events_article_id ON notification_events(article_id);
CREATE INDEX IF NOT EXISTS idx_notification_events_created_at ON notification_events(created_at);
"""


ARTICLE_MIGRATIONS = {
    "source_category": "ALTER TABLE articles ADD COLUMN source_category TEXT NOT NULL DEFAULT 'news'",
    "newsroom_status": "ALTER TABLE articles ADD COLUMN newsroom_status TEXT NOT NULL DEFAULT 'new'",
    "desk_notes": "ALTER TABLE articles ADD COLUMN desk_notes TEXT",
    "assignee": "ALTER TABLE articles ADD COLUMN assignee TEXT",
    "status_updated_at": "ALTER TABLE articles ADD COLUMN status_updated_at TEXT",
    "ai_summary": "ALTER TABLE articles ADD COLUMN ai_summary TEXT",
    "check_points": "ALTER TABLE articles ADD COLUMN check_points TEXT",
    "region_tags": "ALTER TABLE articles ADD COLUMN region_tags TEXT",
    "quality_score": "ALTER TABLE articles ADD COLUMN quality_score REAL NOT NULL DEFAULT 0",
    "quality_flags": "ALTER TABLE articles ADD COLUMN quality_flags TEXT",
    "verification_checklist": "ALTER TABLE articles ADD COLUMN verification_checklist TEXT",
}


SOURCE_MIGRATIONS = {
    "source_category": "ALTER TABLE sources ADD COLUMN source_category TEXT NOT NULL DEFAULT 'news'",
    "timeout_seconds": "ALTER TABLE sources ADD COLUMN timeout_seconds REAL",
    "max_retries": "ALTER TABLE sources ADD COLUMN max_retries INTEGER",
}


CRAWL_RUN_MIGRATIONS = {
    "fetched_article_count": "ALTER TABLE crawl_runs ADD COLUMN fetched_article_count INTEGER NOT NULL DEFAULT 0",
}


POST_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS idx_articles_duplicate_group_id ON articles(duplicate_group_id);
CREATE INDEX IF NOT EXISTS idx_articles_newsroom_status ON articles(newsroom_status);
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    source_name TEXT NOT NULL,
    importance_score REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    UNIQUE(article_id, reason)
);
CREATE INDEX IF NOT EXISTS idx_alert_events_created_at ON alert_events(created_at);
CREATE INDEX IF NOT EXISTS idx_alert_events_acknowledged ON alert_events(acknowledged);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS contact_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    contact_target TEXT NOT NULL,
    response_note TEXT NOT NULL,
    next_check_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_contact_logs_article_id ON contact_logs(article_id);
CREATE TABLE IF NOT EXISTS notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    channel TEXT NOT NULL,
    destination TEXT,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_notification_events_article_id ON notification_events(article_id);
CREATE INDEX IF NOT EXISTS idx_notification_events_created_at ON notification_events(created_at);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title,
    summary,
    body_text,
    keywords,
    content='articles',
    content_rowid='id',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS articles_fts_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, summary, body_text, keywords)
    VALUES (new.id, new.title, new.summary, new.body_text, new.keywords);
END;
CREATE TRIGGER IF NOT EXISTS articles_fts_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, body_text, keywords)
    VALUES('delete', old.id, old.title, old.summary, old.body_text, old.keywords);
END;
CREATE TRIGGER IF NOT EXISTS articles_fts_au AFTER UPDATE OF title, summary, body_text, keywords ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, body_text, keywords)
    VALUES('delete', old.id, old.title, old.summary, old.body_text, old.keywords);
    INSERT INTO articles_fts(rowid, title, summary, body_text, keywords)
    VALUES (new.id, new.title, new.summary, new.body_text, new.keywords);
END;
INSERT INTO articles_fts(articles_fts) VALUES('rebuild');
"""


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    configure_logging()
    ensure_parent(settings.database_path)
    conn = sqlite3.connect(
        settings.database_path,
        timeout=settings.sqlite_busy_timeout_ms / 1000,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    settings.raw_html_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(articles)").fetchall()
        }
        for column, statement in ARTICLE_MIGRATIONS.items():
            if column not in existing_columns:
                conn.execute(statement)
        existing_source_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sources)").fetchall()
        }
        for column, statement in SOURCE_MIGRATIONS.items():
            if column not in existing_source_columns:
                conn.execute(statement)
        existing_crawl_run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(crawl_runs)").fetchall()
        }
        for column, statement in CRAWL_RUN_MIGRATIONS.items():
            if column not in existing_crawl_run_columns:
                conn.execute(statement)
        conn.execute(
            """
            UPDATE articles
            SET source_category = COALESCE(
                (SELECT source_category FROM sources WHERE sources.name = articles.source_name),
                source_category
            )
            """
        )
        conn.executescript(POST_MIGRATION_SQL)
        try:
            conn.executescript(FTS_SQL)
        except sqlite3.OperationalError:
            # Some embedded SQLite builds omit FTS5. The app keeps the LIKE fallback in that case.
            pass
        conn.commit()


def db_session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
