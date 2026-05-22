from pathlib import Path
import logging
import threading
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from app.database import db_session, init_db
from app.config import settings
from app.keyword_loader import load_keyword_groups
from app.repository import (
    auto_triage_articles,
    count_detail_backlog,
    count_detail_enrichment_queue,
    detail_enrichment_summary,
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
    list_detail_enrichment_queue,
    insert_notification_event,
    list_notification_events,
    newsroom_stats,
    repair_article_data,
    save_ai_assist,
    search_crawl_runs,
    scheduler_status,
    set_app_state,
    source_failure_diagnostics,
    update_article_checklist,
    update_article_workflow,
)
from app.services.ai_assist import build_ai_assist
from app.services.backup_service import backup_database, list_backups, restore_database
from app.services.cluster_service import rebuild_clusters
from app.services.crawl_service import MANUAL_CRAWL_CANCEL_KEY, CrawlService
from app.services.detail_service import enrich_article_details, enrich_missing_detail_report
from app.services.export_service import articles_to_csv, articles_to_cuesheet
from app.services.notification_service import (
    NotificationConfigError,
    NotificationSendError,
    notification_status,
    notify_search_matches,
    send_article_to_telegram,
    send_telegram_test_message,
)
from app.services.newsroom_advisor import (
    build_reporter_dashboard,
    build_command_center,
    compare_cluster_for_reporter,
    false_risk_dashboard,
    reporter_advice_for_article,
    suggested_ready_queue,
    suggested_alert_rules,
    today_budget,
)
from app.services.retention_service import prune_old_alerts, prune_old_data
from app.services.validation_service import validate_all

app = FastAPI(title="Broadcast News Crawling Assistant")
logger = logging.getLogger("app")
MANUAL_CRAWL_LOCK = threading.Lock()


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


class KeywordGroupsUpdate(BaseModel):
    groups: dict[str, list[str]]


class CrawlRefreshRequest(BaseModel):
    source_name: str | None = None
    source_names: list[str] = Field(default_factory=list)
    source_category: str | None = None
    q: str | None = None


class ContactLogCreate(BaseModel):
    contact_target: str
    response_note: str
    next_check_at: str | None = None


class TelegramArticleSendRequest(BaseModel):
    note: str | None = None


def _source_filter_values(source_name: str | None = None, source_names: list[str] | None = None) -> list[str]:
    values: list[str] = []
    if source_name:
        values.append(source_name)
    values.extend(source_names or [])
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


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


@app.get("/sources/failure-diagnostics")
def failure_diagnostics(hours: int = Query(default=24, ge=1, le=168), conn=Depends(db_session)) -> dict:
    return source_failure_diagnostics(conn, hours=hours)


