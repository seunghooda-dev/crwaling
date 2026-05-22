import json
import sqlite3
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from app.author import clean_author_display, infer_reporter_from_article_text
from app.crawlers.html import HtmlCrawler
from app.crawlers.http import crawler_headers, fetch_with_retry
from app.crawlers.daum_channel import DaumChannelCrawler
from app.crawlers.jtbc_api import JtbcApiCrawler
from app.crawlers.safe_korea import SafeKoreaDisasterMessageCrawler
from app.database import SCHEMA
from app.content import clean_html_text, extract_media_urls
from app.models import Article, Source, SourceType
from app.repository import (
    auto_triage_articles,
    count_detail_backlog,
    count_detail_enrichment_queue,
    classify_crawl_error,
    count_articles,
    detail_enrichment_summary,
    insert_article,
    list_alert_articles,
    list_crawl_runs,
    list_articles,
    list_detail_enrichment_queue,
    list_region_counts,
    list_source_quality,
    repair_article_data,
    scheduler_status,
    set_app_state,
    source_failure_diagnostics,
)
from app.services.ai_assist import build_ai_assist
from app.services.crawl_service import CrawlService, _should_auto_enrich_details
from app.services.detail_service import _extract_author, _extract_body_text, enrich_article_details
from app.services.notification_service import (
    NotificationConfigError,
    notification_status,
    notify_search_matches,
    send_article_to_telegram,
)
from app.services.quality_service import enrich_article_quality
from app.services.reliability_service import article_reliability
from app.services.region_service import extract_regions
from app.services.scoring import apply_newsroom_scoring
from app.services.newsroom_advisor import (
    build_command_center,
    compare_cluster_for_reporter,
    false_risk_dashboard,
    reporter_advice_for_article,
    suggested_ready_queue,
    suggested_alert_rules,
)
from app.text import canonicalize_url, normalize_article_url, normalize_space
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


def test_title_extractor_splits_tight_news_body_after_sentence():
    title, summary = split_title_summary(
        "러시아 선박 침몰 가능성이 제기됐습니다.CNN 방송은 현지 수사 자료와 정보 분석을 토대로 추가 내용을 전했습니다."
    )
    assert title == "러시아 선박 침몰 가능성이 제기됐습니다."
    assert summary.startswith("CNN 방송")


def test_title_extractor_decodes_entities_and_removes_date_suffix():
    assert clean_title_text("[포토] &#039;원샷원킬&#039; 솔지 2026.05.13 (19:32)") == "[포토] '원샷원킬' 솔지"


def test_text_normalization_repairs_broken_entities_and_tracking_urls():
    assert normalize_space("폭염중대경보 가동hellip;9월까지&nbsp;운영") == "폭염중대경보 가동...9월까지 운영"
    assert normalize_space("ldquo;단독rdquo; 조사 결과 lsquo;주의rsquo; middot; 확인") == "\"단독\" 조사 결과 '주의' · 확인"
    assert (
        canonicalize_url("https://news.sbs.co.kr/news/endPage.do?news_id=N1&plink=RSSLINK&cooper=RSSREADER&utm_source=rss")
        == "https://news.sbs.co.kr/news/endPage.do?news_id=N1"
    )
    assert canonicalize_url("https://www.pressian.com/pages/articles/123&ref=rss") == "https://www.pressian.com/pages/articles/123"
    assert (
        normalize_article_url("https://news.jtbc.co.kr/article/article.aspx?news_id=NB12220975")
        == "https://news.jtbc.co.kr/article/NB12220975"
    )
    assert (
        canonicalize_url("https://news.jtbc.co.kr/article/article.aspx?news_id=nb12220975&utm_source=rss")
        == "https://news.jtbc.co.kr/article/NB12220975"
    )
    assert (
        canonicalize_url("https://www.ubc.co.kr/wp/archives/127635%20")
        == "https://www.ubc.co.kr/wp/archives/127635"
    )
    assert (
        normalize_article_url("https://www.safe182.go.kr/cont/homeLogContents.do?contentsNm=sexual_chatbot%20")
        == "https://www.safe182.go.kr/cont/homeLogContents.do?contentsNm=sexual_chatbot"
    )
    assert (
        normalize_article_url(
            "https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do;jsessionid=abc.node10?bbsId=BBSMSTR_000000000008&nttId=125957"
        )
        == "https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do?bbsId=BBSMSTR_000000000008&nttId=125957"
    )
    assert (
        canonicalize_url(
            "https://www.g1tv.co.kr/news/?mid=1_207_6&newsid=343605&newscode=010700"
        )
        == "https://www.g1tv.co.kr/news/?newsid=343605"
    )
    assert (
        canonicalize_url(
            "https://www.tbc.co.kr/news/view?c1=8news&c2=&pno=20260515175344AE09849&id=206476"
        )
        == "https://www.tbc.co.kr/news/view?id=206476&pno=20260515175344AE09849"
    )


def test_insert_article_skips_duplicate_canonical_url():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    first = Article(
        source_name="MOIS Press Releases",
        source_type="html",
        source_category="disaster",
        title="고위관리자 교육으로 재난대처 실전 지휘역량 제고",
        url="https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do;jsessionid=abc.node10?bbsId=BBSMSTR_000000000008&nttId=125957",
        canonical_url=None,
        fingerprint="first",
    )
    second = Article(
        source_name="MOIS Press Releases",
        source_type="html",
        source_category="disaster",
        title="고위관리자 교육으로 재난대처 실전 지휘역량 제고",
        url="https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do;jsessionid=def.node20?bbsId=BBSMSTR_000000000008&nttId=125957",
        canonical_url=None,
        fingerprint="second",
    )

    assert insert_article(conn, first) is True
    assert insert_article(conn, second) is False
    assert conn.execute("SELECT count(*) FROM articles").fetchone()[0] == 1


def test_repair_article_data_splits_long_mixed_title_and_holds_navigation():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, quality_score, quality_flags)
        VALUES ('JTV News Monitor', 'html', ?, 'https://jtv/1', 'old', 80, '[]')
        """,
        (
            "[JTV 8뉴스] 남원시청 압수수색 이재명 대통령 지시로 단속이 진행되고 있습니다. "
            "경찰은 압수물을 분석하고 있으며 추가 조사를 이어갈 방침입니다. "
            "남원시는 특정 시설에 특혜를 주기 위한 공사가 아니라고 설명했습니다. "
            "수사대는 공사 발주와 행정 처리 과정 전반에 위법 행위가 있었는지 살펴보고 있습니다. 김민지 기자입니다.",
        ),
    )
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, quality_score, quality_flags)
        VALUES ('MOIS Press Releases', 'html', '개인정보처리방침', 'https://mois/nav', 'nav', 80, '[]')
        """
    )

    result = repair_article_data(conn)

    repaired = conn.execute("SELECT * FROM articles WHERE url = 'https://jtv/1'").fetchone()
    held = conn.execute("SELECT * FROM articles WHERE url = 'https://mois/nav'").fetchone()
    assert result["repaired_titles"] == 1
    assert len(repaired["title"]) < 120
    assert "추가 조사를" in repaired["summary"]
    assert held["newsroom_status"] == "hold"
    assert "navigation_like_title" in held["quality_flags"]


def test_repair_article_data_normalizes_entity_damaged_text():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, summary, author, quality_score, quality_flags)
        VALUES ('Pressian Latest RSS', 'rss', '폭염중대경보 가동hellip;9월까지', 'https://pressian/1', 'old', '현장&nbsp;대응', '\\uBC15\\uBC94\\uC2DD', 100, '[]')
        """
    )

    result = repair_article_data(conn)
    article = conn.execute("SELECT * FROM articles WHERE url = 'https://pressian/1'").fetchone()

    assert result["normalized_texts"] == 1
    assert result["cleaned_authors"] == 1
    assert article["title"] == "폭염중대경보 가동...9월까지"
    assert article["summary"] == "현장 대응"
    assert article["author"] == "박범식 기자"


def test_repair_article_data_infers_missing_reporter_from_article_text():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, summary, quality_score, quality_flags)
        VALUES (
            'JTV News Monitor',
            'html',
            '익산시 자율주행 버스 점검',
            'https://jtv/author',
            'jtv-author',
            '자율주행 시스템입니다. 김진형 기자 jtvjin@jtv.co.kr (JTV 전주방송) JTV 8뉴스 김진형 기자 2026.05.14',
            100,
            '[]'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, summary, quality_score, quality_flags)
        VALUES (
            'Channel A News Monitor',
            'html',
            '[아는기자]1분 멈추면 18억 손실',
            'https://channel-a/no-author',
            'channel-a-no-author',
            '',
            100,
            '[]'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO articles (
            source_name, source_type, title, url, fingerprint, body_text, quality_score, quality_flags
        )
        VALUES (
            'CJB News Monitor',
            'html',
            '새총으로 쇠구슬 쏴 택시 파손',
            'https://cjb/author',
            'cjb-author',
            '새총으로 쇠구슬 쏴 택시 파손 작성자 박언 작성일 2026-05-17 조회수 223 청주에서 발생했습니다.',
            100,
            '[]'
        )
        """
    )

    result = repair_article_data(conn)
    rows = conn.execute("SELECT url, author FROM articles ORDER BY url").fetchall()
    authors = {row["url"]: row["author"] for row in rows}

    assert result["inferred_authors"] == 2
    assert authors["https://cjb/author"] == "박언 기자"
    assert authors["https://jtv/author"] == "김진형 기자"
    assert authors["https://channel-a/no-author"] is None


