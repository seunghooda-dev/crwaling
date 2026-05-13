from app.models import Source, SourceType


DEFAULT_SOURCES = [
    Source(
        name="Yonhap English RSS",
        source_type=SourceType.rss,
        url="https://en.yna.co.kr/RSS/news.xml",
        crawl_interval_seconds=300,
    ),
    Source(
        name="BBC World RSS",
        source_type=SourceType.rss,
        url="https://feeds.bbci.co.uk/news/world/rss.xml",
        crawl_interval_seconds=300,
    ),
]

