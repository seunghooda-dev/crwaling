from datetime import datetime

from app.crawlers.base import Crawler
from app.crawlers.http import crawler_headers, fetch_with_retry
from app.models import Article, Source
from app.text import article_fingerprint, canonicalize_url, normalize_space


SAFE_KOREA_DETAIL_URL = "https://www.safekorea.go.kr/safekorea-kor/em/emergency/emergency_er1_d.html"


class SafeKoreaDisasterMessageCrawler(Crawler):
    def crawl(self, source: Source) -> list[Article]:
        headers = crawler_headers(str(source.url), "application/json, text/plain, */*")
        response = fetch_with_retry(str(source.url), headers, source)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []

        articles = []
        for item in payload:
            if isinstance(item, dict) and item.get("delYn") != "Y":
                article = self._article_from_message(source, item)
                if article:
                    articles.append(article)
        return articles[:200]

    def _article_from_message(self, source: Source, item: dict) -> Article | None:
        message_id = str(item.get("smsTrsmSn") or "").strip()
        message = normalize_space(str(item.get("msgCn") or ""))
        if not message_id or not message:
            return None

        published_at = self._parse_datetime(item.get("creatDt"))
        published_date = published_at.date().isoformat() if published_at else None
        disaster_type = normalize_space(str(item.get("dsstrSeNm") or "재난"))
        step = normalize_space(str(item.get("emrgncyStepNm") or "재난문자"))
        regions = self._regions(item)
        region_label = regions[0] if regions else self._region_from_message(message)
        title = self._title(disaster_type, step, region_label, message)
        url = f"{SAFE_KOREA_DETAIL_URL}?bbsOrdr={message_id}"
        keywords = [value for value in {disaster_type, step, "재난문자"} if value and value != "기타"]

        return Article(
            source_name=source.name,
            source_type=source.source_type.value,
            source_category=source.source_category,
            title=title,
            url=url,
            canonical_url=canonicalize_url(url),
            published_at=published_at,
            body_text=message,
            summary=message,
            category=disaster_type,
            keywords=keywords,
            region_tags=regions,
            fingerprint=article_fingerprint(message_id, source.name, published_date),
            duplicate_group_id=article_fingerprint(message, "global", published_date)[:16],
        )

    def _parse_datetime(self, value: object) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y/%m/%d %H:%M:%S")
        except ValueError:
            return None

    def _regions(self, item: dict) -> list[str]:
        raw = str(item.get("rcvAreaNm") or "")
        regions = [normalize_space(region) for region in raw.split(",")]
        return [region for region in regions if region]

    def _region_from_message(self, message: str) -> str:
        if "[" not in message or "]" not in message:
            return ""
        return normalize_space(message.rsplit("[", 1)[-1].split("]", 1)[0])

    def _title(self, disaster_type: str, step: str, region: str, message: str) -> str:
        label_parts = [part for part in (step, disaster_type) if part and part != "기타"]
        label = " · ".join(label_parts) or "재난문자"
        prefix = f"{label} - {region}" if region else label
        return normalize_space(prefix if len(prefix) >= 10 else f"{prefix} {message[:60]}")