def test_article_list_can_filter_needs_work_across_database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO articles (
            source_name, source_type, source_category, title, url, fingerprint,
            summary, author, quality_score, verification_status, duplicate_group_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "Verified API",
                "api",
                "news",
                "충분히 검증된 기사",
                "https://ok",
                "ok",
                (
                    "본문이 충분하고 기관 API에서 들어온 기사입니다. 원문 시간과 출처가 확인됐고 "
                    "방송 전 추가 확인 부담이 크지 않은 안정적인 기사로 분류할 수 있습니다."
                ),
                None,
                100,
                "verified",
                "ok-group",
            ),
            (
                "Local HTML",
                "html",
                "news",
                "기자명 없는 지역 기사",
                "https://work",
                "work",
                "본문은 있지만 기자명과 복수 출처 확인이 필요한 기사입니다.",
                None,
                100,
                "needs_review",
                "work-group",
            ),
            (
                "Bad Source",
                "html",
                "news",
                "품질 낮은 기사",
                "https://bad",
                "bad",
                "",
                None,
                30,
                "needs_review",
                "bad-group",
            ),
        ],
    )

    items = list_articles(conn, needs_work=True, sort="latest")

    assert count_articles(conn) == 3
    assert count_articles(conn, needs_work=True) == 2
    assert {item["url"] for item in items} == {"https://work", "https://bad"}


def test_article_list_can_filter_needs_review_statuses():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, verification_status)
        VALUES (?, 'html', ?, ?, ?, ?)
        """,
        [
            ("Source A", "검증 필요 기사", "https://review", "review", "needs_review"),
            ("Source B", "사용 주의 기사", "https://caution", "caution", "caution"),
            ("Source C", "확인 완료 기사", "https://verified", "verified", "verified"),
        ],
    )

    items = list_articles(conn, needs_review=True, sort="latest")

    assert count_articles(conn, needs_review=True) == 2
    assert {item["url"] for item in items} == {"https://review", "https://caution"}


def test_article_list_can_filter_missing_news_authors():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO articles (
            source_name, source_type, source_category, title, url, fingerprint, author
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("HTML News", "html", "news", "기자명 없는 뉴스", "https://missing", "missing", None),
            ("RSS News", "rss", "news", "기자명 공백 뉴스", "https://blank", "blank", " "),
            ("API News", "api", "news", "API 기관 기사", "https://api", "api", None),
            ("Fire Source", "html", "fire", "소방 기관 자료", "https://fire", "fire", None),
            ("HTML News", "html", "news", "기자명 있는 뉴스", "https://author", "author", "홍길동 기자"),
        ],
    )

    items = list_articles(conn, missing_author=True, sort="latest")

    assert count_articles(conn, missing_author=True) == 2
    assert {item["url"] for item in items} == {"https://missing", "https://blank"}


def test_article_list_can_filter_stat_cards():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO articles (
            id, source_name, source_type, source_category, title, url, fingerprint,
            body_text, summary, author, importance_score, verification_status, quality_score
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                1,
                "Alert Source",
                "html",
                "news",
                "알림 기사",
                "https://alert",
                "alert",
                "본문이 있는 알림 기사입니다." * 10,
                None,
                "홍길동 기자",
                5.0,
                "needs_review",
                100,
            ),
            (
                2,
                "Detail Source",
                "html",
                "news",
                "우선 보강 기사",
                "https://detail",
                "detail",
                None,
                None,
                None,
                5.0,
                "needs_review",
                100,
            ),
            (
                3,
                "Done Source",
                "html",
                "news",
                "완성 기사",
                "https://done",
                "done",
                "본문이 있는 완성 기사입니다." * 10,
                None,
                "김민수 기자",
                0.0,
                "verified",
                100,
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO alert_events (article_id, title, source_name, importance_score, reason)
        VALUES (1, '알림 기사', 'Alert Source', 5.0, 'test')
        """
    )

    assert count_articles(conn, has_alert=True) == 1
    assert [item["url"] for item in list_articles(conn, has_alert=True)] == ["https://alert"]
    assert count_articles(conn, has_body=True) == 2
    assert {item["url"] for item in list_articles(conn, needs_detail=True)} == {"https://detail"}


def test_search_notification_without_config_is_noop():
    assert notify_search_matches([{"title": "교통사고 발생", "url": "https://x"}], "교통사고") == 0


def test_notification_status_masks_telegram_destination():
    status = notification_status({"telegram_bot_token": "token", "telegram_chat_id": "123456789"})

    assert status["telegram"]["configured"] is True
    assert status["telegram"]["chat_id"] == "12***89"


