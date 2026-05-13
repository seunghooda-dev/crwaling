from abc import ABC, abstractmethod

from app.models import Article, Source


class Crawler(ABC):
    @abstractmethod
    def crawl(self, source: Source) -> list[Article]:
        """Collect articles from a source."""

