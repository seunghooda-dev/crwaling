import json
from pathlib import Path

from app.models import Source, SourceType
from app.seed import DEFAULT_SOURCES


def load_sources(path: Path = Path("config/sources.json")) -> list[Source]:
    if not path.exists():
        return DEFAULT_SOURCES

    payload = json.loads(path.read_text(encoding="utf-8"))
    sources: list[Source] = []
    for item in payload:
        sources.append(
            Source(
                name=item["name"],
                source_type=SourceType(item["source_type"]),
                url=item["url"],
                source_category=str(item.get("source_category", "news")),
                enabled=bool(item.get("enabled", True)),
                crawl_interval_seconds=int(item.get("crawl_interval_seconds", 300)),
            )
        )
    return sources
