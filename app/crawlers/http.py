from collections.abc import Callable
from time import sleep

import httpx

from app.config import settings
from app.models import Source


RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def crawler_headers(url: str, accept: str) -> dict[str, str]:
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": accept,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    try:
        headers["Referer"] = str(httpx.URL(url).copy_with(path="/", query=None, fragment=None))
    except httpx.InvalidURL:
        pass
    return headers


def fetch_with_retry(
    url: str,
    headers: dict[str, str],
    source: Source,
    *,
    request_get: Callable[..., httpx.Response] | None = None,
    sleep_seconds: float | None = None,
) -> httpx.Response:
    request = request_get or httpx.get
    retries = max(0, source.max_retries if source.max_retries is not None else 2)
    timeout = source.timeout_seconds if source.timeout_seconds is not None else settings.request_timeout_seconds
    last_error: Exception | None = None
    last_response: httpx.Response | None = None

    for attempt in range(retries + 1):
        try:
            response = request(
                url,
                headers=headers,
                timeout=timeout,
                follow_redirects=True,
            )
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < retries:
                last_response = response
                _sleep_before_retry(attempt, sleep_seconds)
                continue
            return response
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            _sleep_before_retry(attempt, sleep_seconds)

    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise RuntimeError("Crawler request failed")


def _sleep_before_retry(attempt: int, sleep_seconds: float | None) -> None:
    delay = min(6.0, 0.6 * (2**attempt)) if sleep_seconds is None else sleep_seconds
    if delay > 0:
        sleep(delay)
