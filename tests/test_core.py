import sqlite3
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from app.crawlers.html import HtmlCrawler
from app.crawlers.http import crawler_headers, fetch_with_retry
from app.database import SCHEMA
from app.content import clean_html_text, extract_media_urls
from app.models import Article, Source, SourceType
from app.repository import count_articles, list_articles, list_source_quality, scheduler_status, set_app_state
from app.services.ai_assist import build_ai_assist
from app.services.notification_service import notify_search_matches
from app.services.scoring import apply_newsroom_scoring
from app.title_extractor import clean_title_text, split_title_summary


def test_clean_html_text_removes_tags():
    assert clean_html_text("<p>Hello <b>world</b></p>") == "Hello world"


def test_extract_media_urls():
    images, videos = extract_media_urls('<img src="a.jpg"><video><source src="b.mp4"></video>')
    assert images == ["a.jpg"]
    assert videos == ["b.mp4"]


def test_title_extractor_splits_body_from_long_anchor_text():
    title, summary = split_title_summary(
        "트럼프 곧 베이징 도착…서울에선 사전 담판 [앵커] 중국행 전용기에 몸을 실은 미국 트럼프 대통령은 곧 베이징에 도착합니다."
    )
    assert title == "트럼프 곧 베이징 도착…서울에선 사전 담판"
    assert summary.startswith("[앵커]")


def test_title_extractor_decodes_entities_and_removes_date_suffix():
    assert clean_title_text("[포토] &#039;원샷원킬&#039; 솔지 2026.05.13 (19:32)") == "[포토] '원샷원킬' 솔지"


def test_search_notification_without_config_is_noop():
    assert notify_search_matches([{"title": "교통사고 발생", "url": "https://x"}], "교통사고") == 0


def test_scoring_title_breaking_keyword():
    article = Article(source_name="SBS News Latest", source_type="rss", title="[단독] 화재 발생", url="https://x", fingerprint="x")
    scored = apply_newsroom_scoring(article)
    assert scored.importance_score >= 4.5
    assert "화재" in scored.keywords


def test_ai_assist_contains_anchor_line():
    assist = build_ai_assist({"title": "테스트 기사", "source_name": "source", "summary": "본문", "keywords": '["화재"]'})
    assert "앵커 멘트 초안" in assist["ai_summary"]


def test_article_period_filter_prefers_published_at():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    recent = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, published_at, collected_at)
        VALUES
          ('source', 'rss', '최근 교통사고', 'https://recent', 'recent', ?, ?),
          ('source', 'rss', '오래된 교통사고', 'https://old', 'old', ?, ?)
        """,
        (recent, recent, old, recent),
    )

    assert count_articles(conn, q="교통사고", collected_within_days=1) == 1
    assert [row["title"] for row in list_articles(conn, q="교통사고", collected_within_days=1)] == ["최근 교통사고"]


def test_article_sort_and_date_range_filters():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, collected_at, importance_score, newsroom_status)
        VALUES
          ('B', 'rss', '낮은 점수', 'https://b', 'b', '2026-05-13 10:00:00', 1, 'new'),
          ('A', 'rss', '방송 후보', 'https://a', 'a', '2026-05-13 11:00:00', 5, 'ready')
        """
    )

    rows = list_articles(conn, collected_from="2026-05-13 00:00:00", collected_to="2026-05-13 23:59:59", sort="source")
    assert [row["source_name"] for row in rows] == ["A", "B"]
    rows = list_articles(conn, collected_from="2026-05-13 00:00:00", collected_to="2026-05-13 23:59:59", sort="oldest")
    assert [row["title"] for row in rows] == ["낮은 점수", "방송 후보"]
    assert count_articles(conn, collected_from="2026-05-14 00:00:00") == 0


def test_article_list_supports_offset_pagination():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, published_at)
        VALUES ('source', 'rss', ?, ?, ?, ?)
        """,
        [
            ("첫 기사", "https://one", "one", "2026-05-13T19:00:00+09:00"),
            ("둘째 기사", "https://two", "two", "2026-05-13T18:00:00+09:00"),
            ("셋째 기사", "https://three", "three", "2026-05-13T17:00:00+09:00"),
        ],
    )

    rows = list_articles(conn, limit=1, offset=1, sort="latest")
    assert [row["title"] for row in rows] == ["둘째 기사"]


def test_latest_sort_prefers_published_at_over_collection_batch():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, published_at, collected_at)
        VALUES
          ('A', 'rss', '늦게 수집된 예전 기사', 'https://old', 'old', '2026-05-13T18:00:00+09:00', '2026-05-13 10:40:00'),
          ('B', 'rss', '먼저 수집된 최신 기사', 'https://new', 'new', '2026-05-13T19:00:00+09:00', '2026-05-13 10:30:00')
        """
    )

    rows = list_articles(conn, sort="latest")
    assert [row["title"] for row in rows] == ["먼저 수집된 최신 기사", "늦게 수집된 예전 기사"]


