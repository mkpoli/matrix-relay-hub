"""Expand X/Twitter status links into the post's text and media.

The QQ group cannot render an x.com link: the client shows neither the post nor
its media. FixTweet reads the post for sites that do, and its API returns the
same content as JSON. The bridge calls that API, writes the post as a normal
group message, and uploads its photos, GIFs and videos. Links are also rewritten
to the FixupX host, whose Open Graph tags give QQ's own unfurler a card when it
works.
"""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .fit import TARGET as QQ_TARGET

log = logging.getLogger(__name__)

API_ROOT = "https://api.fxtwitter.com"
FIXUP_ROOT = "https://fixupx.com"
USER_AGENT = "matrix-relay-hub-qq/1 (+https://github.com/mkpoli/matrix-relay-hub)"
# QQ groups show Asia/Shanghai time, and the bridge's backfill uses it too.
# Shanghai has kept UTC+8 since 1991, so a fixed offset needs no tzdata.
SHANGHAI = datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")
MAX_ATTACHMENTS = 4
MAX_IMAGE = 20 * 1024 * 1024
MAX_VIDEO = 48 * 1024 * 1024
# X serves post media from these hosts; allowing no others keeps an unexpected
# API answer from making the bridge fetch an internal address.
IMAGE_HOSTS = frozenset({"pbs.twimg.com"})
VIDEO_HOSTS = frozenset({"video.twimg.com"})
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=12)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=60)

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
    variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class Attachment:
    """One file to download and forward: an image, or a GIF/video as MP4."""

    kind: str
    url: str
    # Every playable MP4 for this video or GIF, best first. `download` probes
    # their real sizes and takes the best one QQ can upload without re-encoding.
    candidates: tuple[str, ...] = field(default=(), compare=False, repr=False)


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


def _video_url(url: Any) -> str | None:
    """A playable H.264 MP4 URL on X's video host, or None."""
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname not in VIDEO_HOSTS:
        return None
    path = parts.path.lower()
    if not path.endswith(".mp4"):
        return None
    if set(path.split("/")) & {"hevc", "av1", "av01", "vp9"}:
        return None
    return url


def _variants(item: dict[str, Any]) -> tuple[tuple[int, bool, str], ...]:
    """The H.264 MP4 variants of a video or GIF: (bitrate, is_h264, url)."""
    variants: list[tuple[int, bool, str]] = []
    formats = item.get("formats")
    if isinstance(formats, list):
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            codec = str(fmt.get("codec") or "").lower()
            if codec and codec not in ("h264", "avc1"):
                continue
            url = _video_url(fmt.get("url"))
            if url is None:
                continue
            bitrate = fmt.get("bitrate")
            if not isinstance(bitrate, (int, float)) or bitrate < 0:
                bitrate = 0
            h264 = codec in ("h264", "avc1") or "avc1" in url.lower()
            variants.append((int(bitrate), h264, url))
    if not variants:
        url = _video_url(item.get("url"))
        if url:
            variants.append((0, True, url))
    return tuple(variants)


def _ordered(item: dict[str, Any]) -> tuple[str, ...]:
    """Playable MP4s, H.264 first and highest bitrate first, without repeats."""
    ranked = sorted(
        _variants(item), key=lambda variant: (variant[1], variant[0]), reverse=True
    )
    urls: list[str] = []
    for _, _, url in ranked:
        if url not in urls:
            urls.append(url)
    return tuple(urls)


