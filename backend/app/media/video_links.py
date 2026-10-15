import re
from functools import lru_cache
from urllib.parse import parse_qs, urljoin, urlparse
import requests
from app import config as cfg


def safe_url(value):
    try:
        parsed = urlparse(value)

        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except (TypeError, ValueError):
        return False


def youtube_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return bool(re.fullmatch(r"/[A-Za-z0-9_-]{11}/?", parsed.path))
    if host not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "www.youtube-nocookie.com",
    }:
        return False
    if parsed.path == "/watch":
        return any(re.fullmatch(r"[A-Za-z0-9_-]{11}", value)
                   for value in parse_qs(parsed.query).get("v", []))
    return bool(re.fullmatch(r"/(?:embed|shorts|live)/[A-Za-z0-9_-]{11}/?", parsed.path))


@lru_cache(maxsize=64)
def manual_video_targets(url):
    """
    Inspect only allowlisted manual-linked pages for video URLs.

    Website text is never sent to the answer model as evidence.
    """

    if youtube_url(url):
        return [url]

    if not cfg.RESOLVE_VIDEOS:
        return []

    current = url

    for _ in range(3):
        parsed = urlparse(current)

        if (
            not safe_url(current)
            or parsed.scheme != "https"
            or parsed.port not in {None, 443}
        ):
            return []

        if youtube_url(current):
            return [current]

        if parsed.hostname not in cfg.VIDEO_HOSTS:
            return []

        with requests.get(
            current,
            allow_redirects=False,
            stream=True,
            timeout=(3, 5),
        ) as response:
            if 300 <= response.status_code < 400:
                current = urljoin(
                    current,
                    response.headers.get("Location", ""),
                )
                continue

            if (
                response.status_code != 200
                or "text/html"
                not in response.headers.get(
                    "Content-Type", ""
                )
            ):
                return []

            body = bytearray()

            for chunk in response.iter_content(16384):
                body.extend(chunk)

                if len(body) >= 256000:
                    break

            html = (
                body.decode("utf-8", errors="replace")
                .replace("\\/", "/")
                .replace("&amp;", "&")
            )

            matches = re.findall(
                r'(?:https?:)?//(?:www\.)?'
                r'(?:youtube(?:-nocookie)?\.com/'
                r'(?:embed/|watch\?v=)|youtu\.be/)'
                r'[A-Za-z0-9_-]{11}',
                html,
            )

            return list(
                dict.fromkeys(
                    "https:" + link
                    if link.startswith("//")
                    else link
                    for link in matches
                )
            )[:3]

    return []
