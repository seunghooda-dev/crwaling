import sqlite3
import logging

from app.crawlers.html import HtmlCrawler
from app.crawlers.rss import RssCrawler
from app.models import Source, SourceType
from app.repository import finish_crawl_run, get_article_by_url, insert_article, list_sources, start_crawl_run
from app.services.alert_service import record_alert_if_needed
from app.services.scoring import apply_newsroom_scoring


class CrawlService:
    def __init__(self) -> None:
        self.crawlers = {
            SourceType.rss: RssCrawler(),
            SourceType.html: HtmlCrawler(),
        }

    def crawl_enabled_sources(self, conn: sqlite3.Connection) -> dict[str, int]:
        logger = logging.getLogger("crawler")
        results: dict[str, int] = {}
        for row in list_sources(conn):
            if not row["enabled"]:
                continue
            source = Source(
                name=row["name"],
                source_type=SourceType(row["source_type"]),
                url=row["url"],
                source_category=row["source_category"],
                enabled=bool(row["enabled"]),
                crawl_interval_seconds=row["crawl_interval_seconds"],
            )
            crawler = self.crawlers.get(source.source_type)
            if crawler is None:
                results[source.name] = 0
                continue
            run_id = start_crawl_run(conn, source.name)
            count = 0
            try:
                logger.info("crawl start source=%s", source.name)
                for article in crawler.crawl(source):
                    article = apply_newsroom_scoring(article)
                    if insert_article(conn, article):
                        count += 1
                        stored = get_article_by_url(conn, article.url)
                        if stored:
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
