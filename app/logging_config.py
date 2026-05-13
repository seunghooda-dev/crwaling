import logging
from logging.handlers import RotatingFileHandler

from app.config import settings


def configure_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        return

    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    app_handler = RotatingFileHandler(
        settings.log_dir / "app.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    crawler_logger = logging.getLogger("crawler")
    crawler_handler = RotatingFileHandler(
        settings.log_dir / "crawler.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    crawler_handler.setFormatter(formatter)
    crawler_logger.addHandler(crawler_handler)