def _still(url: Any) -> str | None:
    """A post picture URL on X's image host, or None."""
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname not in IMAGE_HOSTS:
        return None
    return url


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
        if kind == "photo":
            url = item.get("url")
            media.append(Media(kind, str(url) if url else None, None))
        elif kind in ("video", "gif"):
            # A GIF is an MP4 on X; its still lives in `url` when no thumbnail
            # is given. Without a playable MP4 the still is all there is.
            thumbnail = _still(item.get("thumbnail_url")) or _still(item.get("url"))
            variants = _ordered(item)
            media.append(
                Media(kind, variants[0] if variants else None, thumbnail, variants)
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


def _picture(url: Any) -> Attachment | None:
    validated = _still(url)
    if validated is None:
        return None
    return Attachment("image", _large(validated))


def attachments(post: Post, limit: int = MAX_ATTACHMENTS) -> list[Attachment]:
    """Files to forward in post order: photos, then GIFs and videos, once each."""
    files: list[Attachment] = []
    for media in post.media:
        if media.kind == "photo":
            attachment = _picture(media.url)
        else:
            url = _video_url(media.url) if media.url else None
            candidates = tuple(
                candidate
                for item in media.variants
                if (candidate := _video_url(item)) is not None
            )
            attachment = (
                Attachment("video", url, candidates)
                if url
                else _picture(media.thumbnail)
            )
        if attachment is None or attachment in files:
            continue
        files.append(attachment)
        if len(files) >= limit:
            break
    return files


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


async def _content_length(session: aiohttp.ClientSession, url: str) -> int | None:
    """The file size X reports for a video variant, or None when it will not say."""
    headers = {"User-Agent": USER_AGENT}
    try:
        async with session.head(
            url, headers=headers, timeout=FETCH_TIMEOUT, allow_redirects=False
        ) as response:
            if response.status != 200:
                return None
            if urllib.parse.urlsplit(str(response.url)).hostname not in VIDEO_HOSTS:
                return None
            return response.content_length or None
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.debug("cannot size %s: %s", url, exc)
        return None


async def _ranked(
    session: aiohttp.ClientSession,
    candidates: tuple[str, ...],
    limit: int = QQ_TARGET,
) -> list[str]:
    """Candidates best first, with those that fit QQ ahead of the rest."""
    if len(candidates) == 1:
        return [candidates[0]]
    budget = min(QQ_TARGET, limit)
    sizes = await asyncio.gather(*(_content_length(session, url) for url in candidates))
    fitting = [
        url
        for url, size in zip(candidates, sizes)
        if size is not None and size <= budget
    ]
    if fitting:
        return fitting + [url for url in candidates if url not in fitting]
    known = sorted(
        (size, url) for url, size in zip(candidates, sizes) if size is not None
    )
    if known:
        measured = {url for _, url in known}
        return [url for _, url in known] + [
            url for url in candidates if url not in measured
        ]
    return list(candidates)


async def _fetch(
    session: aiohttp.ClientSession,
    url: str,
    kind: str,
    hosts: frozenset[str],
    prefix: str,
    limit: int,
) -> tuple[bytes, str] | None:
    headers = {"User-Agent": USER_AGENT}
    try:
        async with session.get(
            url, headers=headers, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False
        ) as response:
            if response.status != 200:
                log.info("%s %s returned %s", kind, url, response.status)
                return None
            # A redirect must not carry the fetch onto another host.
            if urllib.parse.urlsplit(str(response.url)).hostname not in hosts:
                log.info("%s %s redirected away from %s", kind, url, hosts)
                return None
            raw_type = response.headers.get("content-type", "")
            content_type = raw_type.split(";", 1)[0].strip().lower()
            if not content_type.startswith(prefix):
                log.info(
                    "%s %s is %s",
                    kind,
                    url,
                    content_type or f"not {prefix.rstrip('/')}",
                )
                return None
            if (response.content_length or 0) > limit:
                log.info("%s %s is over %d bytes", kind, url, limit)
                return None
            data = bytearray()
            async for piece in response.content.iter_chunked(1 << 16):
                data += piece
                if len(data) > limit:
                    log.info("%s %s grew over %d bytes", kind, url, limit)
                    return None
            return bytes(data), content_type
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.info("cannot download %s: %s", url, exc)
        return None


async def download(
    session: aiohttp.ClientSession,
    attachment: Attachment,
    max_bytes: int | None = None,
) -> tuple[bytes, str] | None:
    """Fetch one attachment; None when it is missing, too large or the wrong type."""
    if attachment.kind == "image":
        if _still(attachment.url) is None:
            return None
        hosts, prefix, cap = IMAGE_HOSTS, "image/", MAX_IMAGE
    else:
        if _video_url(attachment.url) is None:
            return None
        hosts, prefix, cap = VIDEO_HOSTS, "video/mp4", MAX_VIDEO
    limit = cap if max_bytes is None else max_bytes
    candidates = tuple(item for item in attachment.candidates if _video_url(item))
    urls = (
        await _ranked(session, candidates, limit)
        if attachment.kind == "video" and candidates
        else [attachment.url]
    )
    for url in urls:
        fetched = await _fetch(session, url, attachment.kind, hosts, prefix, limit)
        if fetched is not None:
            return fetched
    return None