@app.get("/coverage/overview")
def coverage_overview(conn=Depends(db_session)) -> dict:
    quality = list_source_quality(conn)
    enabled = [source for source in quality if source.get("enabled")]
    danger = [source for source in enabled if source.get("risk_level") == "danger"]
    warning = [source for source in enabled if source.get("risk_level") == "warning"]
    running = [source for source in enabled if source.get("zero_new_status") == "running"]
    healthy = [source for source in enabled if source.get("risk_level") == "normal"]
    recently_checked = [
        source
        for source in enabled
        if source.get("seconds_since_last_run") is not None
        and int(source.get("seconds_since_last_run") or 0)
        <= max(int(source.get("crawl_interval_seconds") or 300) * 6, 1800)
    ]
    recent_success = [source for source in recently_checked if source.get("last_status") == "success"]
    last_checked_at = max((source.get("last_started_at") or "" for source in enabled), default="")
    stale = [
        source
        for source in enabled
        if source.get("zero_new_status") in {"stale", "stale_watch", "never_crawled"}
    ]
    global_idle = [source for source in enabled if source.get("global_collection_idle")]
    stalled = [source for source in enabled if source.get("zero_new_status") == "stalled"]
    empty = [
        source
        for source in enabled
        if source.get("zero_new_status") in {"selector_check", "empty_fetch_watch"}
    ]
    failed = [
        source
        for source in enabled
        if source.get("zero_new_status") in {"repeated_failed", "transient_failed"}
    ]
    network_failed = [
        source
        for source in failed
        if source.get("failure_cause") in {"connection_reset", "timeout", "dns", "ssl"}
    ]
    connected_count = len(recent_success)
    connection_ok = (
        bool(enabled)
        and connected_count == len(enabled)
        and not danger
        and not failed
        and not stalled
        and not network_failed
    )
    risks = sorted(
        danger + warning,
        key=lambda source: (-int(source.get("risk_score") or 0), source.get("name") or ""),
    )[:8]
    return {
        "ok": not danger,
        "enabled_source_count": len(enabled),
        "healthy_count": len(healthy),
        "danger_count": len(danger),
        "warning_count": len(warning),
        "running_count": len(running),
        "failed_count": len(failed),
        "network_failed_count": len(network_failed),
        "stalled_count": len(stalled),
        "stale_count": len(stale),
        "empty_fetch_count": len(empty),
        "recent_checked_count": len(recently_checked),
        "recent_success_count": len(recent_success),
        "connected_count": connected_count,
        "last_checked_at": last_checked_at,
        "connection_ok": connection_ok,
        "global_collection_idle": bool(global_idle),
        "global_idle_seconds": min((int(source.get("global_idle_seconds") or 0) for source in global_idle), default=0),
        "risks": risks,
    }


@app.get("/details/summary")
def detail_summary(conn=Depends(db_session)) -> dict:
    return detail_enrichment_summary(conn)


@app.get("/details/queue")
def detail_queue(
    limit: int = Query(default=8, ge=1, le=100),
    priority_only: bool = True,
    missing: str | None = Query(default=None, pattern="^(all|author|body)$"),
    source_name: str | None = None,
    conn=Depends(db_session),
) -> list[dict]:
    return list_detail_enrichment_queue(
        conn,
        limit=limit,
        priority_only=priority_only,
        missing=missing,
        source_name=source_name,
    )


@app.post("/details/enrich-batch")
def enrich_detail_batch(
    limit: int = Query(default=5, ge=1, le=30),
    priority_only: bool = True,
    missing: str | None = Query(default=None, pattern="^(all|author|body)$"),
    source_name: str | None = None,
    conn=Depends(db_session),
) -> dict:
    return enrich_missing_detail_report(
        conn,
        limit=limit,
        priority_only=priority_only,
        missing=missing,
        source_name=source_name,
    )


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


@app.post("/admin/repair-data")
def repair_data(limit: int = Query(default=500, ge=1, le=5000), conn=Depends(db_session)) -> dict:
    repair = repair_article_data(conn, limit=limit)
    triage = auto_triage_articles(conn, limit=limit)
    return {
        "repair": repair,
        "triage": triage,
        "detail_queue_count": count_detail_enrichment_queue(conn),
        "detail_backlog_count": count_detail_backlog(conn),
    }


@app.get("/admin/keywords")
def keywords() -> dict[str, list[str]]:
    return load_keyword_groups(settings.keyword_config_path)