def test_send_article_to_telegram_posts_formatted_message(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": {"message_id": 10}}

    def fake_post(url, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("app.services.notification_service.httpx.post", fake_post)
    result = send_article_to_telegram(
        {
            "title": "공장 화재 1명 부상",
            "source_name": "SBS News Latest",
            "author": "김민수 기자",
            "published_at": "2026-05-20T10:00:00+09:00",
            "importance_score": 5.6,
            "verification_status": "needs_review",
            "keywords": '["화재", "부상"]',
            "region_tags": '["수도권"]',
            "summary": "소방당국이 정확한 화재 원인을 조사하고 있습니다.",
            "url": "https://example.com/article",
        },
        config={"telegram_bot_token": "token", "telegram_chat_id": "123456789"},
    )

    assert result["ok"] is True
    assert calls[0]["url"] == "https://api.telegram.org/bottoken/sendMessage"
    assert calls[0]["json"]["chat_id"] == "123456789"
    assert "공장 화재" in calls[0]["json"]["text"]
    assert "김민수 기자" in calls[0]["json"]["text"]
    assert "https://example.com/article" in calls[0]["json"]["text"]


def test_send_article_to_telegram_requires_config():
    try:
        send_article_to_telegram({"title": "기사", "url": "https://x"}, config={})
    except NotificationConfigError as exc:
        assert "텔레그램 설정" in str(exc)
    else:
        raise AssertionError("expected NotificationConfigError")


def test_clean_author_display_normalizes_common_feed_values():
    assert clean_author_display("noisycart@sbs.co.kr(최승훈 기자)") == "최승훈 기자"
    assert clean_author_display("KBC광주방송") is None
    assert clean_author_display("156762057") is None
    assert clean_author_display("작성자 박석호") == "박석호 기자"
    assert clean_author_display("임지선 경제부장") == "임지선 경제부장"
    assert clean_author_display("아는 기자") is None
    assert clean_author_display("@tvchosun.com") is None
    assert clean_author_display("tvchosun.com") is None
    assert clean_author_display("\\uC774\\uC120\\uD559") == "이선학 기자"


def test_infer_reporter_from_article_text_uses_strict_byline_contexts():
    assert (
        infer_reporter_from_article_text(
            "JTV News Monitor",
            "[JTV 8뉴스] 익산시, 자율주행 버스 점검",
            "자율주행 시스템입니다. 김진형 기자 jtvjin@jtv.co.kr (JTV 전주방송) JTV 8뉴스 김진형 기자 2026.05.14",
            "",
        )
        == "김진형 기자"
    )
    assert (
        infer_reporter_from_article_text(
            "Donga Ilbo RSS",
            "김하성의 부진",
            "[동아닷컴 조성운 기자] 부상 복귀 후 타격 부진을 면치 못하고 있습니다.",
            "",
        )
        == "조성운 기자"
    )
    assert (
        infer_reporter_from_article_text(
            "JTBC News Monitor",
            "월드컵 중계권 모두 확보",
            "JTBC가 2026년과 2030년 FIFA 월드컵 중계권을 확보했습니다. 홍지용 기자",
            "",
        )
        == "홍지용 기자"
    )
    assert (
        infer_reporter_from_article_text(
            "CJB News Monitor",
            "새총으로 쇠구슬 쏴 택시 파손",
            "",
            "새총으로 쇠구슬 쏴 택시 파손한 60대 아버지·20대 아들 입건 "
            "작성자 박언 작성일 2026-05-17 조회수 223 청주에서 새총으로 쇠구슬을 쐈습니다.",
        )
        == "박언 기자"
    )
    assert (
        infer_reporter_from_article_text(
            "Channel A News Monitor",
            "[아는기자]1분 멈추면 18억 손실",
            "",
            "",
        )
        is None
    )
    assert (
        infer_reporter_from_article_text(
            "MoneyToday RSS",
            "'취재진 폭행' 사건",
            "취재진을 폭행한 혐의와 기자회견 관련 내용을 전했습니다.",
            "",
        )
        is None
    )


def test_article_reliability_rewards_verified_cross_checked_articles():
    result = article_reliability(
        {
            "source_name": "National Fire Agency Press",
            "source_type": "html",
            "source_category": "fire",
            "title": "서울 공장 화재",
            "body_text": (
                "서울 공장에서 큰불이 나 소방당국이 대응했습니다. "
                "현장에는 장비와 인력이 투입됐고 인명 피해와 진화 상황을 확인 중입니다. "
                "관할 소방서는 주변 도로를 통제하고 추가 폭발 위험 여부를 점검하고 있습니다. "
                "경찰과 소방은 불길을 잡는 대로 정확한 화재 원인과 피해 규모를 조사할 계획입니다."
            ),
            "author": "김민수 기자",
            "quality_score": 92,
            "verification_status": "verified",
            "newsroom_status": "ready",
            "region_tags": '["서울"]',
            "cluster_source_count": 3,
            "cluster_article_count": 4,
            "image_urls": '["https://image"]',
        }
    )

    assert result["level"] == "high"
    assert "복수 출처 3곳" in result["reasons"]
    assert not result["missing"]


def test_article_reliability_flags_missing_body_and_reporter():
    result = article_reliability(
        {
            "source_name": "Local News",
            "source_type": "html",
            "source_category": "news",
            "title": "사고 발생",
            "summary": "",
            "quality_score": 45,
            "verification_status": "needs_review",
            "newsroom_status": "new",
            "quality_flags": '["short_title"]',
            "cluster_source_count": 1,
        }
    )

    assert result["level"] in {"warning", "danger"}
    assert "본문" in result["missing"]
    assert "기자명" in result["missing"]


def test_scoring_title_breaking_keyword():
    article = Article(source_name="SBS News Latest", source_type="rss", title="[단독] 화재 발생", url="https://x", fingerprint="x")
    scored = apply_newsroom_scoring(article)
    assert scored.importance_score >= 4.5
    assert "화재" in scored.keywords


def test_scoring_broad_political_terms_do_not_dominate_urgent_queue():
    article = Article(source_name="SBS News Latest", source_type="rss", title="대통령 관련 일정 확인", url="https://x", fingerprint="x")
    scored = apply_newsroom_scoring(article)
    assert scored.importance_score < 4.5


def test_scoring_suppresses_false_disaster_messages():
    article = Article(
        source_name="Safe Korea Disaster Messages",
        source_type="api",
        title="안전안내 · 화재 - 경기도 연천군",
        summary="방금 발신된 메시지는 훈련상황으로 실제상황이 아니며 오발송입니다.",
        url="https://x",
        fingerprint="x",
    )

    scored = apply_newsroom_scoring(article)

    assert scored.importance_score < 1
    assert scored.verification_status == "caution"
    assert "오발송/훈련" in scored.keywords


def test_reporter_advice_holds_false_disaster_messages():
    advice = reporter_advice_for_article(
        {
            "source_name": "Safe Korea Disaster Messages",
            "source_category": "disaster",
            "title": "안전안내 · 화재 - 경기도 연천군",
            "summary": "방금 발신된 메시지는 훈련상황으로 실제상황이 아니며 오발송입니다.",
            "importance_score": 5.8,
            "published_at": "2026-05-20T10:00:00",
            "region_tags": '["경기"]',
        }
    )

    assert advice["decision"] == "보류"
    assert advice["verification_risk"] == "높음"
    assert "오발송/훈련 가능" in advice["truth_flags"]


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


def test_detail_enrichment_queue_prioritizes_missing_body_and_author():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO articles (
            source_name, source_type, title, url, fingerprint, quality_score,
            importance_score, verification_status, body_text, author
        )
        VALUES ('SBS News Latest', 'rss', ?, ?, ?, 100, ?, ?, ?, ?)
        """,
        [
            ("낮은 기사", "https://low", "low", 1.0, "unchecked", None, None),
            ("긴급 기사", "https://hot", "hot", 6.0, "needs_review", None, None),
            ("완성 기사", "https://done", "done", 8.0, "needs_review", "본문이 충분합니다." * 20, "홍길동 기자"),
        ],
    )

    queue = list_detail_enrichment_queue(conn, limit=5)

    assert [item["url"] for item in queue] == ["https://hot"]
    assert queue[0]["detail_missing_fields"] == ["본문", "기자명"]


def test_detail_enrichment_summary_separates_priority_from_backlog():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO articles (
            source_name, source_type, title, url, fingerprint, quality_score,
            importance_score, verification_status, body_text, author
        )
        VALUES (?, ?, ?, ?, ?, 100, ?, ?, ?, ?)
        """,
        [
            ("RSS A", "rss", "낮은 기자명 없음", "https://low-author", "low-author", 1.0, "unchecked", "본문이 충분합니다." * 20, None),
            ("HTML B", "html", "우선 본문 없음", "https://priority-body", "priority-body", 5.5, "needs_review", None, "김민수 기자"),
            ("HTML B", "html", "우선 둘 다 없음", "https://priority-both", "priority-both", 6.0, "needs_review", None, None),
        ],
    )

    summary = detail_enrichment_summary(conn)
    priority_items = list_detail_enrichment_queue(conn, limit=10)
    backlog_items = list_detail_enrichment_queue(conn, limit=10, priority_only=False)

    assert count_detail_enrichment_queue(conn) == 2
    assert count_detail_backlog(conn) == 3
    assert summary["missing_author_count"] == 2
    assert summary["missing_body_count"] == 2
    assert [item["url"] for item in priority_items] == ["https://priority-both", "https://priority-body"]
    assert {item["url"] for item in backlog_items} == {"https://low-author", "https://priority-body", "https://priority-both"}
    assert summary["top_sources"][0]["recommendation"]


def test_auto_triage_marks_high_signal_articles_ready():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (
            source_name, source_type, title, url, fingerprint, quality_score,
            importance_score, verification_status, keywords
        )
        VALUES ('SBS News Latest', 'rss', '공장 화재 1명 사망', 'https://ready', 'ready', 100, 7, 'unchecked', '["화재", "사망"]')
        """
    )

    result = auto_triage_articles(conn)
    article = conn.execute("SELECT newsroom_status, verification_status FROM articles").fetchone()

    assert result["ready"] == 1
    assert article["newsroom_status"] == "ready"
    assert article["verification_status"] == "needs_review"


def test_article_filters_accept_multiple_sources():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, published_at)
        VALUES
          ('KNN News Monitor', 'html', '부산 지역 기사', 'https://knn', 'knn', '2026-05-13T19:00:00+09:00'),
          ('KBC News Monitor', 'html', '광주 지역 기사', 'https://kbc', 'kbc', '2026-05-13T18:00:00+09:00'),
          ('TBC News Monitor', 'html', '대구 지역 기사', 'https://tbc', 'tbc', '2026-05-13T17:00:00+09:00')
        """
    )

    rows = list_articles(conn, source_names=["KNN News Monitor", "KBC News Monitor"], sort="latest")

    assert [row["source_name"] for row in rows] == ["KNN News Monitor", "KBC News Monitor"]
    assert count_articles(conn, source_names=["KNN News Monitor", "KBC News Monitor"]) == 2