def test_latest_sort_puts_unknown_published_time_after_known_time():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, published_at, collected_at)
        VALUES
          ('A', 'html', '수집 시간만 있는 기사', 'https://unknown', 'unknown', NULL, '2026-05-13 10:40:00'),
          ('B', 'rss', '발행 시간이 있는 기사', 'https://known', 'known', '2026-05-13T19:00:00+09:00', '2026-05-13 10:30:00')
        """
    )

    rows = list_articles(conn, sort="latest")
    assert [row["title"] for row in rows] == ["발행 시간이 있는 기사", "수집 시간만 있는 기사"]


def test_source_quality_reports_zero_new_streak():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url)
        VALUES ('Source A', 'rss', 'news', 'https://example.com')
        """
    )
    conn.executemany(
        "INSERT INTO crawl_runs (source_name, status, new_article_count) VALUES ('Source A', 'success', ?)",
        [(0,), (0,), (0,)],
    )

    [quality] = list_source_quality(conn)
    assert quality["zero_new_streak"] == 3
    assert quality["empty_fetch_streak"] == 3
    assert quality["zero_new_status"] == "selector_check"
    assert quality["risk_level"] == "danger"


def test_source_quality_distinguishes_duplicate_only_from_empty_fetch():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url)
        VALUES ('Source A', 'rss', 'news', 'https://example.com')
        """
    )
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint)
        VALUES ('Source A', 'rss', '기존 기사', 'https://article', 'article')
        """
    )
    conn.executemany(
        """
        INSERT INTO crawl_runs (source_name, status, new_article_count, fetched_article_count)
        VALUES ('Source A', 'success', 0, ?)
        """,
        [(12,), (11,), (10,)],
    )

    [quality] = list_source_quality(conn)
    assert quality["duplicate_only_streak"] == 3
    assert quality["empty_fetch_streak"] == 0
    assert quality["zero_new_status"] == "duplicate_only"
    assert quality["risk_level"] == "normal"


def test_source_quality_success_rate_excludes_canceled_runs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url)
        VALUES ('Source A', 'rss', 'news', 'https://example.com')
        """
    )
    conn.executemany(
        "INSERT INTO crawl_runs (source_name, status, new_article_count) VALUES ('Source A', ?, 0)",
        [("success",), ("success",), ("canceled",)],
    )

    [quality] = list_source_quality(conn)
    assert quality["canceled_runs"] == 1
    assert quality["measured_runs"] == 2
    assert quality["success_rate"] == 100.0


def test_source_quality_flags_never_crawled_source():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url)
        VALUES ('Source A', 'rss', 'news', 'https://example.com')
        """
    )

    [quality] = list_source_quality(conn)
    assert quality["zero_new_status"] == "never_crawled"
    assert quality["risk_level"] == "danger"


def test_fetch_with_retry_retries_transient_status_codes():
    calls = []
    source = Source(
        name="Source A",
        source_type=SourceType.rss,
        url="https://example.com/feed.xml",
        max_retries=1,
    )

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        request = httpx.Request("GET", url)
        status = 503 if len(calls) == 1 else 200
        return httpx.Response(status, text="ok", request=request)

    response = fetch_with_retry(
        "https://example.com/feed.xml",
        crawler_headers("https://example.com/feed.xml", "application/rss+xml"),
        source,
        request_get=fake_get,
        sleep_seconds=0,
    )

    assert response.status_code == 200
    assert len(calls) == 2
    assert calls[0][1]["follow_redirects"] is True
    assert "ko-KR" in calls[0][1]["headers"]["Accept-Language"]


def test_html_crawler_prefers_concise_title_attributes():
    source = Source(
        name="MBC News Monitor",
        source_type=SourceType.html,
        url="https://imnews.imbc.com/m_main.html",
    )
    soup = BeautifulSoup(
        """
        <a href="/news/2026/society/article.html" title="정확한 기사 제목">
          정확한 기사 제목 기자 설명과 긴 본문 요약이 이어집니다
        </a>
        """,
        "html.parser",
    )

    assert HtmlCrawler()._anchor_title(source, soup.a) == "정확한 기사 제목"


def test_scheduler_status_includes_crawl_progress():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    set_app_state(conn, "crawl_progress_status", "running")
    set_app_state(conn, "crawl_progress_current", "SBS News Latest")
    set_app_state(conn, "crawl_progress_index", "3")
    set_app_state(conn, "crawl_progress_total", "35")

    status = scheduler_status(conn)
    assert status["progress"] == {
        "status": "running",
        "current_source": "SBS News Latest",
        "index": 3,
        "total": 35,
    }
