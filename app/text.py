import hashlib
import re
from urllib.parse import urlsplit, urlunsplit


SPACE_RE = re.compile(r"\s+")


def normalize_space(value: str) -> str:
    return SPACE_RE.sub(" ", value).strip()


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url)
    clean_query = "&".join(
        part for part in parsed.query.split("&") if part and not part.startswith(("utm_", "fbclid=", "gclid="))
    )
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, clean_query, ""))


def article_fingerprint(title: str, source_name: str, published_date: str | None = None) -> str:
    basis = normalize_space(f"{title}|{source_name}|{published_date or ''}").lower()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()

