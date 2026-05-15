import sqlite3
import logging

from app.crawlers.html import HtmlCrawler
from app.crawlers.rss import RssCrawler
from app.crawlers.safe_korea import SafeKoreaDisasterMessageCrawler
from app.models import Source, SourceType
from app.repository import (
    finish_crawl_run,
    get_article_by_url,
    get_app_state,
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


MANUAL_CRAWL_CANCEL_KEY = "manual_crawl_cancel_requested"
CRAWL_PROGRESS_CURRENT_KEY = "crawl_progress_current"
CRAWL_PROGRESS_INDEX_KEY = "crawl_progress_index"
CRAWL_PROGRESS_TOTAL_KEY = "crawl_progress_total"
CRAWL_PROGRESS_STATUS_KEY = "crawl_progress_status"


class CrawlService:
    def __init__(self) -> None:
        self.crawlers = {
            SourceType.rss: RssCrawler(),
            SourceType.html: HtmlCrawler(),
            SourceType.api: SafeKoreaDisasterMessageCrawler(),
        }

    def crawl_enabled_sources(
        self,
        conn: sqlite3.Connection,
        source_name: str | None = None,
        source_category: str | None = None,
    ) -> dict[str, int]:
        return self.crawl_enabled_sources_with_articles(conn, source_name, source_category)["results"]

    def crawl_enabled_sources_with_articles(
        self,
        conn: sqlite3.Connection,
        source_name: str | None = None,
        source_category: str | None = None,
        cancel_key: str | None = None,
        reset_cancel: bool = False,
    ) -> dict:
        logger = logging.getLogger("crawler")
        results: dict[str, int] = {}
        fetched_results: dict[str, int] = {}
        new_articles: list[dict] = []
        canceled = False
        if cancel_key and reset_cancel:
            set_app_state(conn, cancel_key, "0")
            conn.commit()
        source_rows = [
            row
            for row in list_sources(conn)
            if row["enabled"]
            and (not source_name or row["name"] == source_name)
            and (not source_category or row["source_category"] == source_category)
        ]
        if cancel_key:
            set_app_state(conn, CRAWL_PROGRESS_TOTAL_KEY, str(len(source_rows)))
            set_app_state(conn, CRAWL_PROGRESS_INDEX_KEY, "0")
            set_app_state(conn, CRAWL_PROGRESS_CURRENT_KEY, "")
            set_app_state(conn, CRAWL_PROGRESS_STATUS_KEY, "running")
            conn.commit()
        for index, row in enumerate(source_rows, start=1):
            if cancel_key and _is_canceled(conn, cancel_key):
                canceled = True
                break
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
                fetched_results[source.name] = 0
                continue
            run_id = start_crawl_run(conn, source.name)
            count = 0
            fetched_count = 0
            try:
                if cancel_key:
                    set_app_state(conn, CRAWL_PROGRESS_INDEX_KEY, str(index))
                    set_app_state(conn, CRAWL_PROGRESS_CURRENT_KEY, source.name)
                    set_app_state(conn, CRAWL_PROGRESS_STATUS_KEY, "running")
                set_app_state(conn, "auto_crawl_heartbeat", source.name)
                conn.commit()
                logger.info("crawl start source=%s", source.name)
                articles = crawler.crawl(source)
                fetched_count = len(articles)
                fetched_results[source.name] = fetched_count
                for article in articles:
                    if cancel_key and _is_canceled(conn, cancel_key):
                        canceled = True
                        break
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
                            new_articles.append(dict(stored))
                finish_crawl_run(conn, run_id, "canceled" if canceled else "success", count, fetched_count)
                conn.commit()
                results[source.name] = count
                logger.info("crawl %s source=%s new=%s", "canceled" if canceled else "success", source.name, count)
                if canceled:
                    break
            except Exception as exc:
                finish_crawl_run(conn, run_id, "failed", count, fetched_count, str(exc))
                conn.commit()
                results[source.name] = -1
                fetched_results[source.name] = 0
                logger.exception("crawl failed source=%s", source.name)
        if cancel_key and canceled:
            set_app_state(conn, cancel_key, "0")
            conn.commit()
        if cancel_key:
            set_app_state(conn, CRAWL_PROGRESS_STATUS_KEY, "canceled" if canceled else "idle")
            set_app_state(conn, CRAWL_PROGRESS_CURRENT_KEY, "")
            conn.commit()
        return {
            "results": results,
            "fetched_results": fetched_results,
            "new_articles": new_articles,
            "canceled": canceled,
        }


def _is_canceled(conn: sqlite3.Connection, key: str) -> bool:
    state = get_app_state(conn, key)
    return bool(state and state.get("value") == "1")