def test_article_list_normalizes_author_display():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, author, published_at)
        VALUES ('SBS News Latest', 'rss', '기자명 정규화 테스트', 'https://author', 'author', ?, '2026-05-13T19:00:00+09:00')
        """,
        ("noisycart@sbs.co.kr(최승훈 기자)",),
    )

    [row] = list_articles(conn)

    assert row["author"] == "최승훈 기자"


def test_crawl_service_auto_enriches_private_broadcast_new_articles(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url, enabled)
        VALUES ('KBC News Monitor', 'html', 'news', 'https://news.ikbc.co.kr/', 1)
        """
    )
    service = CrawlService()

    class FakeCrawler:
        def crawl(self, source):
            return [
                Article(
                    source_name=source.name,
                    source_type=source.source_type.value,
                    source_category=source.source_category,
                    title="KBC 기자명 자동 보강 테스트",
                    url="https://news.ikbc.co.kr/article/view/kbc202605170030",
                    canonical_url="https://news.ikbc.co.kr/article/view/kbc202605170030",
                    fingerprint="kbc-auto-author",
                    duplicate_group_id="kbc-auto-author",
                )
            ]

    calls = []

    def fake_enrich_article_details(conn_arg, article_id):
        calls.append(article_id)
        conn_arg.execute(
            "UPDATE articles SET author = ?, body_text = ? WHERE id = ?",
            ("박석호 기자", "상세 본문", article_id),
        )
        return True

    service.crawlers[SourceType.html] = FakeCrawler()
    monkeypatch.setattr("app.services.crawl_service.enrich_article_details", fake_enrich_article_details)

    result = service.crawl_enabled_sources_with_articles(conn, source_name="KBC News Monitor")

    assert result["results"]["KBC News Monitor"] == 1
    assert calls
    [row] = list_articles(conn, source_name="KBC News Monitor")
    assert row["author"] == "박석호 기자"
    assert result["new_articles"][0]["author"] == "박석호 기자"


def test_crawl_service_auto_enriches_existing_private_broadcast_articles(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url, enabled)
        VALUES ('KBC News Monitor', 'html', 'news', 'https://news.ikbc.co.kr/', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, source_category, title, url, canonical_url, fingerprint)
        VALUES (
            'KBC News Monitor', 'html', 'news', 'KBC 기존 기사',
            'https://news.ikbc.co.kr/article/view/kbc202605170030',
            'https://news.ikbc.co.kr/article/view/kbc202605170030',
            'existing-kbc'
        )
        """
    )
    service = CrawlService()

    class FakeCrawler:
        def crawl(self, source):
            return [
                Article(
                    source_name=source.name,
                    source_type=source.source_type.value,
                    source_category=source.source_category,
                    title="KBC 기존 기사",
                    url="https://news.ikbc.co.kr/article/view/kbc202605170030",
                    canonical_url="https://news.ikbc.co.kr/article/view/kbc202605170030",
                    fingerprint="existing-kbc",
                    duplicate_group_id="existing-kbc",
                )
            ]

    calls = []

    def fake_enrich_article_details(conn_arg, article_id):
        calls.append(article_id)
        conn_arg.execute(
            "UPDATE articles SET author = ?, body_text = ? WHERE id = ?",
            ("박석호 기자", "상세 본문", article_id),
        )
        return True

    service.crawlers[SourceType.html] = FakeCrawler()
    monkeypatch.setattr("app.services.crawl_service.enrich_article_details", fake_enrich_article_details)

    result = service.crawl_enabled_sources_with_articles(conn, source_name="KBC News Monitor")

    assert result["results"]["KBC News Monitor"] == 0
    assert calls
    [row] = list_articles(conn, source_name="KBC News Monitor")
    assert row["author"] == "박석호 기자"


def test_strict_urgent_alerts_keep_recent_safety_items_only():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany(
        """
        INSERT INTO articles (
            source_name, source_type, source_category, title, url, fingerprint,
            collected_at, importance_score, keywords, quality_score, quality_flags
        )
        VALUES (?, 'rss', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "SBS News Latest",
                "news",
                "강남 아파트 화재 주민 긴급 대피",
                "https://urgent",
                "urgent",
                recent,
                7,
                '["화재", "긴급", "대피"]',
                100,
                "[]",
            ),
            (
                "Politics RSS",
                "news",
                "대통령 관련 감사원 압수수색",
                "https://politics",
                "politics",
                recent,
                8,
                '["대통령", "압수수색"]',
                100,
                "[]",
            ),
            (
                "Old Fire RSS",
                "fire",
                "공장 화재 1명 사망",
                "https://old",
                "old",
                old,
                8,
                '["화재", "사망"]',
                100,
                "[]",
            ),
            (
                "Bad Parser",
                "news",
                "긴급 화재 제목과 본문이 뒤섞인 기사",
                "https://bad",
                "bad",
                recent,
                8,
                '["긴급", "화재"]',
                65,
                '["navigation_like_title"]',
            ),
        ],
    )

    rows = list_alert_articles(conn, threshold=4.5, strict_urgent=True, collected_within_hours=24)

    assert [row["url"] for row in rows] == ["https://urgent"]


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


def test_default_article_sort_is_latest_not_importance():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, published_at, importance_score)
        VALUES
          ('A', 'rss', '오래된 중요 기사', 'https://old-important', 'old-important', '2026-05-13T19:00:00+09:00', 9),
          ('B', 'rss', '방금 나온 기사', 'https://latest', 'latest', '2026-05-15T20:00:00+09:00', 1)
        """
    )

    rows = list_articles(conn)
    assert [row["title"] for row in rows] == ["방금 나온 기사", "오래된 중요 기사"]


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
        INSERT INTO sources (name, source_type, source_category, url, crawl_interval_seconds)
        VALUES ('Source A', 'rss', 'news', 'https://example.com', 86400)
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
        INSERT INTO sources (name, source_type, source_category, url, crawl_interval_seconds)
        VALUES ('Source A', 'rss', 'news', 'https://example.com', 86400)
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


def test_source_quality_marks_single_recent_failure_as_transient_warning():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url, crawl_interval_seconds)
        VALUES ('Source A', 'rss', 'news', 'https://example.com', 300)
        """
    )
    conn.executemany(
        """
        INSERT INTO crawl_runs (source_name, started_at, status, new_article_count, error_message)
        VALUES ('Source A', ?, ?, ?, ?)
        """,
        [
            ("2026-05-15 10:00:00", "success", 3, None),
            ("2026-05-15 10:05:00", "failed", 0, "connection reset"),
        ],
    )

    [quality] = list_source_quality(conn)
    assert quality["failure_streak"] == 1
    assert quality["zero_new_status"] == "transient_failed"
    assert quality["zero_new_label"] == "일시 실패"
    assert quality["risk_level"] == "warning"
    assert quality["failure_cause"] == "connection_reset"
    assert quality["failure_cause_label"] == "원격 연결 종료"


def test_source_quality_marks_repeated_recent_failures_as_danger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url, crawl_interval_seconds)
        VALUES ('Source A', 'rss', 'news', 'https://example.com', 300)
        """
    )
    conn.executemany(
        """
        INSERT INTO crawl_runs (source_name, started_at, status, new_article_count, error_message)
        VALUES ('Source A', ?, ?, ?, ?)
        """,
        [
            ("2026-05-15 10:00:00", "success", 3, None),
            ("2026-05-15 10:05:00", "failed", 0, "connection reset"),
            ("2026-05-15 10:10:00", "failed", 0, "connection reset"),
        ],
    )

    [quality] = list_source_quality(conn)
    assert quality["failure_streak"] == 2
    assert quality["zero_new_status"] == "repeated_failed"
    assert quality["zero_new_label"] == "연속 실패"
    assert quality["risk_level"] == "danger"


def test_source_quality_marks_stale_running_run_as_stalled():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url, crawl_interval_seconds)
        VALUES ('Source A', 'rss', 'news', 'https://example.com', 300)
        """
    )
    conn.execute(
        """
        INSERT INTO crawl_runs (source_name, started_at, status, new_article_count)
        VALUES ('Source A', datetime('now', '-20 minutes'), 'running', 0)
        """
    )

    [quality] = list_source_quality(conn)
    assert quality["zero_new_status"] == "stalled"
    assert quality["zero_new_label"] == "수집 멈춤"
    assert quality["risk_level"] == "danger"


def test_source_quality_deescalates_global_collection_idle():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for index in range(3):
        name = f"Source {index}"
        conn.execute(
            """
            INSERT INTO sources (name, source_type, source_category, url, crawl_interval_seconds)
            VALUES (?, 'rss', 'news', ?, 300)
            """,
            (name, f"https://example.com/{index}"),
        )
        conn.execute(
            """
            INSERT INTO crawl_runs (source_name, started_at, status, new_article_count, fetched_article_count)
            VALUES (?, datetime('now', '-2 hours'), 'success', 0, 10)
            """,
            (name,),
        )

    quality = list_source_quality(conn)

    assert {item["zero_new_status"] for item in quality} == {"global_idle"}
    assert {item["risk_level"] for item in quality} == {"normal"}
    assert all(item["global_collection_idle"] for item in quality)


