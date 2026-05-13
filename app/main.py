from pathlib import Path
import logging
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from app.database import db_session, init_db
from app.config import settings
from app.repository import (
    get_article,
    acknowledge_all_alert_events,
    acknowledge_alert_event,
    count_articles,
    list_alert_articles,
    list_alert_events,
    list_cluster_articles,
    list_articles,
    list_crawl_runs,
    list_issue_clusters,
    list_priority_articles,
    list_sources,
    list_source_quality,
    newsroom_stats,
    save_ai_assist,
    search_crawl_runs,
    scheduler_status,
    update_article_checklist,
    update_article_workflow,
)
from app.services.ai_assist import build_ai_assist
from app.services.backup_service import backup_database, list_backups, restore_database
from app.services.cluster_service import rebuild_clusters
from app.services.detail_service import enrich_article_details
from app.services.export_service import articles_to_csv, articles_to_cuesheet
from app.services.retention_service import prune_old_alerts, prune_old_data
from app.services.validation_service import validate_all

app = FastAPI(title="Broadcast News Crawling Assistant")
logger = logging.getLogger("app")


@app.middleware("http")
async def request_logging(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request failed method=%s path=%s", request.method, request.url.path)
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "request method=%s path=%s status=%s elapsed_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.1f}"
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled exception method=%s path=%s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


class ArticleWorkflowUpdate(BaseModel):
    newsroom_status: str | None = None
    verification_status: str | None = None
    desk_notes: str | None = None
    assignee: str | None = None


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    crawl_interval_seconds: int | None = None


class ChecklistUpdate(BaseModel):
    verification_checklist: dict[str, bool]


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/sources")
def sources(conn=Depends(db_session)) -> list[dict]:
    return list_sources(conn)


@app.get("/sources/quality")
def source_quality(conn=Depends(db_session)) -> list[dict]:
    return list_source_quality(conn)


@app.patch("/sources/{source_id}")
def update_source(source_id: int, payload: SourceUpdate, conn=Depends(db_session)) -> dict:
    current = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if current is None:
        raise HTTPException(status_code=404, detail="Source not found")
    conn.execute(
        """
        UPDATE sources
        SET enabled = COALESCE(?, enabled),
            crawl_interval_seconds = COALESCE(?, crawl_interval_seconds)
        WHERE id = ?
        """,
        (
            None if payload.enabled is None else int(payload.enabled),
            payload.crawl_interval_seconds,
            source_id,
        ),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())


@app.get("/admin/validate")
def validate_settings() -> dict:
    errors = validate_all()
    return {"ok": not errors, "errors": errors}


@app.get("/stats")
def stats(conn=Depends(db_session)) -> dict:
    return newsroom_stats(conn)


@app.get("/scheduler/status")
def crawl_scheduler_status(conn=Depends(db_session)) -> dict:
    return scheduler_status(conn)


@app.get("/articles")
def articles(
    limit: int = Query(default=50, ge=1, le=200),
    source_name: str | None = None,
    q: str | None = None,
    min_importance: float | None = Query(default=None, ge=0),
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    conn=Depends(db_session),
    ) -> list[dict]:
    return list_articles(
        conn,
        limit=limit,
        source_name=source_name,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
    )


@app.get("/articles/count")
def articles_count(
    source_name: str | None = None,
    q: str | None = None,
    min_importance: float | None = Query(default=None, ge=0),
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    conn=Depends(db_session),
) -> dict:
    return {
        "count": count_articles(
            conn,
            source_name=source_name,
            q=q,
            min_importance=min_importance,
            newsroom_status=newsroom_status,
            source_category=source_category,
            assignee=assignee,
        )
    }


@app.get("/priority")
def priority_articles(
    limit: int = Query(default=50, ge=1, le=200),
    source_category: str | None = None,
    conn=Depends(db_session),
) -> list[dict]:
    return list_priority_articles(conn, limit=limit, source_category=source_category)


@app.get("/alerts")
def alerts(
    limit: int = Query(default=50, ge=1, le=200),
    threshold: float = Query(default=4.0, ge=0),
    source_category: str | None = None,
    conn=Depends(db_session),
) -> list[dict]:
    return list_alert_articles(conn, limit=limit, threshold=threshold, source_category=source_category)


@app.get("/alert-events")
def alert_events(
    limit: int = Query(default=50, ge=1, le=200),
    acknowledged: bool | None = None,
    conn=Depends(db_session),
) -> list[dict]:
    return list_alert_events(conn, limit=limit, acknowledged=acknowledged)


@app.post("/alert-events/{alert_id}/ack")
def acknowledge_alert(alert_id: int, conn=Depends(db_session)) -> dict:
    item = acknowledge_alert_event(conn, alert_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Alert event not found")
    return item


@app.post("/alert-events/ack-all")
def acknowledge_all_alerts(conn=Depends(db_session)) -> dict:
    return {"acknowledged": acknowledge_all_alert_events(conn)}


@app.get("/clusters")
def clusters(limit: int = Query(default=50, ge=1, le=200), conn=Depends(db_session)) -> list[dict]:
    return list_issue_clusters(conn, limit=limit)


@app.get("/clusters/{duplicate_group_id}/articles")
def cluster_articles(
    duplicate_group_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    conn=Depends(db_session),
) -> list[dict]:
    return list_cluster_articles(conn, duplicate_group_id=duplicate_group_id, limit=limit)


@app.get("/articles/{article_id}")
def article(article_id: int, conn=Depends(db_session)) -> dict:
    item = get_article(conn, article_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return item


@app.patch("/articles/{article_id}/workflow")
def update_workflow(article_id: int, payload: ArticleWorkflowUpdate, conn=Depends(db_session)) -> dict:
    item = update_article_workflow(
        conn,
        article_id,
        newsroom_status=payload.newsroom_status,
        verification_status=payload.verification_status,
        desk_notes=payload.desk_notes,
        assignee=payload.assignee,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return item


@app.patch("/articles/{article_id}/checklist")
def update_checklist(article_id: int, payload: ChecklistUpdate, conn=Depends(db_session)) -> dict:
    import json

    item = update_article_checklist(conn, article_id, json.dumps(payload.verification_checklist, ensure_ascii=False))
    if item is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return item


@app.post("/articles/{article_id}/ai-assist")
def ai_assist(article_id: int, conn=Depends(db_session)) -> dict:
    item = get_article(conn, article_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Article not found")
    assist = build_ai_assist(item)
    updated = save_ai_assist(conn, article_id, assist["ai_summary"], assist["check_points"])
    if updated is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return updated


@app.post("/articles/{article_id}/enrich")
def enrich_article(article_id: int, conn=Depends(db_session)) -> dict:
    ok = enrich_article_details(conn, article_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Article not found")
    conn.commit()
    item = get_article(conn, article_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return item


@app.post("/clusters/rebuild")
def rebuild_issue_clusters(limit: int = Query(default=500, ge=1, le=5000), conn=Depends(db_session)) -> dict:
    count = rebuild_clusters(conn, limit=limit)
    return {"updated": count}


@app.get("/crawl-runs")
def crawl_runs(limit: int = Query(default=50, ge=1, le=200), conn=Depends(db_session)) -> list[dict]:
    return list_crawl_runs(conn, limit=limit)


@app.get("/crawl-runs/search")
def crawl_run_search(
    q: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    conn=Depends(db_session),
) -> list[dict]:
    return search_crawl_runs(conn, q=q, status=status, limit=limit)


@app.get("/exports/articles.csv")
def export_articles_csv(
    limit: int = Query(default=200, ge=1, le=1000),
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    conn=Depends(db_session),
) -> Response:
    articles = list_articles(
        conn,
        limit=limit,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
    )
    return Response(
        content=articles_to_csv(articles),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=newsroom_articles.csv"},
    )


@app.get("/exports/cuesheet.txt")
def export_cuesheet(
    limit: int = Query(default=50, ge=1, le=300),
    newsroom_status: str | None = "ready",
    source_category: str | None = None,
    assignee: str | None = None,
    conn=Depends(db_session),
) -> PlainTextResponse:
    articles = list_articles(
        conn,
        limit=limit,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
    )
    return PlainTextResponse(
        content=articles_to_cuesheet(articles),
        headers={"Content-Disposition": "attachment; filename=newsroom_cuesheet.txt"},
    )


@app.post("/maintenance/backup")
def backup_now() -> dict:
    path = backup_database()
    return {"backup_path": str(path)}


@app.get("/maintenance/backups")
def backups() -> list[dict]:
    return list_backups()


@app.get("/operations/overview")
def operations_overview() -> dict:
    backups = list_backups()
    latest_backup = backups[0] if backups else None
    database_size = settings.database_path.stat().st_size if settings.database_path.exists() else 0
    return {
        "database_path": str(settings.database_path),
        "database_size": database_size,
        "backup_count": len(backups),
        "latest_backup": latest_backup,
        "max_backup_count": settings.max_backup_count,
    }


@app.post("/maintenance/restore/{backup_name}")
def restore_backup(backup_name: str) -> dict:
    try:
        safety_backup = restore_database(backup_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup not found")
    return {"restored": backup_name, "safety_backup": str(safety_backup)}


@app.post("/maintenance/prune")
def prune_data(days: int = Query(default=90, ge=1, le=3650), conn=Depends(db_session)) -> dict:
    return prune_old_data(conn, days=days)


@app.post("/maintenance/prune-alerts")
def prune_alerts(days: int = Query(default=14, ge=1, le=3650), conn=Depends(db_session)) -> dict:
    return prune_old_alerts(conn, days=days)
