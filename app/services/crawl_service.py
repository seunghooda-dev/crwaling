import sqlite3
import logging

from app.crawlers.html import HtmlCrawler
from app.crawlers.rss import RssCrawler
from app.models import Source, SourceType
from app.repository import (
    finish_crawl_run,
    get_article_by_url,
    insert_article,
    list_sources,
    start_crawl_run,
    set_app_state,
    update_article_snapshot_path,
)
from app.services.alert_service import record_alert_if_needed
from app.services.quality_service import enrich_article_quality
from app.services.scoring import apply_newsroom_scoring
from app.services.snapshot_service import save_article_snapshot


class CrawlService:
    def __init__(self) -> None:
        self.crawlers = {
            SourceType.rss: RssCrawler(),
            SourceType.html: HtmlCrawler(),
        }

    def crawl_enabled_sources(
        self,
        conn: sqlite3.Connection,
        source_name: str | None = None,
        source_category: str | None = None,
    ) -> dict[str, int]:
        logger = logging.getLogger("crawler")
        results: dict[str, int] = {}
        for row in list_sources(conn):
            if not row["enabled"]:
                continue
            if source_name and row["name"] != source_name:
                continue
            if source_category and row["source_category"] != source_category:
                continue
            source = Source(
                name=row["name"],
                source_type=SourceType(row["source_type"]),
                url=row["url"],
                source_category=row["source_category"],
                enabled=bool(row["enabled"]),
                crawl_interval_seconds=row["crawl_interval_seconds"],
                timeout_seconds=row["timeout_seconds"],
                max_retries=row["max_retries"],
            )
            crawler = self.crawlers.get(source.source_type)
            if crawler is None:
                results[source.name] = 0
                continue
            run_id = start_crawl_run(conn, source.name)
            count = 0
            try:
                set_app_state(conn, "auto_crawl_heartbeat", source.name)
                conn.commit()
                logger.info("crawl start source=%s", source.name)
                for article in crawler.crawl(source):
                    article = enrich_article_quality(article)
                    article = apply_newsroom_scoring(article)
                    if insert_article(conn, article):
                        count += 1
                        stored = get_article_by_url(conn, article.url)
                        if stored:
                            snapshot_path = save_article_snapshot(stored, article)
                            if snapshot_path:
                                update_article_snapshot_path(conn, stored["id"], str(snapshot_path))
                            record_alert_if_needed(conn, stored)
                finish_crawl_run(conn, run_id, "success", count)
                conn.commit()
                results[source.name] = count
                logger.info("crawl success source=%s new=%s", source.name, count)
            except Exception as exc:
                finish_crawl_run(conn, run_id, "failed", count, str(exc))
                conn.commit()
                results[source.name] = -1
                logger.exception("crawl failed source=%s", source.name)
        return results
