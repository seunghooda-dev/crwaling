from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SourceType(StrEnum):
    rss = "rss"
    html = "html"
    api = "api"
    monitor = "monitor"


@dataclass
class Source:
    name: str
    source_type: SourceType
    url: str
    source_category: str = "news"
    enabled: bool = True
    crawl_interval_seconds: int = 300
    timeout_seconds: float | None = None
    max_retries: int | None = None


@dataclass
class Article:
    source_name: str
    source_type: str
    title: str
    url: str
    fingerprint: str
    source_category: str = "news"
    canonical_url: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    body_text: str | None = None
    summary: str | None = None
    category: str | None = None
    keywords: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    duplicate_group_id: str | None = None
    importance_score: float = 0
    verification_status: str = "unchecked"
    raw_html_path: str | None = None