def test_crawl_run_list_decorates_failure_causes_and_stalled_runs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO crawl_runs (source_name, started_at, status, error_message)
        VALUES (?, datetime('now', ?), ?, ?)
        """,
        [
            ("Failed Source", "-5 minutes", "failed", "[WinError 10054] 현재 연결은 원격 호스트에 의해 강제로 끊겼습니다"),
            ("Stalled Source", "-20 minutes", "running", None),
        ],
    )

    rows = list_crawl_runs(conn, limit=2)
    by_source = {row["source_name"]: row for row in rows}
    assert by_source["Failed Source"]["failure_cause"] == "connection_reset"
    assert by_source["Failed Source"]["status_label"] == "실패"
    assert by_source["Stalled Source"]["effective_status"] == "stalled"
    assert by_source["Stalled Source"]["status_label"] == "멈춤 의심"


def test_crawl_error_classifier_groups_common_errors():
    assert classify_crawl_error("ReadTimeout")["failure_cause"] == "timeout"
    assert classify_crawl_error("HTTP 403 Forbidden")["failure_cause"] == "blocked"
    assert classify_crawl_error("Could not resolve host")["failure_cause"] == "dns"
    assert classify_crawl_error("HTTP 429 Too Many Requests")["failure_retryable"] is True
    assert classify_crawl_error("JSONDecodeError Expecting value")["failure_group"] == "parser"


def test_source_failure_diagnostics_summarizes_recent_causes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url, enabled)
        VALUES ('Blocked Source', 'html', 'news', 'https://blocked', 1)
        """
    )
    conn.executemany(
        """
        INSERT INTO crawl_runs (source_name, started_at, status, error_message)
        VALUES ('Blocked Source', datetime('now', ?), 'failed', ?)
        """,
        [
            ("-30 minutes", "HTTP 403 Forbidden"),
            ("-20 minutes", "HTTP 429 Too Many Requests"),
        ],
    )

    diagnostics = source_failure_diagnostics(conn)
    causes = {item["cause"]: item for item in diagnostics["cause_counts"]}

    assert diagnostics["failed_run_count"] == 2
    assert diagnostics["current_risk_count"] == 1
    assert causes["blocked"]["group"] == "access"
    assert causes["rate_limited"]["retryable"] is True
    assert diagnostics["action_items"][0]["source_name"] == "Blocked Source"


def test_source_quality_latest_new_articles_clear_zero_label():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url, crawl_interval_seconds)
        VALUES ('Source A', 'rss', 'news', 'https://example.com', 86400)
        """
    )
    conn.executemany(
        """
        INSERT INTO crawl_runs (source_name, started_at, status, new_article_count, fetched_article_count)
        VALUES ('Source A', datetime('now', ?), 'success', ?, ?)
        """,
        [
            ("-30 minutes", 0, 10),
            ("-20 minutes", 0, 10),
            ("-10 minutes", 5, 10),
        ],
    )

    [quality] = list_source_quality(conn)
    assert quality["last_new_article_count"] == 5
    assert quality["zero_new_status"] == "ok"
    assert quality["zero_new_label"] == "정상"


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


def test_html_crawler_recognizes_knn_article_links():
    source = Source(
        name="KNN News Monitor",
        source_type=SourceType.html,
        url="https://news.knn.co.kr/news",
    )
    soup = BeautifulSoup('<a href="/news/article/186573">부산 천연가스 발전소 화재 8시간 만에 진화</a>', "html.parser")
    crawler = HtmlCrawler()
    title = crawler._anchor_title(source, soup.a)

    assert crawler._selector_for(source) == "a[href*='/news/article/']"
    assert crawler._is_candidate(source, title, soup.a["href"]) is True


def test_html_crawler_recognizes_kbc_article_links():
    source = Source(
        name="KBC News Monitor",
        source_type=SourceType.html,
        url="https://news.ikbc.co.kr/",
    )
    soup = BeautifulSoup(
        '<a href="/article/view/kbc202605170027">내일도 한낮 30도 이상 초여름 더위 일교차 주의</a>',
        "html.parser",
    )
    crawler = HtmlCrawler()
    title = crawler._anchor_title(source, soup.a)

    assert crawler._selector_for(source) == "a[href*='/article/view/']"
    assert crawler._is_candidate(source, title, soup.a["href"]) is True


def test_detail_author_extractor_prefers_reporter_box_over_site_meta():
    soup = BeautifulSoup(
        """
        <meta name="author" content="KBC광주방송">
        <div class="reporter_info">
          <a href="/article/list/@haitai2000"><span class="name">박석호 기자</span></a>
          <span class="email">haitai2000@ikbc.co.kr</span>
        </div>
        """,
        "html.parser",
    )

    assert _extract_author(soup, "https://news.ikbc.co.kr/article/view/kbc202605170030") == "박석호 기자"


def test_detail_enrichment_refreshes_quality_after_body_and_author(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (
            source_name, source_type, source_category, title, url, fingerprint,
            quality_score, quality_flags
        )
        VALUES (
            'KBC News Monitor', 'html', 'news', 'KBC 상세 보강 품질 테스트',
            'https://news.ikbc.co.kr/article/view/kbc202605170018',
            'kbc-detail-quality', 90, '["no_body_yet"]'
        )
        """
    )
    article_id = conn.execute("SELECT id FROM articles").fetchone()["id"]
    body = "KBC 상세 본문입니다. 기자명 보강 뒤에는 본문 부족 플래그가 사라져야 합니다. " * 5

    class FakeResponse:
        text = f"""
        <html>
          <body>
            <article><p>{body}</p></article>
            <div class="reporter_info">
              <span class="name">임소영 기자</span>
              <span class="email">ysoy@ikbc.co.kr</span>
            </div>
          </body>
        </html>
        """

        def raise_for_status(self):
            return None

    monkeypatch.setattr("app.services.detail_service.settings.raw_html_dir", tmp_path)
    monkeypatch.setattr("app.services.detail_service._respect_domain_delay", lambda url: None)
    monkeypatch.setattr("app.services.detail_service.httpx.get", lambda *args, **kwargs: FakeResponse())

    assert enrich_article_details(conn, article_id) is True

    row = conn.execute("SELECT author, body_text, quality_flags FROM articles WHERE id = ?", (article_id,)).fetchone()
    assert row["author"] == "임소영 기자"
    assert "KBC 상세 본문입니다" in row["body_text"]
    assert "no_body_yet" not in json.loads(row["quality_flags"])


def test_detail_author_extractor_uses_kbc_script_fallbacks():
    soup = BeautifulSoup(
        """
        <meta name="author" content="KBC광주방송">
        <script>
          gtag('config', 'G-6PSXGD0YEP', {"reporterName":"박석호"});
        </script>
        """,
        "html.parser",
    )
    assert _extract_author(soup, "https://news.ikbc.co.kr/article/view/kbc202605170030") == "박석호 기자"


def test_detail_author_extractor_reads_broad_meta_and_script_keys():
    soup = BeautifulSoup(
        """
        <meta name="dable:author" content="KBC광주방송">
        <script>
          window.article = { writerName: "김민정", reporterNm: "김민정", byline: "김민정 기자" };
        </script>
        """,
        "html.parser",
    )
    assert _extract_author(soup, "https://news.example.com/view/1") == "김민정 기자"

    soup = BeautifulSoup(
        """
        <meta name="byl" content="최현정 기자">
        <meta name="author" content="156762057">
        """,
        "html.parser",
    )
    assert _extract_author(soup, "https://news.example.com/view/2") == "최현정 기자"

    soup = BeautifulSoup(
        """
        <meta name="author" content="KBC광주방송">
        <script type="application/ld+json">
          {"@context":"https://schema.org","@type":"NewsArticle","author":{"@type":"Person","name":"박석호"}}
        </script>
        """,
        "html.parser",
    )
    assert _extract_author(soup, "https://news.ikbc.co.kr/article/view/kbc202605170030") == "박석호 기자"


