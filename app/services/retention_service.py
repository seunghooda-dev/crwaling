import csv
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings


def prune_old_data(conn, days: int | None = None) -> dict:
    days = days or settings.retention_days
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = settings.archive_dir / f"articles-before-{cutoff[:10]}.csv"

    rows = conn.execute(
        "SELECT * FROM articles WHERE collected_at < ? ORDER BY collected_at",
        (cutoff,),
    ).fetchall()
    if rows:
        with archive_path.open("w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
    conn.execute("DELETE FROM alert_events WHERE created_at < ?", (cutoff,))
    deleted_articles = conn.execute("DELETE FROM articles WHERE collected_at < ?", (cutoff,)).rowcount
    conn.commit()
    return {"cutoff": cutoff, "archived": len(rows), "deleted_articles": deleted_articles, "archive_path": str(archive_path)}
