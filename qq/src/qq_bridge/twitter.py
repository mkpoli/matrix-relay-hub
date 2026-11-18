"""Expand X/Twitter status links into the post's text and pictures.

The QQ group cannot render an x.com link: the client shows neither the post nor
its images. FixTweet reads the post for sites that do, and its API returns the
same content as JSON. The bridge calls that API, writes the post as a normal
group message, and uploads its photos. Links are also rewritten to the
FixupX host, whose Open Graph tags give QQ's own unfurler a card when it works.
"""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

API_ROOT = "https://api.fxtwitter.com"
FIXUP_ROOT = "https://fixupx.com"
USER_AGENT = "matrix-relay-hub-qq/1 (+https://github.com/mkpoli/matrix-relay-hub)"
# QQ groups show Asia/Shanghai time, and the bridge's backfill uses it too.
# Shanghai has kept UTC+8 since 1991, so a fixed offset needs no tzdata.
SHANGHAI = datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")
MAX_IMAGES = 4
MAX_MEDIA = 20 * 1024 * 1024
# X serves post pictures and video stills from this host; refusing others keeps
# an unexpected API answer from making the bridge fetch an internal address.
IMAGE_HOSTS = frozenset({"pbs.twimg.com"})
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=12)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=40)

# A complete URL, stopped at whitespace, angle brackets and quotes. Matching
# whole spans keeps an X link inside another URL's query from being rewritten.
URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
LINK_HOSTS = frozenset(
    {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
)
STATUS_PATH = re.compile(
    r"^/(?P<handle>[A-Za-z0-9_]{1,15}|i)/(?:web/)?status/(?P<status>\d+)(?:/.*)?$",
    re.IGNORECASE,
)
# Punctuation a sentence leaves against a URL. CJK stops count too.
URL_TRAIL = ".,;:!?)]}*`。、，"


@dataclass(frozen=True)
class Link:
    start: int
    end: int
    handle: str
    status: str

    @property
    def api_url(self) -> str:
        return f"{API_ROOT}/{self.handle}/status/{self.status}"

    @property
    def fixup_url(self) -> str:
        return f"{FIXUP_ROOT}/{self.handle}/status/{self.status}"


@dataclass(frozen=True)
class Media:
    kind: str
    url: str | None
    thumbnail: str | None


@dataclass(frozen=True)
class Quote:
    handle: str
    name: str
    text: str


@dataclass(frozen=True)
class Post:
    handle: str
    name: str
    text: str
    created: datetime.datetime | None
    link: Link
    media: tuple[Media, ...]
    quote: Quote | None
    poll: tuple[str, ...]
    sensitive: bool
    replying_to: str | None


def _spans(text: str) -> list[Link]:
    """Every complete X/Twitter status link in `text`, in reading order."""
    links: list[Link] = []
    for match in URL_RE.finditer(text or ""):
        raw = match.group(0).rstrip(URL_TRAIL)
        if not raw:
            continue
        try:
            parts = urllib.parse.urlsplit(raw)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https"):
            continue
        if parts.hostname is None or parts.hostname.lower() not in LINK_HOSTS:
            continue
        path = STATUS_PATH.match(parts.path)
        if not path:
            continue
        links.append(
            Link(
                match.start(),
                match.start() + len(raw),
                path.group("handle"),
                path.group("status"),
            )
        )
    return links


def find(text: str) -> list[Link]:
    """The distinct X/Twitter status links in `text`, in reading order."""
    links: list[Link] = []
    seen: set[str] = set()
    for link in _spans(text):
        key = f"{link.handle.lower()}/{link.status}"
        if key in seen:
            continue
        seen.add(key)
        links.append(link)
    return links


def rewrite(text: str) -> str:
    """Replace every X/Twitter status link with the FixupX host."""
    pieces: list[str] = []
    last = 0
    for link in _spans(text):
        pieces.append(text[last : link.start])
        pieces.append(link.fixup_url)
        last = link.end
    pieces.append(text[last:])
    return "".join(pieces)


def _created_at(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def local_time(value: datetime.datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _media(data: dict[str, Any]) -> tuple[Media, ...]:
    # `all` keeps the post's order; `photos` is the fallback when it is absent.
    items = data.get("all")
    if not isinstance(items, list):
        items = data.get("photos")
    if not isinstance(items, list):
        return ()
    media: list[Media] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "photo")
        if kind not in ("photo", "video", "gif"):
            continue
        url = item.get("url")
        thumbnail = item.get("thumbnail_url")
        media.append(
            Media(
                kind,
                str(url) if url else None,
                str(thumbnail) if thumbnail else None,
            )
        )
    return tuple(media)


def _quote(data: Any) -> Quote | None:
    if not isinstance(data, dict):
        return None
    text = str(data.get("text") or "").strip()
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    handle = str(author.get("screen_name") or data.get("screen_name") or "")
    name = str(author.get("name") or handle)
    if not (text or handle):
        return None
    return Quote(handle or "?", name or handle or "?", text)


def _poll(data: Any) -> tuple[str, ...]:
    if not isinstance(data, dict):
        return ()
    choices = data.get("choices")
    if not isinstance(choices, list):
        return ()
    labels: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        label = str(choice.get("label") or "").strip()
        percentage = choice.get("percentage")
        if label and isinstance(percentage, (int, float)):
            labels.append(f"{label} ({percentage:g}%)")
        elif label:
            labels.append(label)
    return tuple(labels)


def parse(data: Any, link: Link) -> Post | None:
    """Turn an API `tweet` object into a post, or None when it is not this link."""
    if not isinstance(data, dict):
        return None
    if str(data.get("id") or "") != link.status:
        return None
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    handle = str(author.get("screen_name") or link.handle)
    name = str(author.get("name") or handle)
    replying = data.get("replying_to")
    if isinstance(replying, dict):
        replying = replying.get("screen_name")
    return Post(
        handle=handle,
        name=name,
        text=str(data.get("text") or "").strip(),
        created=_created_at(data.get("created_at")),
        link=link,
        media=_media(data.get("media") if isinstance(data.get("media"), dict) else {}),
        quote=_quote(data.get("quote")),
        poll=_poll(data.get("poll")),
        sensitive=bool(data.get("possibly_sensitive")),
        replying_to=str(replying) if replying else None,
    )


async def fetch(session: aiohttp.ClientSession, link: Link) -> Post | None:
    """Read one post through the FixTweet API; None covers every failure."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        async with session.get(link.api_url, headers=headers, timeout=FETCH_TIMEOUT) as response:
            if response.status != 200:
                log.info("FixTweet returned %s for %s", response.status, link.status)
                return None
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.info("cannot read %s through FixTweet: %s", link.status, exc)
        return None
    if not isinstance(data, dict) or data.get("code") != 200:
        return None
    return parse(data.get("tweet"), link)


def _large(url: str) -> str:
    """Ask X for a chat-sized copy instead of the original upload."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "name"]
    query.append(("name", "large"))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def image_urls(post: Post, limit: int = MAX_IMAGES) -> list[str]:
    """Pictures to upload: photos, then video and GIF stills, once each."""
    urls: list[str] = []
    for media in post.media:
        url = media.url if media.kind == "photo" else media.thumbnail
        if not url:
            continue
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if parts.scheme != "https" or parts.hostname not in IMAGE_HOSTS:
            continue
        url = _large(url)
        if url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def format_post(post: Post) -> str:
    """The group message for a post: author, text, quote, poll and source link."""
    head = f"[Twitter] {post.name} (@{post.handle})"
    when = local_time(post.created)
    if when:
        head += f" · {when}"
    lines = [head]
    if post.replying_to:
        lines.append(f"Replying to @{post.replying_to}")
    if post.sensitive:
        lines.append("[sensitive]")
    lines.append("")
    lines.append(post.text or "[media only]")
    if post.quote:
        quote = f"Quoting {post.quote.name} (@{post.quote.handle})"
        if post.quote.text:
            quote += f": {post.quote.text}"
        lines.extend(("", quote))
    if post.poll:
        lines.extend(("", "Poll: " + " · ".join(post.poll)))
    lines.extend(("", post.link.fixup_url))
    return "\n".join(lines).strip()


async def download(
    session: aiohttp.ClientSession, url: str, max_bytes: int = MAX_MEDIA
) -> tuple[bytes, str] | None:
    """Fetch one picture; None when it is missing, too large or not an image."""
    headers = {"User-Agent": USER_AGENT}
    try:
        async with session.get(
            url, headers=headers, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False
        ) as response:
            if response.status != 200:
                log.info("picture %s returned %s", url, response.status)
                return None
            # A redirect must not carry the fetch onto another host.
            if urllib.parse.urlsplit(str(response.url)).hostname not in IMAGE_HOSTS:
                log.info("picture %s redirected away from %s", url, IMAGE_HOSTS)
                return None
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if not content_type.startswith("image/"):
                log.info("picture %s is %s", url, content_type or "not an image")
                return None
            if (response.content_length or 0) > max_bytes:
                log.info("picture %s is over %d bytes", url, max_bytes)
                return None
            data = bytearray()
            async for piece in response.content.iter_chunked(1 << 16):
                data += piece
                if len(data) > max_bytes:
                    log.info("picture %s grew over %d bytes", url, max_bytes)
                    return None
            return bytes(data), content_type
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.info("cannot download %s: %s", url, exc)
        return None
