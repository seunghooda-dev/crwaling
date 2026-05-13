import sqlite3
from datetime import datetime, timedelta

from app.database import SCHEMA
from app.content import clean_html_text, extract_media_urls
from app.models import Article
from app.repository import count_articles, list_articles
from app.services.ai_assist import build_ai_assist
from app.services.scoring import apply_newsroom_scoring


def test_clean_html_text_removes_tags():
    assert clean_html_text("<p>Hello <b>world</b></p>") == "Hello world"


def test_extract_media_urls():
    images, videos = extract_media_urls('<img src="a.jpg"><video><source src="b.mp4"></video>')
    assert images == ["a.jpg"]
    assert videos == ["b.mp4"]


def test_scoring_title_breaking_keyword():
    article = Article(source_name="SBS News Latest", source_type="rss", title="[단독] 화재 발생", url="https://x", fingerprint="x")
    scored = apply_newsroom_scoring(article)
    assert scored.importance_score >= 4.5
    assert "화재" in scored.keywords


def test_ai_assist_contains_anchor_line():
    assist = build_ai_assist({"title": "테스트 기사", "source_name": "source", "summary": "본문", "keywords": '["화재"]'})
    assert "앵커 멘트 초안" in assist["ai_summary"]


def test_article_period_filter_uses_collected_at():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    recent = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, collected_at)
        VALUES
          ('source', 'rss', '최근 교통사고', 'https://recent', 'recent', ?),
          ('source', 'rss', '오래된 교통사고', 'https://old', 'old', ?)
        """,
        (recent, old),
    )

    assert count_articles(conn, q="교통사고", collected_within_days=1) == 1
    assert [row["title"] for row in list_articles(conn, q="교통사고", collected_within_days=1)] == ["최근 교통사고"]