@app.put("/admin/keywords")
def update_keywords(payload: KeywordGroupsUpdate) -> dict[str, list[str]]:
    import json

    groups = {
        str(group).strip(): sorted({str(keyword).strip() for keyword in keywords if str(keyword).strip()})
        for group, keywords in payload.groups.items()
        if str(group).strip()
    }
    settings.keyword_config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.keyword_config_path.write_text(json.dumps(groups, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return groups


@app.get("/stats")
def stats(conn=Depends(db_session)) -> dict:
    return newsroom_stats(conn)


@app.get("/reporter/dashboard")
def reporter_dashboard(limit: int = Query(default=12, ge=1, le=50), conn=Depends(db_session)) -> dict:
    return build_reporter_dashboard(conn, limit=limit)


@app.get("/intelligence/command-center")
def intelligence_command_center(conn=Depends(db_session)) -> dict:
    return build_command_center(conn)


@app.get("/reporter/articles/{article_id}/advice")
def reporter_article_advice(article_id: int, conn=Depends(db_session)) -> dict:
    item = get_article(conn, article_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return reporter_advice_for_article(item)


@app.get("/reporter/clusters/{duplicate_group_id}/compare")
def reporter_cluster_compare(duplicate_group_id: str, conn=Depends(db_session)) -> dict:
    return compare_cluster_for_reporter(conn, duplicate_group_id)


@app.get("/reporter/alert-rules")
def reporter_alert_rules(conn=Depends(db_session)) -> list[dict]:
    return suggested_alert_rules(conn)


@app.get("/reporter/budget")
def reporter_budget(conn=Depends(db_session)) -> list[dict]:
    return today_budget(conn)


@app.get("/reporter/ready-queue")
def reporter_ready_queue(limit: int = Query(default=10, ge=1, le=50), conn=Depends(db_session)) -> list[dict]:
    return suggested_ready_queue(conn, limit=limit)


@app.get("/reporter/false-risk")
def reporter_false_risk(limit: int = Query(default=10, ge=1, le=50), conn=Depends(db_session)) -> list[dict]:
    return false_risk_dashboard(conn, limit=limit)


@app.get("/articles/{article_id}/contact-logs")
def article_contact_logs(article_id: int, conn=Depends(db_session)) -> list[dict]:
    if get_article(conn, article_id) is None:
        raise HTTPException(status_code=404, detail="Article not found")
    rows = conn.execute(
        "SELECT * FROM contact_logs WHERE article_id = ? ORDER BY created_at DESC LIMIT 20",
        (article_id,),
    ).fetchall()
    return [dict(row) for row in rows]


@app.get("/notifications/status")
def notifications_status() -> dict:
    return notification_status()


@app.post("/notifications/telegram/test")
def telegram_test_message(conn=Depends(db_session)) -> dict:
    try:
        result = send_telegram_test_message()
    except NotificationConfigError as exc:
        insert_notification_event(
            conn,
            article_id=None,
            channel="telegram",
            destination=None,
            status="config_error",
            message=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotificationSendError as exc:
        insert_notification_event(
            conn,
            article_id=None,
            channel="telegram",
            destination=None,
            status="failed",
            message=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    insert_notification_event(
        conn,
        article_id=None,
        channel="telegram",
        destination=result.get("destination"),
        status="sent",
        message="텔레그램 테스트 메시지 전송",
    )
    return result


@app.get("/articles/{article_id}/notification-events")
def article_notification_events(article_id: int, conn=Depends(db_session)) -> list[dict]:
    if get_article(conn, article_id) is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return list_notification_events(conn, article_id=article_id, limit=20)


@app.post("/articles/{article_id}/telegram")
def article_telegram_send(
    article_id: int,
    payload: TelegramArticleSendRequest | None = None,
    conn=Depends(db_session),
) -> dict:
    item = get_article(conn, article_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Article not found")
    try:
        result = send_article_to_telegram(item, note=payload.note if payload else None)
    except NotificationConfigError as exc:
        insert_notification_event(
            conn,
            article_id=article_id,
            channel="telegram",
            destination=None,
            status="config_error",
            message=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotificationSendError as exc:
        insert_notification_event(
            conn,
            article_id=article_id,
            channel="telegram",
            destination=None,
            status="failed",
            message=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    insert_notification_event(
        conn,
        article_id=article_id,
        channel="telegram",
        destination=result.get("destination"),
        status="sent",
        message=f"기사 전송: {item.get('title')}",
    )
    return result


@app.post("/articles/{article_id}/contact-logs")
def add_article_contact_log(article_id: int, payload: ContactLogCreate, conn=Depends(db_session)) -> dict:
    if get_article(conn, article_id) is None:
        raise HTTPException(status_code=404, detail="Article not found")
    target = payload.contact_target.strip()
    note = payload.response_note.strip()
    if not target or not note:
        raise HTTPException(status_code=400, detail="contact_target and response_note are required")
    cursor = conn.execute(
        """
        INSERT INTO contact_logs (article_id, contact_target, response_note, next_check_at)
        VALUES (?, ?, ?, ?)
        """,
        (article_id, target, note, payload.next_check_at),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM contact_logs WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@app.get("/scheduler/status")
def crawl_scheduler_status(conn=Depends(db_session)) -> dict:
    return scheduler_status(conn)


@app.post("/crawl/refresh")
def crawl_refresh(payload: CrawlRefreshRequest, conn=Depends(db_session)) -> dict:
    if not MANUAL_CRAWL_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "이미 전체 또는 실시간 수집이 진행 중입니다. 왼쪽 자동 수집기 진행률을 확인하세요.",
                "scheduler": scheduler_status(conn),
            },
        )
    started = time.perf_counter()
    try:
        source_names = _source_filter_values(payload.source_name, payload.source_names)
        crawl_result = CrawlService().crawl_enabled_sources_with_articles(
            conn,
            source_names=source_names,
            source_category=payload.source_category,
            cancel_key=MANUAL_CRAWL_CANCEL_KEY,
            reset_cancel=True,
        )
        results = crawl_result["results"]
        fetched_results = crawl_result["fetched_results"]
        notified_count = notify_search_matches(crawl_result["new_articles"], payload.q)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "results": results,
            "fetched_results": fetched_results,
            "source_count": len(results),
            "fetched_article_count": sum(count for count in fetched_results.values() if count > 0),
            "new_article_count": sum(count for count in results.values() if count > 0),
            "search_match_notification_count": notified_count,
            "failed_source_count": sum(1 for count in results.values() if count < 0),
            "failed_source_names": [name for name, count in results.items() if count < 0],
            "empty_source_count": sum(1 for count in fetched_results.values() if count == 0),
            "empty_source_names": [name for name, count in fetched_results.items() if count == 0],
            "canceled": bool(crawl_result.get("canceled")),
            "elapsed_ms": elapsed_ms,
        }
    finally:
        MANUAL_CRAWL_LOCK.release()


@app.post("/crawl/cancel")
def crawl_cancel(conn=Depends(db_session)) -> dict:
    set_app_state(conn, MANUAL_CRAWL_CANCEL_KEY, "1")
    conn.commit()
    return {"cancel_requested": True}


@app.get("/articles")
def articles(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    source_name: str | None = None,
    source_names: list[str] | None = Query(default=None),
    q: str | None = None,
    min_importance: float | None = Query(default=None, ge=0),
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = Query(default=None, ge=1, le=365),
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
    needs_work: bool = False,
    needs_review: bool = False,
    missing_author: bool = False,
    has_alert: bool = False,
    has_body: bool = False,
    needs_detail: bool = False,
    sort: str | None = Query(default="latest", pattern="^(latest|oldest|importance|source|ready)$"),
    conn=Depends(db_session),
    ) -> list[dict]:
    return list_articles(
        conn,
        limit=limit,
        offset=offset,
        source_name=source_name,
        source_names=source_names,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
        collected_within_days=collected_within_days,
        collected_from=collected_from,
        collected_to=collected_to,
        region_group=region_group,
        needs_work=needs_work,
        needs_review=needs_review,
        missing_author=missing_author,
        has_alert=has_alert,
        has_body=has_body,
        needs_detail=needs_detail,
        sort=sort,
    )


@app.get("/articles/count")
def articles_count(
    source_name: str | None = None,
    source_names: list[str] | None = Query(default=None),
    q: str | None = None,
    min_importance: float | None = Query(default=None, ge=0),
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = Query(default=None, ge=1, le=365),
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
    needs_work: bool = False,
    needs_review: bool = False,
    missing_author: bool = False,
    has_alert: bool = False,
    has_body: bool = False,
    needs_detail: bool = False,
    conn=Depends(db_session),
) -> dict:
    return {
        "count": count_articles(
            conn,
            source_name=source_name,
            source_names=source_names,
            q=q,
            min_importance=min_importance,
            newsroom_status=newsroom_status,
            source_category=source_category,
            assignee=assignee,
            collected_within_days=collected_within_days,
            collected_from=collected_from,
            collected_to=collected_to,
            region_group=region_group,
            needs_work=needs_work,
            needs_review=needs_review,
            missing_author=missing_author,
            has_alert=has_alert,
            has_body=has_body,
            needs_detail=needs_detail,
        )
    }


@app.get("/priority")
def priority_articles(
    limit: int = Query(default=50, ge=1, le=200),
    source_names: list[str] | None = Query(default=None),
    source_category: str | None = None,
    region_group: str | None = None,
    conn=Depends(db_session),
) -> list[dict]:
    return list_priority_articles(
        conn,
        limit=limit,
        source_names=source_names,
        source_category=source_category,
        region_group=region_group,
    )


@app.get("/alerts")
def alerts(
    limit: int = Query(default=50, ge=1, le=200),
    threshold: float = Query(default=4.0, ge=0),
    source_names: list[str] | None = Query(default=None),
    source_category: str | None = None,
    region_group: str | None = None,
    strict_urgent: bool = False,
    collected_within_hours: int | None = Query(default=None, ge=1, le=168),
    conn=Depends(db_session),
) -> list[dict]:
    return list_alert_articles(
        conn,
        limit=limit,
        threshold=threshold,
        source_names=source_names,
        source_category=source_category,
        region_group=region_group,
        strict_urgent=strict_urgent,
        collected_within_hours=collected_within_hours,
    )


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
    source_names: list[str] | None = Query(default=None),
    q: str | None = None,
    min_importance: float | None = Query(default=None, ge=0),
    newsroom_status: str | None = None,
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = Query(default=None, ge=1, le=365),
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
    needs_work: bool = False,
    needs_review: bool = False,
    missing_author: bool = False,
    has_alert: bool = False,
    has_body: bool = False,
    needs_detail: bool = False,
    sort: str | None = Query(default="latest", pattern="^(latest|oldest|importance|source|ready)$"),
    conn=Depends(db_session),
) -> Response:
    articles = list_articles(
        conn,
        limit=limit,
        source_names=source_names,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
        collected_within_days=collected_within_days,
        collected_from=collected_from,
        collected_to=collected_to,
        region_group=region_group,
        needs_work=needs_work,
        needs_review=needs_review,
        missing_author=missing_author,
        has_alert=has_alert,
        has_body=has_body,
        needs_detail=needs_detail,
        sort=sort,
    )
    return Response(
        content=articles_to_csv(articles),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=newsroom_articles.csv"},
    )


@app.get("/exports/cuesheet.txt")
def export_cuesheet(
    limit: int = Query(default=50, ge=1, le=300),
    source_names: list[str] | None = Query(default=None),
    q: str | None = None,
    min_importance: float | None = Query(default=None, ge=0),
    newsroom_status: str | None = "ready",
    source_category: str | None = None,
    assignee: str | None = None,
    collected_within_days: int | None = Query(default=None, ge=1, le=365),
    collected_from: str | None = None,
    collected_to: str | None = None,
    region_group: str | None = None,
    needs_work: bool = False,
    needs_review: bool = False,
    missing_author: bool = False,
    has_alert: bool = False,
    has_body: bool = False,
    needs_detail: bool = False,
    sort: str | None = Query(default="latest", pattern="^(latest|oldest|importance|source|ready)$"),
    conn=Depends(db_session),
) -> PlainTextResponse:
    articles = list_articles(
        conn,
        limit=limit,
        source_names=source_names,
        q=q,
        min_importance=min_importance,
        newsroom_status=newsroom_status,
        source_category=source_category,
        assignee=assignee,
        collected_within_days=collected_within_days,
        collected_from=collected_from,
        collected_to=collected_to,
        region_group=region_group,
        needs_work=needs_work,
        needs_review=needs_review,
        missing_author=missing_author,
        has_alert=has_alert,
        has_body=has_body,
        needs_detail=needs_detail,
        sort=sort,
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