def test_detail_author_extractor_reads_private_broadcast_layouts():
    cases = [
        (
            "https://news.tvchosun.com/site/data/html_dir/2026/05/19/2026051990016.html",
            """
            <meta property="dable:author" content="정은혜 기자">
            <div class="view-title">
              <div class="editor">
                <div class="img-box"><img alt="정은혜 기자"></div>
                <div class="name-box"><a class="reporter">정은혜 기자</a></div>
              </div>
            </div>
            <script>
              var _author_info = new Array();
              _author_info.push({"name":"정은혜 기자", "email":"jung.eunhye@chosun.com"});
            </script>
            """,
            "정은혜 기자",
        ),
        (
            "https://news.knn.co.kr/news/article/186963",
            '<div class="info">최혁규 입력 : 2026.04.24 18:01</div>',
            "최혁규 기자",
        ),
        (
            "https://www.tbc.co.kr/news/view?id=206479",
            '<div class="reporter_wrap">김용우 기자 (bywoo31@tbc.co.kr) 2026년 05월 17일</div>',
            "김용우 기자",
        ),
        (
            "https://www.jtv.co.kr/2021/?c=3/45&uid=2202292",
            '<div class="reporter">김민지 기자 (mzk19@jtv.co.kr)</div>',
            "김민지 기자",
        ),
        (
            "https://www.g1tv.co.kr/news/?newsid=343587",
            '<div class="reporter">김윤지 기자[ yunzy@g1tv.co.kr ]</div>',
            "김윤지 기자",
        ),
        (
            "https://www.ubc.co.kr/wp/archives/127568",
            '<span class="author">admin</span><div class="entry-content">-2026/05/15 윤주웅 기자</div>',
            "윤주웅 기자",
        ),
        (
            "https://www.cjb.co.kr/home/sub.php?menukey=61&mod=view&P_NO=260517006",
            '<div class="board-view">작성자 박언 작성일 2026-05-17 조회수 11</div>',
            "박언 기자",
        ),
        (
            "https://www.jibs.co.kr/news/articles/articlesDetail/61277",
            '<div class="articles-detail-attach-right">JIBS 제주방송 강석창( ksc064@naver.com ) 기자</div>',
            "강석창 기자",
        ),
    ]

    for url, html, expected in cases:
        soup = BeautifulSoup(html, "html.parser")
        assert _extract_author(soup, url) == expected


def test_auto_detail_enrichment_includes_tv_chosun():
    source = Source(
        name="TV Chosun News Monitor",
        source_type=SourceType.html,
        url="https://news.tvchosun.com/",
    )

    assert _should_auto_enrich_details(source, {"author": None, "body_text": None}, 0) is True
    assert (
        _should_auto_enrich_details(
            source,
            {"author": "정은혜 기자", "body_text": "본문이 충분한 기사입니다. " * 10},
            0,
        )
        is False
    )


def test_detail_body_extractor_reads_kbc_body_wrap_content():
    soup = BeautifulSoup(
        """
        <div class="body-wrap" id="body_wrap">
          <div class="content">
            <figure><img src="/photo.jpg"></figure>
            아랍에미리트 바라카 원전 단지가 드론 공격을 받아 화재가 발생했습니다.
            <script>window.ad = true;</script>
            아부다비 정부 공보청은 긴급 대응했으며 방사능 안전 수준에 영향이 없다고 밝혔습니다.
          </div>
        </div>
        """,
        "html.parser",
    )

    text = _extract_body_text(soup)
    assert "드론 공격" in text
    assert "window.ad" not in text


def test_detail_body_extractor_prefers_tv_chosun_text_box():
    soup = BeautifulSoup(
        """
        <article id="ui_contents">
          <div class="view-title">
            <h2 class="title">월드컵 코앞인데 또 총기 참사</h2>
            <div class="editor"><a class="reporter">정은혜 기자</a></div>
          </div>
          <div class="item-main">
            <div class="contents">
              <div class="text-box">
                <p>2026 북중미 월드컵이 한 달도 남지 않은 가운데 멕시코에서 대형 총기 사건이 발생했다.
                수사 당국은 이번 범행의 정확한 경위를 조사하고 있다.
                치안 불안이 커지면서 현지 정부가 대응을 강화하고 있다.</p>
              </div>
              <div class="copyrights">Copyrights ⓒ TV조선. 무단전재 및 재배포 금지</div>
            </div>
          </div>
        </article>
        """,
        "html.parser",
    )

    text = _extract_body_text(soup)

    assert text.startswith("2026 북중미 월드컵")
    assert "정은혜 기자" not in text
    assert "Copyrights" not in text


def test_detail_body_extractor_uses_jsonld_article_body():
    soup = BeautifulSoup(
        """
        <script type="application/ld+json">
          {
            "@context":"https://schema.org",
            "@type":"NewsArticle",
            "articleBody":"광주 도심에서 대형 화재가 발생해 소방당국이 대응 1단계를 발령했습니다. 현장에는 장비 수십 대와 인력이 투입됐고, 인근 주민에게는 안전 안내 문자가 발송됐습니다. 경찰과 소방은 정확한 화재 원인을 조사하고 있습니다."
          }
        </script>
        """,
        "html.parser",
    )

    text = _extract_body_text(soup)
    assert "대응 1단계" in text
    assert "정확한 화재 원인" in text


def test_detail_body_extractor_scores_article_over_navigation_noise():
    soup = BeautifulSoup(
        """
        <article>
          <nav>로그인 회원가입 검색 메뉴 목록 공유 댓글 구독</nav>
          <div class="article-content">
            <p>부산의 한 공장에서 폭발 사고가 발생해 작업자들이 긴급 대피했습니다.</p>
            <p>소방당국은 현장에 구조대를 투입했고 추가 피해 여부를 확인하고 있습니다.</p>
            <p>관계기관은 안전 조치를 마치는 대로 정확한 사고 경위를 조사할 방침입니다.</p>
          </div>
          <div class="related">관련기사 추천기사 인기기사 많이 본 기사</div>
        </article>
        """,
        "html.parser",
    )

    text = _extract_body_text(soup)
    assert "작업자들이 긴급 대피" in text
    assert "로그인 회원가입" not in text
    assert "관련기사" not in text


def test_detail_body_extractor_prefers_clean_daum_article_body():
    soup = BeautifulSoup(
        """
        <article id="mArticle">
          <div class="head_view">
            <h3>백석대 건학 50주년 기념 전국태권도대회 개막</h3>
            <div class="util_wrap">음성재생 설정 이동 통신망에서 음성 재생 시 데이터 요금이 발생할 수 있습니다. 번역 beta 글자크기 설정</div>
          </div>
          <div class="news_view">
            <strong class="summary_view">TJB 아침뉴스</strong>
            <div class="article_view">
              <section dmcf-sid="4RoFtroMFz">
                <p dmcf-pid="a" dmcf-ptype="general">천안 백석대학교가 건학 50주년을 맞아 마련한 총장배 전국태권도 대회가 개막했습니다.</p>
                <p dmcf-pid="b" dmcf-ptype="general">이번 대회에는 엘리트 선수와 생활체육 동호인 등 5천5백여 명이 참가합니다.</p>
                <p dmcf-pid="c" dmcf-ptype="general">품새와 겨루기, 격파 종목을 통합 운영하는 첫 대회로 치러집니다.</p>
                <p dmcf-pid="d" dmcf-ptype="general">이선학 취재 기자 | shlee@tjb.co.kr</p>
              </section>
            </div>
            <p>Copyright © TJB</p>
          </div>
        </article>
        """,
        "html.parser",
    )

    text = _extract_body_text(soup)
    assert text.startswith("천안 백석대학교")
    assert "음성재생 설정" not in text
    assert "이선학 취재 기자" not in text
    assert "Copyright" not in text


def test_detail_body_extractor_prefers_sbs_article_body_over_side_noise():
    soup = BeautifulSoup(
        """
        <html>
          <body>
            <aside>
              <p class="notice">최근 24시간 이내 속보 및 알림을 표시합니다.</p>
              <p>댓글 매너봇 공유 구독 관련기사 인기기사 많이 본 기사</p>
            </aside>
            <div class="w_article">
              <div class="main_text">
                <div class="text_area" itemprop="articleBody">
                  <p>충남 당진의 한 도로에서 SUV 차량에 불이 나 운전자가 숨졌습니다.</p>
                  <p>신고를 받고 출동한 소방은 장비와 인력을 투입해 약 20분 만에 불을 껐습니다.</p>
                  <p>소방과 경찰은 정확한 화재 원인과 사고 경위를 조사하고 있습니다.</p>
                </div>
                <div class="copyrightsbs" data-nosnippet>Copyright Ⓒ SBS. All rights reserved. 무단 전재, 재배포 금지</div>
              </div>
            </div>
          </body>
        </html>
        """,
        "html.parser",
    )

    text = _extract_body_text(soup)
    assert text.startswith("충남 당진")
    assert "소방과 경찰" in text
    assert "최근 24시간" not in text
    assert "댓글 매너봇" not in text
    assert "Copyright" not in text


