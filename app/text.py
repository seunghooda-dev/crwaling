import hashlib
import json
import re
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SPACE_RE = re.compile(r"\s+")
UNICODE_ESCAPE_RE = re.compile(r"(?:\\u[0-9a-fA-F]{4})+")
BROKEN_ENTITY_REPLACEMENTS = (
    (re.compile(r"(^|[^&])hellip;", re.IGNORECASE), r"\1..."),
    (re.compile(r"(^|[^&])nbsp;", re.IGNORECASE), r"\1 "),
    (re.compile(r"(^|[^&])quot;", re.IGNORECASE), r'\1"'),
    (re.compile(r"(^|[^&])apos;", re.IGNORECASE), r"\1'"),
    (re.compile(r"(^|[^&])#039;", re.IGNORECASE), r"\1'"),
    (re.compile(r"(^|[^&])ldquo;", re.IGNORECASE), r'\1"'),
    (re.compile(r"(^|[^&])rdquo;", re.IGNORECASE), r'\1"'),
    (re.compile(r"(^|[^&])lsquo;", re.IGNORECASE), r"\1'"),
    (re.compile(r"(^|[^&])rsquo;", re.IGNORECASE), r"\1'"),
    (re.compile(r"(^|[^&])middot;", re.IGNORECASE), r"\1·"),
)
TRACKING_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "igshid",
    "ref",
    "ref_src",
    "rss",
    "plink",
    "cooper",
}


def normalize_text(value: str | None) -> str:
    text = str(value or "")
    for pattern, replacement in BROKEN_ENTITY_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    text = UNICODE_ESCAPE_RE.sub(_decode_unicode_escape, text)
    return unescape(text).replace("\xa0", " ")


def _decode_unicode_escape(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        decoded = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw
    return decoded if isinstance(decoded, str) else raw


def normalize_space(value: str | None) -> str:
    return SPACE_RE.sub(" ", normalize_text(value)).strip()


def normalize_article_url(url: str) -> str:
    url = str(url or "").strip()
    url = re.sub(r"(?:%20|\s)+$", "", url, flags=re.IGNORECASE)
    parsed = urlsplit(url)
    clean_path = re.sub(r";jsessionid=[^/?#]+", "", parsed.path, flags=re.IGNORECASE)
    if parsed.netloc.lower() == "news.jtbc.co.kr" and parsed.path.lower() == "/article/article.aspx":
        query = parse_qsl(parsed.query, keep_blank_values=True)
        news_id = next((value for key, value in query if key.lower() == "news_id"), "")
        if re.fullmatch(r"NB\d+", news_id, re.IGNORECASE):
            return urlunsplit((parsed.scheme or "https", parsed.netloc.lower(), f"/article/{news_id.upper()}", "", ""))
    if clean_path != parsed.path:
        return urlunsplit((parsed.scheme, parsed.netloc, clean_path, parsed.query, ""))
    return url


def canonicalize_url(url: str) -> str:
    url = normalize_article_url(url)
    parsed = urlsplit(url)
    netloc = parsed.netloc.lower()
    clean_path = re.sub(r"(?:%20|\s)+$", "", parsed.path, flags=re.IGNORECASE)
    clean_path = re.sub(r"[?&](?:ref|rss)=rss$", "", clean_path, flags=re.IGNORECASE)
    query_items = _canonical_query_items(netloc, clean_path, parsed.query)
    clean_query = urlencode(query_items, doseq=True)
    return urlunsplit((parsed.scheme, netloc, clean_path, clean_query, ""))


def _canonical_query_items(netloc: str, path: str, query: str) -> list[tuple[str, str]]:
    items = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_PARAMS and not key.lower().startswith("utm_")
    ]
    by_key = {key.lower(): (key, value) for key, value in items}

    if "g1tv.co.kr" in netloc and path.rstrip("/") == "/news" and "newsid" in by_key:
        return [("newsid", by_key["newsid"][1])]
    if "tbc.co.kr" in netloc and path.rstrip("/") == "/news/view":
        selected = []
        for key in ("id", "pno"):
            if key in by_key:
                selected.append((key, by_key[key][1]))
        if selected:
            return selected
    if "mois.go.kr" in netloc and path.endswith("/commonSelectBoardArticle.do"):
        selected = []
        for key in ("bbsid", "nttid"):
            if key in by_key:
                selected.append((by_key[key][0], by_key[key][1]))
        if selected:
            return selected
    if "police.go.kr" in netloc and path.endswith("/BD_selectBbs.do"):
        selected = []
        for key in ("q_bbscode", "q_bbscttsn"):
            if key in by_key:
                selected.append((by_key[key][0], by_key[key][1]))
        if selected:
            return selected

    return sorted(items, key=lambda item: (item[0].lower(), item[1]))


def article_fingerprint(title: str, source_name: str, published_date: str | None = None) -> str:
    basis = normalize_space(f"{title}|{source_name}|{published_date or ''}").lower()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
