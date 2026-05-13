import hashlib
import re
from difflib import SequenceMatcher

from app.repository import iter_articles, update_article_cluster
from app.text import normalize_space


STOPWORDS = {
    "속보",
    "단독",
    "영상",
    "자막뉴스",
    "종합",
    "브리핑",
    "관련",
    "발표",
}


def rebuild_clusters(conn, limit: int | None = None) -> int:
    articles = iter_articles(conn, limit=limit)
    buckets: dict[str, list[dict]] = {}
    for article in articles:
        key = _date_key(article)
        buckets.setdefault(key, []).append(article)

    changed = 0
    for bucket in buckets.values():
        representatives: list[tuple[str, str]] = []
        for article in bucket:
            normalized = normalize_title(article["title"])
            cluster_id = None
            for rep_title, rep_cluster in representatives:
                if SequenceMatcher(None, normalized, rep_title).ratio() >= 0.68:
                    cluster_id = rep_cluster
                    break
            if cluster_id is None:
                cluster_id = _cluster_id(normalized, article.get("published_at") or article.get("collected_at") or "")
                representatives.append((normalized, cluster_id))
            update_article_cluster(conn, article["id"], cluster_id)
            changed += 1
    conn.commit()
    return changed


def normalize_title(title: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", title)
    text = re.sub(r"['\"“”‘’…·,.:;!?()]", " ", text)
    tokens = [token for token in normalize_space(text).split(" ") if token and token not in STOPWORDS]
    return " ".join(tokens).lower()


def _date_key(article: dict) -> str:
    value = article.get("published_at") or article.get("collected_at") or ""
    return str(value)[:10]


def _cluster_id(normalized_title: str, date_value: str) -> str:
    basis = f"{str(date_value)[:10]}|{normalized_title[:80]}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
