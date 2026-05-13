import sqlite3
from datetime import datetime, timedelta

from app.database import SCHEMA
from app.content import clean_html_text, extract_media_urls
from app.models import Article
from app.repository import count_articles, list_articles, list_source_quality
from app.services.ai_assist import build_ai_assist
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