def test_html_crawler_recognizes_private_broadcast_article_links():
    cases = [
        (
            Source(name="TBC News Monitor", source_type=SourceType.html, url="https://www.tbc.co.kr/news"),
            '<a href="/news/view?c1=&c2=&pno=20260517105837AE09874&id=206479">군위서도 국힘 탈당 김부겸 지지 선언</a>',
            "a[href*='/news/view']",
        ),
        (
            Source(name="TJB News Monitor", source_type=SourceType.html, url="https://www.tjb.co.kr/sub0307"),
            '<a href="/sub0307/issue/view/id/96329">대전시청에 안전공업 화재 참사 합동분향소 차려져</a>',
            "a[href*='/issue/view/id/'], a[href*='/category/view/id/']",
        ),
        (
            Source(name="JTV News Monitor", source_type=SourceType.html, url="https://www.jtv.co.kr/2021/?c=3/45"),
            '<a href="/2021/?c=3/45&amp;uid=2202301">전북 지역 현안 점검 보도</a>',
            "a[href*='uid=']",
        ),
        (
            Source(name="G1 News Monitor", source_type=SourceType.html, url="https://www.g1tv.co.kr/news/?mid=1_2"),
            '<a href="/news/?mid=1_207_6&amp;newsid=343587&amp;newscode=010700">강원 지방선거 후보자 등록 마감</a>',
            "a[href*='newsid=']",
        ),
        (
            Source(name="UBC News Monitor", source_type=SourceType.html, url="https://www.ubc.co.kr/wp/archives/category/news_list"),
            '<a href="https://www.ubc.co.kr/wp/archives/127568">울산 산업 현장 안전 점검</a>',
            "a[href*='/wp/archives/']",
        ),
        (
            Source(name="CJB News Monitor", source_type=SourceType.html, url="https://www.cjb.co.kr/home/sub.php?menukey=61"),
            '<a href="sub.php?menukey=61&amp;mod=view&amp;P_NO=260517006&amp;PRO_CODE=4&amp;scode=99999999">충북 고유가 피해지원금 지급 시작</a>',
            "a[href*='mod=view'][href*='P_NO=']",
        ),
    ]

    crawler = HtmlCrawler()
    for source, html, selector in cases:
        soup = BeautifulSoup(html, "html.parser")
        title = crawler._anchor_title(source, soup.a)
        href = crawler._article_href(source, soup.a["href"])
        assert crawler._selector_for(source) == selector
        assert crawler._is_candidate(source, title, href) is True


def test_html_crawler_uses_ubc_entry_title_for_read_more_links():
    source = Source(
        name="UBC News Monitor",
        source_type=SourceType.html,
        url="https://www.ubc.co.kr/wp/archives/category/news_list",
    )
    soup = BeautifulSoup(
        """
        <article>
          <h1 class="entry-title">
            <a href="https://www.ubc.co.kr/wp/archives/127635">울산 부동산시장 소비심리지수 두 달 연속 하락</a>
          </h1>
          <div class="entry-content">
            <p>윤주웅</p>
            <a class="read-more" href="https://www.ubc.co.kr/wp/archives/127635 ">Read More</a>
          </div>
        </article>
        """,
        "html.parser",
    )
    crawler = HtmlCrawler()
    link = soup.select_one(".read-more")

    href = crawler._article_href(source, link["href"])
    title = crawler._anchor_title(source, link)

    assert href == "https://www.ubc.co.kr/wp/archives/127635"
    assert title == "울산 부동산시장 소비심리지수 두 달 연속 하락"
    assert crawler._is_candidate(source, title, href) is True


def test_html_crawler_prefers_kbs_visible_title_over_generic_aria_label():
    source = Source(
        name="KBS News Korean Monitor",
        source_type=SourceType.html,
        url="https://news.kbs.co.kr/news/pc/main/main.html",
    )
    soup = BeautifulSoup(
        """
        <a href="/news/pc/view/view.do?ncd=8564025" aria-label="이 시각 추천 뉴스 링크">
          “신입 대신 50~60대”…AI 일자리 변동 ‘시작’
        </a>
        """,
        "html.parser",
    )
    crawler = HtmlCrawler()

    title = crawler._anchor_title(source, soup.a)

    assert title == "“신입 대신 50~60대”…AI 일자리 변동 ‘시작’"
    assert crawler._is_candidate(source, title, soup.a["href"]) is True


def test_html_crawler_keeps_legitimate_news_titles_with_navigation_words():
    source = Source(name="News Monitor", source_type=SourceType.html, url="https://example.com/news")
    soup = BeautifulSoup(
        '<a href="/article/1">개인정보 유출 과징금 기준 강화</a>'
        '<a href="/article/2">다음 달 산후조리원 운영 시작</a>',
        "html.parser",
    )
    crawler = HtmlCrawler()

    for link in soup.select("a"):
        title = crawler._anchor_title(source, link)
        assert crawler._is_candidate(source, title, link["href"]) is True


def test_html_crawler_keeps_jibs_slide_titles_matched_to_each_anchor():
    source = Source(
        name="JIBS News Monitor",
        source_type=SourceType.html,
        url="https://www.jibs.co.kr/news/articles/viewArticles",
    )
    soup = BeautifulSoup(
        """
        <div class="newsarticle-div">
          <div class="item">
            <a href="javascript:goArticlesDetailPage(61336);">
              <img alt="image" src="headline.png" />
            </a>
            <div class="newsMainHeadLineTitle">"내가 해냈다" 여당 후보들 자랑했던 성산 해양치유센터</div>
          </div>
          <div class="news-top-slide-nav">
            <a href="javascript:goArticlesDetailPage(61336);">
              <div class="newsMainHeadLineTitle">"내가 해냈다" 여당 후보들 자랑했던 성산 해양치유센터</div>
            </a>
            <a href="javascript:goArticlesDetailPage(61335);">
              <div class="newsMainHeadLineTitle">새벽에도 시속 30km 스쿨존...24시간 규제 드디어 손 본다</div>
            </a>
          </div>
        </div>
        """,
        "html.parser",
    )
    crawler = HtmlCrawler()
    image_link = soup.select_one(".item a")
    nav_links = soup.select(".news-top-slide-nav a")

    assert crawler._anchor_title(source, image_link) == '"내가 해냈다" 여당 후보들 자랑했던 성산 해양치유센터'
    assert crawler._anchor_title(source, nav_links[0]) == '"내가 해냈다" 여당 후보들 자랑했던 성산 해양치유센터'
    assert crawler._anchor_title(source, nav_links[1]) == "새벽에도 시속 30km 스쿨존...24시간 규제 드디어 손 본다"


def test_daum_channel_crawler_maps_tjb_channel_items(monkeypatch):
    source = Source(
        name="TJB News Monitor",
        source_type=SourceType.api,
        url="https://hades-cerberus.v.daum.net/charon/media_home_news_all/data?cpId=551724&size=30",
    )

    def fake_fetch(url, headers, source):
        assert "cpId=551724" in url
        assert headers["Referer"] == "https://v.daum.net/channel/551724/list"
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "createDt": 1778973158466,
                        "title": "30년 사실혼 남성 흉기로 33차례 찔러 살해한 60대 여성 징역 25년",
                        "pcLink": "https://v.daum.net/v/20260517081238175",
                        "thumbnail": "https://t1.daumcdn.net/news/tjb.jpg",
                    }
                ]
            },
            request=request,
        )

    monkeypatch.setattr("app.crawlers.daum_channel.fetch_with_retry", fake_fetch)

    articles = DaumChannelCrawler().crawl(source)

    assert len(articles) == 1
    assert articles[0].source_name == "TJB News Monitor"
    assert articles[0].source_type == "api"
    assert articles[0].url == "https://v.daum.net/v/20260517081238175"
    assert articles[0].image_urls == ["https://t1.daumcdn.net/news/tjb.jpg"]


def test_jtbc_api_crawler_maps_current_article_items(monkeypatch):
    source = Source(
        name="JTBC News Monitor",
        source_type=SourceType.api,
        url="https://news-api.jtbc.co.kr/v1/get/contents/section/list/articles?pageNo=1&pageSize=20&articleListType=ARTICLE",
    )

    def fake_fetch(url, headers, source):
        assert "news-api.jtbc.co.kr" in url
        assert headers["Referer"] == "https://news.jtbc.co.kr/sections"
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            json={
                "resultCode": "00",
                "data": {
                    "list": [
                        {
                            "articleIdx": "NB12298841",
                            "articleTitle": "이란, 새 종전안 미국에 전달",
                            "articleInnerTextContent": "이란이 새 종전안을 미국 측에 전달했다는 보도가 나왔습니다.",
                            "articleThumbnailImgUrl": "https://thumb.jtbc.co.kr/photo.jpg",
                            "publicationDate": "2026-05-19T00:24",
                            "journalistName": "홍지용",
                        }
                    ]
                },
            },
            request=request,
        )

    monkeypatch.setattr("app.crawlers.jtbc_api.fetch_with_retry", fake_fetch)

    articles = JtbcApiCrawler().crawl(source)

    assert len(articles) == 1
    assert articles[0].url == "https://news.jtbc.co.kr/article/NB12298841"
    assert articles[0].canonical_url == "https://news.jtbc.co.kr/article/NB12298841"
    assert articles[0].source_type == "api"
    assert articles[0].author == "홍지용 기자"
    assert "새 종전안" in articles[0].body_text
    assert articles[0].image_urls == ["https://thumb.jtbc.co.kr/photo.jpg"]


