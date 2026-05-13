import argparse
from time import sleep

from app.database import connect, init_db
from app.repository import disable_sources_not_in, upsert_source
from app.source_loader import load_sources


def seed_sources() -> None:
    init_db()
    sources = load_sources()
    with connect() as conn:
        for source in sources:
            upsert_source(conn, source)
        disable_sources_not_in(conn, [source.name for source in sources])
        conn.execute(
            """
            UPDATE articles
            SET source_category = COALESCE(
                (SELECT source_category FROM sources WHERE sources.name = articles.source_name),
                source_category
            )
            """
        )
        conn.commit()


def crawl_once() -> None:
    from app.services.crawl_service import CrawlService

    init_db()
    with connect() as conn:
        results = CrawlService().crawl_enabled_sources(conn)
    for source_name, count in results.items():
        print(f"{source_name}: {count} new articles")


def rescore() -> None:
    from app.services.rescore_service import rescore_articles

    init_db()
    with connect() as conn:
        count = rescore_articles(conn)
    print(f"Rescored {count} articles.")


def detail_crawl(limit: int) -> None:
    from app.services.detail_service import enrich_missing_details

    init_db()
    with connect() as conn:
        count = enrich_missing_details(conn, limit=limit)
    print(f"Enriched {count} articles.")


def rebuild_clusters(limit: int | None) -> None:
    from app.services.cluster_service import rebuild_clusters as rebuild

    init_db()
    with connect() as conn:
        count = rebuild(conn, limit=limit)
    print(f"Rebuilt clusters for {count} articles.")


def backup() -> None:
    from app.services.backup_service import backup_database

    init_db()
    path = backup_database()
    print(f"Backup created: {path}")


def validate_config() -> None:
    from app.services.validation_service import validate_all

    errors = validate_all()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        raise SystemExit(1)
    print("Config OK.")


def prune(days: int) -> None:
    from app.services.retention_service import prune_old_data

    init_db()
    with connect() as conn:
        result = prune_old_data(conn, days=days)
    print(result)


def maintenance(limit: int) -> None:
    print("Maintenance started.")
    seed_sources()
    crawl_once()
    rescore()
    detail_crawl(limit)
    rebuild_clusters(limit)
    backup()
    print("Maintenance complete.")


def auto_crawl(interval_seconds: int) -> None:
    print(f"Auto crawl started. interval={interval_seconds}s")
    while True:
        seed_sources()
        crawl_once()
        sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Broadcast news crawling assistant")
    parser.add_argument(
        "command",
        choices=[
            "init-db",
            "seed-sources",
            "crawl-once",
            "rescore",
            "auto-crawl",
            "detail-crawl",
            "rebuild-clusters",
            "backup",
            "maintenance",
            "validate-config",
            "prune",
        ],
    )
    parser.add_argument("--interval", type=int, default=300, help="auto-crawl interval in seconds")
    parser.add_argument("--limit", type=int, default=50, help="number of articles to process")
    parser.add_argument("--days", type=int, default=90, help="retention window in days")
    args = parser.parse_args()

    if args.command == "init-db":
        init_db()
        seed_sources()
        print("Database initialized.")
    elif args.command == "seed-sources":
        seed_sources()
        print("Default sources saved.")
    elif args.command == "crawl-once":
        seed_sources()
        crawl_once()
    elif args.command == "rescore":
        rescore()
    elif args.command == "auto-crawl":
        auto_crawl(args.interval)
    elif args.command == "detail-crawl":
        detail_crawl(args.limit)
    elif args.command == "rebuild-clusters":
        rebuild_clusters(args.limit)
    elif args.command == "backup":
        backup()
    elif args.command == "maintenance":
        maintenance(args.limit)
    elif args.command == "validate-config":
        validate_config()
    elif args.command == "prune":
        prune(args.days)


if __name__ == "__main__":
    main()
