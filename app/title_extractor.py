from html import unescape
import re

from app.text import normalize_space


BODY_MARKERS = (
    " [앵커]",
    " [기자]",
    " ◇ ",
    " ◆ ",
    " 기자 ",
    " 특파원 ",
)

DATE_SUFFIX_PATTERN = re.compile(r"\s+\d{4}\.\d{2}\.\d{2}\s+\(\d{2}:\d{2}\)\s*$")
END_SENTENCE_PATTERN = re.compile(r"(?<=[.!?。！？])\s+")
KOREAN_SENTENCE_PATTERN = re.compile(r"(?<=[가-힣0-9][다요죠음임까다])\.\s*")


def clean_title_text(value: str | None) -> str:
    text = normalize_space(unescape(value or ""))
    return DATE_SUFFIX_PATTERN.sub("", text).strip()


def split_title_summary(text: str | None, summary: str | None = None) -> tuple[str, str | None]:
    cleaned = clean_title_text(text)
    cleaned_summary = normalize_space(unescape(summary or "")) or None
    if not cleaned:
        return "", cleaned_summary

    marker_index = _first_marker_index(cleaned)
    if marker_index:
        return _split_at(cleaned, marker_index, cleaned_summary)

    sentence_index = _sentence_boundary(cleaned)
    if sentence_index:
        return _split_at(cleaned, sentence_index, cleaned_summary)

    if len(cleaned) > 120:
        split_at = cleaned.rfind(" ", 0, 100)
        if split_at > 35:
            return _split_at(cleaned, split_at, cleaned_summary)

    return cleaned, cleaned_summary


def _first_marker_index(text: str) -> int | None:
    indexes = [text.find(marker) for marker in BODY_MARKERS if text.find(marker) > 12]
    return min(indexes) if indexes else None


def _sentence_boundary(text: str) -> int | None:
    if len(text) < 90:
        return None
    for pattern in (END_SENTENCE_PATTERN, KOREAN_SENTENCE_PATTERN):
        match = pattern.search(text)
        if match and match.end() >= 35:
            return match.end()
    return None


def _split_at(text: str, index: int, summary: str | None) -> tuple[str, str | None]:
    title = normalize_space(text[:index])
    body = normalize_space(text[index:])
    if summary:
        body = normalize_space(f"{body} {summary}") if body else summary
    return title, body or None
