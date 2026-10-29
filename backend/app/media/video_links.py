# checks manual links are safe and finds related youtube video links.

import re
from functools import lru_cache
from urllib.parse import parse_qs, urljoin, urlparse
import requests
from app import config as cfg
from app.observability.timing import timed


# checks that a link uses http or https and has no username or password in it
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


# checks if a link is a real youtube video link
def youtube_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    # check short youtube links
    if host == "youtu.be":
        return bool(re.fullmatch(r"/[A-Za-z0-9\_-]{11}/?", parsed.path))

    # allow only these youtube domains
    if host not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "www.youtube-nocookie.com",
    }:
        return False

    # check regular youtube watch links
    if parsed.path == "/watch":
        return any(re.fullmatch(r"[A-Za-z0-9\_-]{11}", value)
                   for value in parse_qs(parsed.query).get("v", []))

    # check youtube embed, shorts, and live links
    return bool(re.fullmatch(r"/(?:embed|shorts|live)/[A-Za-z0-9\_-]{11}/?", parsed.path))


# finds youtube links on an approved manual page. gives back up to 3 links.
# results are cached
@timed('video_check')
@lru_cache(maxsize=64)
@timed('video_cache_miss')
def manual_video_targets(url):
    """
    only looks at approved manual pages to find video links.

    the website text is never sent to the answer model as evidence.
    """

    # return the link directly if it is already a youtube video
    if youtube_url(url):
        return [url]

    # stop if video link checking is turned off
    if not cfg.RESOLVE_VIDEOS:
        return []

    current = url

    # follow up to 3 redirects
    for _ in range(3):
        parsed = urlparse(current)

        # allow only safe https links on the standard port
        if (
            not safe_url(current)
            or parsed.scheme != "https"
            or parsed.port not in {None, 443}
        ):
            return []

        # return if a redirect leads directly to youtube
        if youtube_url(current):
            return [current]

        # check that the website is on the approved list
        if parsed.hostname not in cfg.VIDEO_HOSTS:
            return []

        # open the page, but do not follow redirects automatically
        with requests.get(
            current,
            allow_redirects=False,
            stream=True,
            timeout=(3, 5),
        ) as response:
            # if it redirects, go to the new page
            if 300 <= response.status_code < 400:
                current = urljoin(
                    current,
                    response.headers.get("Location", ""),
                )
                continue

            # continue only if the page loaded and is html
            if (
                response.status_code != 200
                or "text/html"
                not in response.headers.get(
                    "Content-Type", ""
                )
            ):
                return []

            # read only the first part of the page
            body = bytearray()

            for chunk in response.iter_content(16384):
                body.extend(chunk)

                if len(body) >= 256000:
                    break

            # clean the page text so links can be found
            html = (
                body.decode("utf-8", errors="replace")
                .replace("\\/", "/")
                .replace("&amp;", "&")
            )

            # find youtube video links in the page
            matches = re.findall(
                r'(?:https?:)?//(?:www\.)?'
                r'(?:youtube(?:-nocookie)?\.com/'
                r'(?:embed/|watch\?v=)|youtu\.be/)'
                r'[A-Za-z0-9\_-]{11}',
                html,
            )

            # return up to three unique links
            return list(
                dict.fromkeys(
                    "https:" + link
                    if link.startswith("//")
                    else link
                    for link in matches
                )
            )[:3]

    return []
