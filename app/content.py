from bs4 import BeautifulSoup

from app.text import normalize_space


def clean_html_text(value: str | None, max_length: int = 700) -> str | None:
    if not value:
        return None
    if "<" not in value and ">" not in value:
        text = normalize_space(value)
        if not text:
            return None
        if len(text) > max_length:
            return text[: max_length - 3].rstrip() + "..."
        return text
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "video", "audio", "source", "img"]):
        tag.decompose()
    text = normalize_space(soup.get_text(" "))
    if not text:
        return None
    if len(text) > max_length:
        return text[: max_length - 3].rstrip() + "..."
    return text


def extract_media_urls(value: str | None) -> tuple[list[str], list[str]]:
    if not value:
        return [], []
    if "<" not in value and ">" not in value:
        return [], []
    soup = BeautifulSoup(value, "html.parser")
    image_urls = []
    video_urls = []
    for img in soup.select("img[src]"):
        src = img.get("src")
        if src:
            image_urls.append(src)
    for media in soup.select("video[src], video source[src], audio[src], audio source[src]"):
        src = media.get("src")
        if src:
            video_urls.append(src)
    return sorted(set(image_urls)), sorted(set(video_urls))
