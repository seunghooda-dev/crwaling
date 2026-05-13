import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.app_name = os.getenv("NEWSROOM_APP_NAME", "Broadcast News Crawling Assistant")
        self.database_path = Path(os.getenv("NEWSROOM_DATABASE_PATH", "data/newsroom.sqlite3"))
        self.raw_html_dir = Path(os.getenv("NEWSROOM_RAW_HTML_DIR", "data/raw_html"))
        self.log_dir = Path(os.getenv("NEWSROOM_LOG_DIR", "logs"))
        self.backup_dir = Path(os.getenv("NEWSROOM_BACKUP_DIR", "backups"))
        self.keyword_config_path = Path(os.getenv("NEWSROOM_KEYWORD_CONFIG_PATH", "config/keywords.json"))
        self.archive_dir = Path(os.getenv("NEWSROOM_ARCHIVE_DIR", "data/archive"))
        self.request_timeout_seconds = float(os.getenv("NEWSROOM_REQUEST_TIMEOUT_SECONDS", "15"))
        self.sqlite_busy_timeout_ms = int(os.getenv("NEWSROOM_SQLITE_BUSY_TIMEOUT_MS", "10000"))
        self.max_raw_html_bytes = int(os.getenv("NEWSROOM_MAX_RAW_HTML_BYTES", "1500000"))
        self.retention_days = int(os.getenv("NEWSROOM_RETENTION_DAYS", "90"))
        self.alert_retention_days = int(os.getenv("NEWSROOM_ALERT_RETENTION_DAYS", "14"))
        self.max_backup_count = int(os.getenv("NEWSROOM_MAX_BACKUP_COUNT", "20"))
        self.user_agent = os.getenv(
            "NEWSROOM_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "Chrome/124.0 Safari/537.36 NewsroomCrawler/0.1",
        )


settings = Settings()