def test_html_crawler_converts_jibs_javascript_article_links():
    source = Source(
        name="JIBS News Monitor",
        source_type=SourceType.html,
        url="https://www.jibs.co.kr/news/articles/viewArticles",
    )
    soup = BeautifulSoup(
        """
        <div class="item">
          <a href="javascript:goArticlesDetailPage(61277);"><img alt="image" src="a.png"></a>
          <div class="newsMainImgTitle newsMainImageTitleDot">안창호 인권위원장 5.18 기념식 불참</div>
        </div>
        """,
        "html.parser",
    )
    crawler = HtmlCrawler()
    href = crawler._article_href(source, soup.a["href"])
    title = crawler._anchor_title(source, soup.a)

    assert href == "/news/articles/articlesDetail/61277"
    assert crawler._selector_for(source) == "a[href^='javascript:goArticlesDetailPage']"
    assert title == "안창호 인권위원장 5.18 기념식 불참"
    assert crawler._is_candidate(source, title, href) is True


def test_safe_korea_crawler_maps_disaster_message():
    source = Source(
        name="Safe Korea Disaster Messages",
        source_type=SourceType.api,
        source_category="disaster",
        url="https://www.safekorea.go.kr/safekorea-kor/nas-files/sms/MAINCALAMITYSMS.json",
    )
    article = SafeKoreaDisasterMessageCrawler()._article_from_message(
        source,
        {
            "smsTrsmSn": 258075,
            "creatDt": "2026/04/21 10:46:25",
            "msgCn": "오늘 10:38 김포시 공장 화재 발생. 주민은 이동하시고 차량은 우회하세요. [김포시]",
            "emrgncyStepNm": "안전안내",
            "dsstrSeNm": "화재",
            "rcvAreaNm": "경기도 김포시 ",
            "delYn": "N",
        },
    )

    assert article is not None
    assert article.title == "안전안내 · 화재 - 경기도 김포시"
    assert article.source_type == "api"
    assert article.published_at.isoformat() == "2026-04-21T10:46:25"
    assert "화재 발생" in article.summary
    assert article.region_tags == ["경기도 김포시"]
    assert article.url.endswith("bbsOrdr=258075")


def test_quality_keeps_explicit_source_regions():
    article = Article(
        source_name="Safe Korea Disaster Messages",
        source_type="api",
        source_category="disaster",
        title="안전안내 · 산불 - 경상북도 봉화군",
        url="https://example.com",
        fingerprint="safe-korea",
        summary="영농부산물 소각 금지 바랍니다.",
        region_tags=["경상북도 봉화군"],
    )

    enriched = enrich_article_quality(article)
    assert enriched.region_tags == ["경상북도 봉화군"]
    assert "no_region" not in enriched.quality_flags


def test_quality_does_not_treat_news_words_as_navigation():
    titles = [
        "개인정보 유출 과징금 기준 강화",
        "다음 달 서울형 산후조리원 운영 시작",
        "이전 정부 정책 감사 결과 발표",
    ]

    for title in titles:
        article = Article(
            source_name="News Source",
            source_type="rss",
            source_category="news",
            title=title,
            url="https://example.com/article",
            fingerprint=title,
            summary="정상 기사입니다.",
        )
        enriched = enrich_article_quality(article)
        assert "navigation_like_title" not in enriched.quality_flags


def test_quality_still_flags_navigation_titles():
    article = Article(
        source_name="MOIS Press Releases",
        source_type="html",
        source_category="disaster",
        title="예산현황 페이지로 이동",
        url="https://www.mois.go.kr/frt/sub/nav",
        fingerprint="nav-title",
    )

    enriched = enrich_article_quality(article)
    assert "navigation_like_title" in enriched.quality_flags


def test_region_extraction_avoids_false_positive_compound_words():
    assert "부산" not in extract_regions("영농부산물 소각 금지 바랍니다.")
    assert "부산" in extract_regions("부산시 해운대구 화재 발생")


def test_region_group_filter_and_counts():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, region_tags, published_at)
        VALUES ('source', 'api', ?, ?, ?, ?, ?)
        """,
        [
            ("김포 공장 화재", "https://capital", "capital", '["경기도 김포시"]', "2026-05-13T19:00:00+09:00"),
            ("봉화 산불 안내", "https://yeongnam", "yeongnam", '["경상북도 봉화군"]', "2026-05-13T18:00:00+09:00"),
        ],
    )

    rows = list_articles(conn, region_group="capital", sort="latest")
    assert [row["title"] for row in rows] == ["김포 공장 화재"]
    assert count_articles(conn, region_group="capital") == 1

    counts = {row["region_group"]: row["count"] for row in list_region_counts(conn)}
    assert counts["capital"] == 1
    assert counts["yeongnam"] == 1


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


def test_reporter_advice_prioritizes_emergency_article():
    advice = reporter_advice_for_article(
        {
            "id": 1,
            "title": "서울 공장 화재로 2명 사망",
            "source_name": "National Fire Agency Press",
            "source_category": "fire",
            "importance_score": 4,
            "region_tags": '["서울"]',
            "cluster_source_count": 2,
        }
    )

    assert advice["decision"] == "즉시 취재"
    assert advice["checklist"]["장소 확인"] is True
    assert advice["verification_score"] >= 4
    assert any(item["label"] == "화재" for item in advice["score_details"])
    assert "소방" in advice["cuesheet"]["contact_hint"]


def test_reporter_cluster_compare_flags_number_conflicts():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, duplicate_group_id)
        VALUES (?, 'rss', ?, ?, ?, 'cluster-a')
        """,
        [
            ("A", "사고로 1명 사망", "https://a", "a"),
            ("B", "사고로 2명 사상", "https://b", "b"),
        ],
    )

    result = compare_cluster_for_reporter(conn, "cluster-a")
    assert result["article_count"] == 2
    assert result["conflicts"]

    [risk] = false_risk_dashboard(conn)
    assert risk["duplicate_group_id"] == "cluster-a"
    assert risk["conflict_count"] >= 1


def test_suggested_alert_rules_include_region_rules():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (source_name, source_type, title, url, fingerprint, region_tags)
        VALUES ('source', 'api', '김포 화재', 'https://x', 'x', '["경기도 김포시"]')
        """
    )

    rules = suggested_alert_rules(conn)
    assert any("긴급 재난" == rule["name"] for rule in rules)
    assert any("경기도 김포시" in rule["name"] for rule in rules)


def test_suggested_ready_queue_keeps_human_approval_step():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO articles (
            source_name, source_type, source_category, title, url, fingerprint,
            importance_score, newsroom_status, region_tags, image_urls
        )
        VALUES (
            'National Fire Agency Press', 'html', 'fire', '서울 공장 화재 2명 사망',
            'https://fire', 'fire', 5, 'new', '["서울"]', '["https://image"]'
        )
        """
    )

    [candidate] = suggested_ready_queue(conn)
    assert candidate["decision"] == "즉시 취재"
    assert candidate["id"]


def test_command_center_reports_operational_signals():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO sources (name, source_type, source_category, url, crawl_interval_seconds)
        VALUES ('Fire Source', 'html', 'fire', 'https://fire', 300)
        """
    )
    conn.execute(
        """
        INSERT INTO crawl_runs (source_name, started_at, finished_at, status, new_article_count)
        VALUES ('Fire Source', datetime('now', '-5 minutes'), datetime('now', '-4 minutes'), 'success', 3)
        """
    )
    conn.execute(
        """
        INSERT INTO articles (
            source_name, source_type, source_category, title, url, fingerprint,
            collected_at, region_tags, verification_status
        )
        VALUES (
            'Fire Source', 'html', 'fire', '서울 화재 발생', 'https://fire/a', 'a',
            datetime('now', '-10 minutes'), '["서울"]', 'needs_review'
        )
        """
    )

    center = build_command_center(conn)
    assert center["operational_score"] > 0
    assert center["sla"]["freshness_score"] == 100
    assert center["playbooks"]
    assert "blind_spots" in center


def test_contact_log_schema_is_available():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO contact_logs (article_id, contact_target, response_note, next_check_at)
        VALUES (1, '소방서', '확인 중', '2026-05-17T14:00')
        """
    )
    row = conn.execute("SELECT * FROM contact_logs WHERE article_id = 1").fetchone()
    assert row["contact_target"] == "소방서"
