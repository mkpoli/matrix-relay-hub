"""Relay logic between the QQ group and the Matrix rooms it mirrors."""

from __future__ import annotations

import asyncio
import contextlib
import json

import aiohttp
import logging
import re
import time
import uuid
from pathlib import Path

from . import twitter
from .config import Config
from .fit import QQ_AUDIO, QQ_FILE_TYPES, TOO_LARGE, UNREADABLE, Prepared, Unfit, prepare, sweep_temp
from .formatting import (
    CustomEmoji,
    chunk,
    extract_custom_emojis,
    extract_face_ids,
    extract_forwarded_attachments,
    format_reaction_key,
    is_trusted_qq_media_url,
    normalize_media_url_key,
    render_qq_text,
    sticker_labels,
    strip_leading_mentions,
    strip_reply_fallback,
)
from .matrix import MatrixClient, MatrixError, MediaTooLarge
from .qq import QQClient, QQError
from .ratelimit import RateLimiter
from .sources import source_tag
from .store import Store

log = logging.getLogger(__name__)

MAX_MEDIA_DOWNLOAD = 40 * 1024 * 1024
# Matrix media held in memory for shrinking; the homeserver takes 50 MB uploads.
MAX_MATRIX_MEDIA = 64 * 1024 * 1024
# Discord, which every public room is bridged to, refuses a longer message and posts an error
# instead of relaying it, so QQ text beyond this goes out as several messages.
MATRIX_TEXT_LIMIT = 2000
QQ_FORMAT_REFUSED = 40034002
# How long the send queue waits for media before letting later messages pass.
MEDIA_WAIT = 20.0
# Events not yet delivered (cut off by a restart, or refused by a QQ or network
# failure that may pass) are tried again every PENDING_RETRY seconds, at most
# PENDING_ATTEMPTS times and for PENDING_MAX_AGE seconds after they arrived.
PENDING_MAX_AGE = 24 * 3600
PENDING_ATTEMPTS = 8
PENDING_RETRY = 15 * 60

# The animated APNG when the mirror has one, then its static PNG. The bridge
# uploads either as image/png; clients animate APNG on their own.
FACE_MIRRORS = (
    "https://face.viki.moe/apng/{face_id}.png",
    "https://cdn.jsdelivr.net/gh/vikiboss/face@main/face/apng/{face_id}.png",
    "https://face.viki.moe/png/{face_id}.png",
    "https://cdn.jsdelivr.net/gh/vikiboss/face@main/face/png/{face_id}.png",
)
# Cache version for the face images above; a cached mxc from an older source
# is dropped so a stale still cannot outlive the switch to APNG.
FACE_SOURCE = "apng"


# An edit of a text that never reached QQ (its send is still being retried) is relayed when the
# original is at most this old; an older one predates the bridge or was never meant for QQ.
EDIT_WINDOW_MS = 86_400_000
# Cap distinct inline custom emojis relayed as media per message to stay within QQ rate limits.
MAX_CUSTOM_EMOJIS = 4


def _emoji_media_content(
    emoji: CustomEmoji,
    *,
    reply_to: str | None = None,
    mentions: dict | None = None,
) -> dict:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", emoji.shortcode.strip(":")).strip("_") or "emoji"
    ext = ".gif" if emoji.animated else ".png"
    mimetype = "image/gif" if emoji.animated else "image/png"
    media_content: dict = {
        "msgtype": "m.image",
        "url": emoji.mxc,
        "body": emoji.shortcode,
        "filename": f"{slug}{ext}",
        "info": {"mimetype": mimetype},
    }
    if reply_to:
        media_content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    if mentions:
        media_content["m.mentions"] = mentions
    return media_content


def _sniff_image_format(data: bytes, mimetype: str, filename: str) -> tuple[str, str]:
    if data.startswith((b"GIF87a", b"GIF89a")):
        detected_mime, ext = "image/gif", ".gif"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_mime, ext = "image/png", ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        detected_mime, ext = "image/jpeg", ".jpg"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        detected_mime, ext = "image/webp", ".webp"
    else:
        return mimetype, filename
    stem = Path(filename).stem or "emoji"
    return detected_mime, f"{stem}{ext}"


def _extract_message_emojis(content: dict) -> tuple[str, list[CustomEmoji], bool]:
    if content.get("format") != "org.matrix.custom.html":
        return "", [], False
    formatted = content.get("formatted_body")
    if not isinstance(formatted, str) or not formatted:
        return "", [], False
    html_body, emojis, emoji_only = extract_custom_emojis(formatted)
    return html_body, emojis[:MAX_CUSTOM_EMOJIS], emoji_only


def _media_caption(content: dict) -> str:
    """Return the MSC2530 caption carried by a media event, or '' when body is just the filename."""
    filename = str(content.get("filename") or "").strip()
    if not filename:
        return ""
    plain_body = strip_reply_fallback(str(content.get("body") or "")).strip()
    html_body, custom_emojis, _ = _extract_message_emojis(content)
    if custom_emojis and (
        not plain_body or any(e.shortcode not in plain_body for e in custom_emojis)
    ):
        body = html_body.strip()
    else:
        body = plain_body
    if not body or body == filename:
        return ""
    # mautrix-telegram emits `Forwarded message from Name\n\n> ` for captionless forwarded media.
    formatted = str(content.get("formatted_body") or "")
    if body.startswith("Forwarded message from ") or "data-telegram-forward" in formatted:
        lines = [line.rstrip() for line in body.splitlines()]
        while lines and lines[-1] == ">":
            lines.pop()
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines).strip()
    return body


def _transient(exc: QQError) -> bool:
    """A refusal that may pass: QQ overloaded, rate limited or failing."""
    return exc.rate_limited or (exc.status or 0) >= 500


def _sticker_label(body: object, *, reason: str | None = None) -> str:
    label = " ".join(str(body or "").split())
    if label:
        if len(label) > 200:
            label = label[:200] + "..."
        return f"[sticker: {label}]"
    return f"[sticker {reason}]" if reason else "[sticker]"


def is_edit(content: dict) -> bool:
    """Matrix sends edits as ordinary messages carrying m.new_content."""
    if "m.new_content" in content:
        return True
    return (content.get("m.relates_to") or {}).get("rel_type") == "m.replace"


_STANDARD_QQ_MESSAGE_KEYS = frozenset(
    {
        "id",
        "group_id",
        "group_openid",
        "content",
        "timestamp",
        "author",
        "attachments",
        "mentions",
        "message_scene",
        "msg_elements",
        "message_type",
        "message_reference",
    }
)
_SENSITIVE_QQ_KEY_PARTS = ("token", "secret", "sig", "cookie", "password", "authorization", "rkey")
_UNKNOWN_EVENT_SCALAR_KEYS = (
    "id",
    "group_openid",
    "group_id",
    "channel_id",
    "guild_id",
    "message_id",
    "msg_id",
    "message_type",
    "timestamp",
    "op_member_openid",
    "msg_seq",
    "seq",
)


def _safe_log_str(value: str, limit: int = 80) -> str:
    cleaned = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in value)
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "..."


def _safe_log_scalar(value: object, limit: int = 80) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if "://" in value:
            head, _, _ = value.partition("?")
            return _safe_log_str(head, limit)
        return _safe_log_str(value, limit)
    return f"<{type(value).__name__}>"


def _summarize_qq_payload(data: object) -> str:
    if not isinstance(data, dict):
        return f"<{type(data).__name__}>"
    summary: dict[str, object] = {
        "keys": [_safe_log_str(str(k), 40) for k in sorted(data, key=str)[:16]]
    }
    for key in _UNKNOWN_EVENT_SCALAR_KEYS:
        if key in data:
            summary[key] = _safe_log_scalar(data[key])
    author = data.get("author")
    if isinstance(author, dict):
        author_summary: dict[str, object] = {}
        for key in ("id", "member_openid", "username", "bot"):
            if key in author:
                author_summary[key] = _safe_log_scalar(author[key])
        if author_summary:
            summary["author"] = author_summary
    scene = data.get("message_scene")
    if isinstance(scene, dict):
        scene_summary: dict[str, object] = {}
        if isinstance(scene.get("source"), str):
            scene_summary["source"] = _safe_log_str(scene["source"], 40)
        ext = scene.get("ext")
        if isinstance(ext, list):
            ext_keys = sorted(
                {
                    _safe_log_str(item.partition("=")[0], 40)
                    for item in ext[:32]
                    if isinstance(item, str)
                    and "=" in item
                    and not any(
                        part in item.partition("=")[0].lower()
                        for part in _SENSITIVE_QQ_KEY_PARTS
                    )
                }
            )[:8]
            if ext_keys:
                scene_summary["ext_keys"] = ext_keys
        if scene_summary:
            summary["message_scene"] = scene_summary
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 400 else text[:400] + "..."


class Relay:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        matrix: MatrixClient,
        qq: QQClient,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.matrix = matrix
        self.qq = qq
        self.domain = cfg.matrix.domain
        self.bot_mxid = cfg.matrix.bot_mxid
        # (pending id, event) pairs from the appservice; the id is None for
        # events that were never stored.
        self.incoming: asyncio.Queue[tuple[int | None, dict]] = asyncio.Queue()
        self._current: int | None = None
        self._outstanding: dict[int, int] = {}
        self._inflight_events: dict[str, int] = {}
        self._inflight_waiters: dict[str, list[dict]] = {}
        self._keep: set[int] = set()
        self._claimed: set[int] = set()
        self._rooms: dict[str, str] = {}
        self._labels: dict[str, str] = {}
        self._incoming: str | None = None
        # One label per room keeps a group of several rooms readable; a single
        # room needs no prefix.
        self._tagged = len(cfg.rooms) > 1
        self._ghosts: dict[str, str] = {}
        self._avatar: str | None = None
        self._avatars: set[str] = set()
        self._joined: set[tuple[str, str]] = set()
        self._names: dict[tuple[str, str], tuple[str, float]] = {}
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        # Link expansion runs beside the send loop so a slow FixTweet call does
        # not hold up the conversation.
        self._twitter: asyncio.Queue[dict] = asyncio.Queue()
        # Media keeps its place in the send queue but is downloaded and shrunk
        # in these lanes, so a long transcode holds up neither text nor images.
        self._images: asyncio.Queue[dict] = asyncio.Queue()
        self._videos: asyncio.Queue[dict] = asyncio.Queue()
        self._global = RateLimiter(60)
        self._group_limiters: dict[str, RateLimiter] = {}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await asyncio.to_thread(sweep_temp)
        if self.store.meta("face_source") != FACE_SOURCE:
            self.store.clear_faces()
            self.store.set_meta("face_source", FACE_SOURCE)
        await self._ensure_bot()
        await self._ensure_avatar()
        for room in self.cfg.rooms:
            try:
                await self._resolve_room(room)
            except Exception as exc:
                log.warning("cannot set up %s yet: %s", room.alias, exc)
        self._tasks = [
            asyncio.create_task(self._consume_matrix()),
            asyncio.create_task(self._send_loop()),
            asyncio.create_task(self._twitter_loop()),
            asyncio.create_task(self._prepare_loop(self._images)),
            asyncio.create_task(self._prepare_loop(self._videos)),
            asyncio.create_task(self._retry_rooms()),
        ]

    def resume_pending(self) -> None:
        """Queue the events a previous run left undelivered; only the bridge
        itself calls this, so a CLI run beside it never sends them twice."""
        self._claim_pending(idle=0)
        self._tasks.append(asyncio.create_task(self._retry_pending()))

    def _claim_pending(self, idle: int) -> None:
        busy = set(self._outstanding) | self._claimed
        if self._current is not None:
            busy.add(self._current)
        items = self.store.claim_pending(
            max_age=PENDING_MAX_AGE,
            max_attempts=PENDING_ATTEMPTS,
            idle=idle,
            busy=busy,
        )
        if self.store.dropped_pending:
            log.warning(
                "gave up on %d Matrix events that never reached QQ",
                self.store.dropped_pending,
            )
        if items:
            log.info("retrying %d Matrix events not yet delivered", len(items))
        for item in items:
            self._claimed.add(item[0])
            self.incoming.put_nowait(item)

    async def _retry_pending(self) -> None:
        while True:
            await asyncio.sleep(PENDING_RETRY)
            try:
                self._claim_pending(idle=PENDING_RETRY)
            except Exception:
                log.exception("cannot read the pending events")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _ensure_bot(self) -> None:
        localpart = self.cfg.matrix.bot_localpart
        try:
            await self.matrix.register_user(localpart, "QQ")
        except Exception as exc:
            log.debug("bot user already present or not creatable: %s", exc)

    async def _ensure_avatar(self) -> None:
        path = Path(__file__).with_name("qq.png")
        try:
            data = path.read_bytes()
        except OSError:
            return
        try:
            self._avatar = await self.matrix.upload(
                data, "image/png", "qq.png", self.bot_mxid
            )
            await self.matrix.set_avatar(self.bot_mxid, self._avatar)
        except Exception as exc:
            log.warning("cannot install the bridge avatar: %s", exc)
            self._avatar = None

    async def _resolve_room(self, room) -> None:
        room_id = await self.matrix.resolve_alias(room.alias)
        self._rooms[room.alias] = room_id
        self._labels[room_id] = room.label
        if room.alias == self.cfg.qq.incoming:
            self._incoming = room_id
        try:
            await self.matrix.join(room_id, self.bot_mxid)
        except MatrixError as exc:
            log.warning("cannot join %s as %s: %s", room.alias, self.bot_mxid, exc)
        if await self.matrix.is_encrypted(room_id):
            log.warning(
                "%s is encrypted; QQ messages can be written but not read",
                room.alias,
            )
        log.info("mirroring %s (%s) as [%s]", room.alias, room_id, room.label)

    async def _retry_rooms(self) -> None:
        while True:
            await asyncio.sleep(60)
            for room in self.cfg.rooms:
                if room.alias in self._rooms:
                    continue
                try:
                    await self._resolve_room(room)
                except Exception:
                    pass

    # -- Matrix -> QQ -----------------------------------------------------

    async def _consume_matrix(self) -> None:
        while True:
            seq, event = await self.incoming.get()
            if seq is not None:
                self._claimed.discard(seq)
                if seq in self._outstanding:
                    # Already on its way to QQ; a second copy would send twice.
                    continue
            self._current = seq
            if seq is not None:
                self._outstanding[seq] = 0
            try:
                await self.handle_matrix_event(event)
            except Exception:
                log.exception("failed to relay a Matrix event")
            finally:
                self._current = None
            if seq is not None and not self._outstanding.get(seq):
                self._outstanding.pop(seq, None)
                self._settle(seq)

    def _put(self, job: dict, queue: asyncio.Queue | None = None) -> None:
        """Queue a job for QQ, counting it against the event it came from."""
        if self._current is not None and "pending" not in job:
            job["pending"] = self._current
        seq = job.get("pending")
        if seq is not None:
            self._outstanding[seq] = self._outstanding.get(seq, 0) + 1
        event_id = job.get("event_id")
        if event_id:
            self._inflight_events[event_id] = self._inflight_events.get(event_id, 0) + 1
        (queue or self._queue).put_nowait(job)

    def _queue_twitter(self, job: dict) -> None:
        """Queue a link expansion; it counts like any other job for the event."""
        self._put(job, self._twitter)

    def _finish(self, job: dict) -> None:
        event_id = job.get("event_id")
        if event_id:
            left_event = self._inflight_events.get(event_id, 0) - 1
            if left_event > 0:
                self._inflight_events[event_id] = left_event
            else:
                self._inflight_events.pop(event_id, None)
                for waiter in self._inflight_waiters.pop(event_id, ()):
                    self._queue.put_nowait(waiter)
        seq = job.get("pending")
        if seq is None:
            return
        left = self._outstanding.get(seq, 1) - 1
        if left > 0:
            self._outstanding[seq] = left
            return
        self._outstanding.pop(seq, None)
        self._settle(seq)

    def _retry_later(self, job: dict | None = None) -> None:
        """Keep the event that produced `job` (or the one being handled) for retry."""
        seq = job.get("pending") if job is not None else self._current
        if seq is not None:
            self._keep.add(seq)

    def _settle(self, seq: int) -> None:
        if seq in self._keep:
            self._keep.discard(seq)
            return
        try:
            self.store.finish_pending(seq)
        except Exception:
            log.exception("cannot clear pending event %s", seq)

    async def handle_matrix_event(self, event: dict) -> None:
        room_id = event.get("room_id")
        sender = event.get("sender")
        if not room_id or not sender:
            return
        event_type = event.get("type")
        if event_type == "m.room.encrypted":
            log.debug("ignoring encrypted event in %s", room_id)
            return
        if event_type == "m.room.redaction":
            await self._redact(event)
            return
        if event_type not in ("m.room.message", "m.sticker", "m.reaction"):
            return
        if event_type == "m.reaction" and not self.cfg.relay_reactions:
            return
        if self._ignored(sender):
            return
        label = self._labels.get(room_id)
        if label is None:
            if len(self._rooms) < len(self.cfg.rooms):
                # A configured room may not be resolved yet; try again later.
                self._retry_later()
            return
        if event_type == "m.reaction":
            await self._handle_reaction(event, room_id, sender, label)
            return
        content = event.get("content") or {}
        sticker = event_type == "m.sticker"
        if sticker:
            # A sticker has no msgtype; send it as the image or video it holds,
            # or fall back to its label when no raster/video media was bridged
            # (e.g. unconverted Lottie JSON or a missing URL).
            mimetype = str((content.get("info") or {}).get("mimetype") or "")
            url = content.get("url") or (content.get("file") or {}).get("url")
            if not url or (mimetype and not mimetype.startswith(("image/", "video/"))):
                content = {
                    **content,
                    "msgtype": "m.text",
                    "body": _sticker_label(content.get("body")),
                }
            else:
                kind = "m.video" if mimetype.startswith("video/") else "m.image"
                content = {**content, "msgtype": kind}
        if is_edit(content):
            await self._handle_edit(event, room_id, sender, label, content)
            return
        msgtype = content.get("msgtype")
        event_id = event.get("event_id", "")
        if self.cfg.ignore_notices and msgtype == "m.notice":
            return
        if event_id and self.store.superseded(event_id):
            log.debug("ignoring %s, edited or redacted since", event_id)
            return
        # A long text or captioned media event posts several QQ messages; after a
        # restart only the parts not yet posted are sent.
        sent = self.store.sent_count(event_id) if event_id else 0
        if event_id and self.store.relayed_qq(event_id):
            log.debug("ignoring an event already carried to QQ: %s", event_id)
            return
        if event_id and sent and msgtype not in ("m.text", "m.emote"):
            media_caption = "" if sticker else _media_caption(content)
            if media_caption:
                if self.cfg.twitter and twitter.find(media_caption):
                    media_caption = twitter.rewrite(media_caption)
                prefix = await self._sender_prefix(room_id, sender)
                reply_name = await self._reply_target_name(room_id, sender, content)
                speaker = f"{prefix} → {reply_name}" if reply_name else prefix
                caption_parts = chunk(f"{speaker}: {media_caption}")
                if msgtype == "m.audio" or len(caption_parts) > 1:
                    reference = self._reply_reference(event)
                    for idx, part in enumerate(caption_parts, start=1):
                        if idx < sent:
                            continue
                        self._put(
                            {
                                "kind": "text",
                                "group": self.cfg.qq.group,
                                "room": room_id,
                                "event_id": event_id,
                                "text": part,
                                "reference": reference if idx == 1 and msgtype == "m.audio" else None,
                                "fallback_unquoted": True,
                            }
                        )
                    return
            log.debug("ignoring an event already carried to QQ: %s", event_id)
            return

        if msgtype in ("m.text", "m.emote"):
            html_body, custom_emojis, emoji_only = _extract_message_emojis(content)
            if emoji_only:
                reply_to = self._reply_event_id(content)
                mentions = (
                    content.get("m.mentions")
                    if isinstance(content.get("m.mentions"), dict)
                    else None
                )
                for index, emoji in enumerate(custom_emojis):
                    if index < sent:
                        continue
                    self._enqueue_media(
                        {
                            "kind": "media",
                            "group": self.cfg.qq.group,
                            "room": room_id,
                            "event_id": event_id,
                            "sender": sender,
                            "content": _emoji_media_content(
                                emoji,
                                reply_to=reply_to if index == 0 else None,
                                mentions=mentions if index == 0 else None,
                            ),
                            "sticker": True,
                            "custom_emoji": True,
                            "emoji_only": True,
                            "sniff_image": True,
                            "reply_to": reply_to if index == 0 else None,
                        }
                    )
                return
            body = (
                html_body
                if custom_emojis
                else strip_reply_fallback(str(content.get("body", "")))
            )
            if not body.strip():
                return
            links = twitter.find(body) if self.cfg.twitter else []
            if links:
                body = twitter.rewrite(body)
            name = await self._display_name(room_id, sender)
            if sticker:
                reply_name = await self._reply_target_name(
                    room_id, sender, content, sticker=True
                )
                if reply_name:
                    name = f"{name} → {reply_name}"
            source = f"[{source_tag(sender)}] "
            tag = f"[{label}] " if self._tagged else ""
            text = (
                f"{source}{tag}* {name} {body}"
                if msgtype == "m.emote"
                else f"{source}{tag}{name}: {body}"
            )
            reference = self._reply_reference(event)
            parts = chunk(text)
            if len(custom_emojis) == 1 and len(parts) == 1:
                reply_to = self._reply_event_id(content)
                if sent == 0:
                    self._enqueue_media(
                        {
                            "kind": "media",
                            "group": self.cfg.qq.group,
                            "room": room_id,
                            "event_id": event_id,
                            "sender": sender,
                            "content": _emoji_media_content(
                                custom_emojis[0], reply_to=reply_to
                            ),
                            "custom_emoji": True,
                            "sniff_image": True,
                            "caption": parts[0],
                            "fallback_text": parts[0],
                            "text_required": True,
                            "reply_to": reply_to,
                        }
                    )
            else:
                for index, part in enumerate(parts):
                    if index < sent:
                        continue
                    self._put(
                        {
                            "kind": "text",
                            "group": self.cfg.qq.group,
                            "room": room_id,
                            "event_id": event_id,
                            "text": part,
                            "reference": reference if index == 0 else None,
                            "fallback_unquoted": sticker,
                        }
                    )
                for index, emoji in enumerate(custom_emojis, start=len(parts)):
                    if index < sent:
                        continue
                    self._enqueue_media(
                        {
                            "kind": "media",
                            "group": self.cfg.qq.group,
                            "room": room_id,
                            "event_id": event_id,
                            "sender": sender,
                            "content": _emoji_media_content(emoji),
                            "sticker": True,
                            "custom_emoji": True,
                            "sniff_image": True,
                            "reply_to": None,
                        }
                    )
            if links:
                self._queue_twitter(
                    {
                        "group": self.cfg.qq.group,
                        "link": links[0],
                        "reply_id": None,
                    }
                )
        elif msgtype in QQ_FILE_TYPES:
            self._enqueue_media(
                {
                    "kind": "media",
                    "group": self.cfg.qq.group,
                    "room": room_id,
                    "event_id": event_id,
                    "sender": sender,
                    "content": content,
                    "sticker": sticker,
                    "reply_to": self._reply_event_id(content),
                }
            )

    async def _handle_reaction(
        self, event: dict, room_id: str, sender: str, label: str
    ) -> None:
        if not self.cfg.relay_reactions:
            return
        event_id = event.get("event_id", "")
        if event_id and (
            self.store.relayed_qq(event_id) or self.store.sent_count(event_id)
        ):
            log.debug("ignoring a reaction already carried to QQ: %s", event_id)
            return
        content = event.get("content") or {}
        relates = content.get("m.relates_to") or {}
        if relates.get("rel_type") != "m.annotation":
            return
        target = relates.get("event_id")
        if not isinstance(target, str) or not target:
            return
        raw_key = relates.get("key")
        emoji = format_reaction_key(raw_key, content=content, relates=relates)
        if not emoji:
            return
        reference = None if self.store.superseded(target) else self._reference_for_event(target)
        reference_event = None
        if reference is None:
            if not self._inflight_events.get(target) and not self._reference_for_event(target):
                log.debug("ignoring reaction to unbridged event %s", target)
                return
            reference_event = target
        name = await self._display_name(room_id, sender)
        source = f"[{source_tag(sender)}] "
        tag = f"[{label}] " if self._tagged else ""
        text = chunk(f"{source}{tag}{name} reacted with {emoji}")[0]
        mxc = raw_key.strip() if isinstance(raw_key, str) else ""
        if mxc.startswith("mxc://"):
            discord_meta = content.get("fi.mau.discord.reaction")
            animated = bool(
                isinstance(discord_meta, dict) and discord_meta.get("animated")
            )
            self._enqueue_media(
                {
                    "kind": "media",
                    "group": self.cfg.qq.group,
                    "room": room_id,
                    "event_id": event_id,
                    "sender": sender,
                    "content": _emoji_media_content(
                        CustomEmoji(mxc=mxc, shortcode=emoji, animated=animated)
                    ),
                    "custom_emoji": True,
                    "sniff_image": True,
                    "reaction": True,
                    "caption": text,
                    "fallback_text": text,
                    "text_required": True,
                    "reference": reference,
                    "reference_event": reference_event,
                    "reply_to": None,
                }
            )
            return
        self._put(
            {
                "kind": "text",
                "group": self.cfg.qq.group,
                "room": room_id,
                "event_id": event_id,
                "text": text,
                "reference": reference,
                "reference_event": reference_event,
            }
        )

    def _enqueue_media(self, job: dict) -> None:
        loop = asyncio.get_running_loop()
        job = {**job, "ready": loop.create_future(), "sent": asyncio.Event()}
        self._put(job)
        lane = self._videos if job["content"].get("msgtype") == "m.video" else self._images
        lane.put_nowait(job)

    def _ignored(self, sender: str) -> bool:
        if sender == self.bot_mxid or sender.startswith("@qq_"):
            return True
        for pattern in self.cfg.ignore_senders:
            if re.fullmatch(pattern, sender):
                return True
        return False

    def _reference_for_event(self, event_id: str) -> str | None:
        row = self.store.conn.execute(
            "SELECT qq_id, ref_idx FROM messages WHERE event_id = ?", (event_id,)
        ).fetchone()
        if not row:
            return None
        return row["ref_idx"] or row["qq_id"]

    @staticmethod
    def _reply_event_id(content: dict) -> str | None:
        relates = content.get("m.relates_to")
        if not isinstance(relates, dict):
            return None
        in_reply_to = relates.get("m.in_reply_to")
        if not isinstance(in_reply_to, dict):
            return None
        target = in_reply_to.get("event_id")
        return target if isinstance(target, str) and target else None

    def _reply_reference(self, event: dict) -> str | None:
        target = self._reply_event_id(event.get("content") or {})
        if not target or self.store.superseded(target) == "redacted":
            return None
        return self._reference_for_event(target)

    async def _reply_target_name(
        self, room_id: str, sender: str, content: dict, *, sticker: bool = False
    ) -> str | None:
        """Display name of the person a media/sticker event replies to, or None."""
        target_sender: str | None = None
        target = self._reply_event_id(content)
        if target:
            try:
                original = await self.matrix.event(room_id, target, self.bot_mxid)
            except (MatrixError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.debug("cannot read reply target %s in %s: %s", target, room_id, exc)
                original = None
            if isinstance(original, dict):
                candidate = original.get("sender")
                if isinstance(candidate, str) and candidate:
                    if candidate == sender:
                        return None
                    target_sender = candidate
        if not target_sender and sticker:
            mentions = content.get("m.mentions")
            user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
            if isinstance(user_ids, list):
                targets = list(
                    dict.fromkeys(
                        uid
                        for uid in user_ids
                        if isinstance(uid, str) and uid and uid != sender
                    )
                )
                if len(targets) == 1:
                    target_sender = targets[0]
        if not target_sender:
            return None
        try:
            raw_name = await self._display_name(room_id, target_sender)
        except (MatrixError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug(
                "cannot read display name for %s in %s: %s", target_sender, room_id, exc
            )
            return None
        name = " ".join(raw_name.split())
        return name or None

    async def _display_name(self, room_id: str, user_id: str) -> str:
        key = (room_id, user_id)
        cached = self._names.get(key)
        if cached and time.time() - cached[1] < 3600:
            return cached[0]
        try:
            member = await self.matrix.member(room_id, user_id)
            name = (member or {}).get("displayname")
        except MatrixError:
            name = None
        if not name:
            name = user_id[1:].split(":", 1)[0]
        self._names[key] = (name, time.time())
        return name

    async def _redact(self, event: dict) -> None:
        target = event.get("redacts") or (event.get("content") or {}).get("redacts")
        if not target:
            return
        self.store.supersede(target, "redacted")
        _, transient = await self._recall(target)
        if transient:
            # Keep the redaction for retry; the copy that QQ took back is already forgotten.
            self._retry_later()

    async def _recall(self, event_id: str) -> tuple[bool, bool]:
        """Recall every QQ message a Matrix event was sent as (a long text is several) and forget
        those QQ took back. Answers (refused, transient): whether QQ refused some for good, as for
        an old message, and whether some failed in a way worth retrying."""
        refused = transient = False
        recalled = []
        for row in self.store.qq_messages_for_event(event_id):
            try:
                await self.qq.delete_group_message(row["group_openid"], row["qq_id"])
            except QQError as exc:
                log.info("cannot recall QQ message %s: %s", row["qq_id"], exc)
                if _transient(exc):
                    transient = True
                else:
                    refused = True
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.info("cannot reach QQ to recall %s: %s", row["qq_id"], exc)
                transient = True
                continue
            recalled.append(row["qq_id"])
        self.store.forget_messages(recalled)
        return refused, transient

    async def _handle_edit(
        self, event: dict, room_id: str, sender: str, label: str, content: dict
    ) -> None:
        """A Matrix edit, by its author, of a text that is on QQ or on its way: queue a job that
        recalls the QQ copy and sends the new text under the original event, so a later edit or
        redaction finds it. From here on the original's old text is never sent again."""
        target = (content.get("m.relates_to") or {}).get("event_id")
        new = content.get("m.new_content")
        edit_id = event.get("event_id", "")
        if not isinstance(target, str) or not isinstance(new, dict) or not edit_id:
            return
        msgtype = new.get("msgtype")
        if msgtype not in ("m.text", "m.emote"):
            log.debug("ignoring an edit of a %s in %s", msgtype, room_id)
            return
        if self.store.edit_done(edit_id) or self.store.superseded(target) == "redacted":
            return
        try:
            original = await self.matrix.event(room_id, target, self.bot_mxid)
        except (MatrixError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.info("cannot read %s to check an edit; keeping it for retry: %s", target, exc)
            self._retry_later()
            return
        if not original or original.get("sender") != sender:
            log.info("ignoring an edit of %s by someone other than its author", target)
            return
        sent_ts = event.get("origin_server_ts") if isinstance(event.get("origin_server_ts"), int) else 0
        original_ts = original.get("origin_server_ts")
        recent = isinstance(original_ts, int) and sent_ts - original_ts <= EDIT_WINDOW_MS
        known = (
            self.store.qq_messages_for_event(target)
            or self.store.superseded(target) == "edited"
            or self._inflight_events.get(target)
            or recent
        )
        if not known:
            log.debug("ignoring an edit of %s, which is not on QQ", target)
            return
        html_body, custom_emojis, _ = _extract_message_emojis(new)
        body = html_body if custom_emojis else str(new.get("body") or "")
        if not body.strip():
            return
        if self.cfg.twitter and twitter.find(body):
            body = twitter.rewrite(body)
        name = await self._display_name(room_id, sender)
        source = f"[{source_tag(sender)}] "
        tag = f"[{label}] " if self._tagged else ""

        def line(who: str) -> str:
            if msgtype == "m.emote":
                return f"{source}{tag}* {who} {body}"
            return f"{source}{tag}{who}: {body}"

        self.store.supersede(target, "edited")
        self._put(
            {
                "kind": "edit",
                "group": self.cfg.qq.group,
                "room": room_id,
                "event_id": target,
                "edit_id": edit_id,
                "sent_ts": sent_ts,
                "text": line(name),
                "marked": line(f"{name} ✎"),
            }
        )

    async def _send_edit(self, job: dict) -> None:
        """Recall the original's QQ copy, then send the edited text in its place. A recall that
        failed for now keeps the edit for retry; one QQ refused for good leaves the old copy and
        marks the new text as an edit. An edit older than one already applied, or of a message
        redacted since, sends nothing."""
        target = job["event_id"]
        if self.store.superseded(target) == "redacted":
            log.debug("dropping an edit of %s, redacted since", target)
            return
        if job.get("sent_ts", 0) < self.store.newest_edit(target):
            log.debug("dropping an edit of %s older than one already applied", target)
            self.store.mark_edit_done(job["edit_id"], target, job.get("sent_ts", 0))
            return
        refused, transient = await self._recall(target)
        if transient:
            self._retry_later(job)
            return
        text = job["marked"] if refused else job["text"]
        for part in chunk(text):
            await self._send_text(
                {
                    "kind": "text",
                    "group": job["group"],
                    "room": job["room"],
                    "event_id": job["event_id"],
                    "text": part,
                    "reference": None,
                    "edit": True,
                    "pending": job.get("pending"),
                }
            )
            if job.get("pending") in self._keep:
                return
        self.store.mark_edit_done(job["edit_id"], job["event_id"], job.get("sent_ts", 0))
        log.info("relayed an edit of %s (%s)", job["event_id"], "kept" if refused else "recalled")

    # -- outbound queue ---------------------------------------------------

    def _defer_for_target(self, job: dict) -> bool:
        """Hold a reaction until an in-flight target message finishes delivering."""
        ref_event = job.get("reference_event")
        if (
            not ref_event
            or job.get("reference") is not None
            or self._reference_for_event(ref_event) is not None
            or not self._inflight_events.get(ref_event)
        ):
            return False
        if "sent" in job:
            # Release the prepare worker so a target's forced re-encode behind this
            # reaction can run without deadlocking the lane.
            job["sent"].set()
        self._inflight_waiters.setdefault(ref_event, []).append(job)
        return True

    async def _send_loop(self) -> None:
        while True:
            job = await self._queue.get()
            if job.get("pending") in self._keep:
                # An earlier part of this event failed; parts must reach QQ in
                # order, so the rest waits for the event's retry.
                if "sent" in job:
                    job["sent"].set()
                self._finish(job)
                continue
            if self._defer_for_target(job):
                continue
            deferred = False
            try:
                if job["kind"] == "text":
                    await self._send_text(job)
                elif job["kind"] == "edit":
                    await self._send_edit(job)
                else:
                    deferred = await self._send_media(job)
            except Exception:
                log.exception("failed to send to QQ")
                self._retry_later(job)
            # A cancelled send (shutdown) skips this, so its event is resumed.
            if not deferred:
                self._finish(job)

    async def _wait_limits(self, group: str) -> bool:
        limiter = self._group_limiters.setdefault(group, RateLimiter(20, 1000))
        if not await limiter.acquire():
            log.warning("daily QQ message quota for %s is used up", group)
            return False
        if not await self._global.acquire():
            return False
        return True

    async def _send_text(self, job: dict) -> bool:
        event_id = job.get("event_id")
        if event_id and not job.get("edit") and self.store.superseded(event_id):
            log.debug("dropping a queued part of %s, edited or redacted since", event_id)
            return False
        reference = job.get("reference")
        if reference is None and job.get("reference_event"):
            reference = self._reference_for_event(job["reference_event"])
            if reference is None:
                log.debug(
                    "skipping reaction; target %s was not delivered to QQ",
                    job["reference_event"],
                )
                return False
        if not await self._wait_limits(job["group"]):
            self._retry_later(job)
            return False
        while True:
            for attempt in range(3):
                try:
                    response = await self.qq.send_group_text(
                        job["group"],
                        job["text"],
                        reference=reference,
                        msg_id=job.get("msg_id"),
                    )
                except QQError as exc:
                    if exc.rate_limited and attempt < 2:
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    if (
                        reference
                        and job.get("fallback_unquoted")
                        and not _transient(exc)
                    ):
                        log.info(
                            "QQ refused reference on %s (%s); sending unquoted",
                            event_id,
                            exc,
                        )
                        reference = None
                        break
                    log.warning("cannot send to %s: %s", job["group"], exc)
                    if _transient(exc):
                        self._retry_later(job)
                    return False
                self._remember_sent(job["group"], job["room"], job["event_id"], response)
                log.info("sent to QQ group %s: %s", job["group"], response.get("id"))
                return True

    async def _prepare_loop(self, lane: asyncio.Queue[dict]) -> None:
        while True:
            job = await lane.get()
            ready: asyncio.Future = job["ready"]
            try:
                result: Prepared | str = await self._prepare_media(job)
            except Unfit as exc:
                result = str(exc)
            except Exception:
                log.exception("failed to prepare media for QQ")
                result = UNREADABLE
            if not ready.done():
                ready.set_result(result)
            # One prepared file per lane at a time keeps memory bounded.
            await job["sent"].wait()

    async def _prepare_media(self, job: dict) -> Prepared:
        content = job["content"]
        url = content.get("url") or (content.get("file") or {}).get("url")
        if not url:
            raise Unfit(UNREADABLE)
        try:
            data = await self.matrix.download(url, MAX_MATRIX_MEDIA)
        except MediaTooLarge as exc:
            raise Unfit(TOO_LARGE) from exc
        info = content.get("info") or {}
        mimetype = str(info.get("mimetype") or "")
        filename = str(content.get("filename") or content.get("body") or "file")
        if job.get("sniff_image"):
            mimetype, filename = _sniff_image_format(data, mimetype, filename)
        prepared = await prepare(
            data,
            str(content.get("msgtype")),
            mimetype,
            filename,
            force=bool(job.get("force")),
        )
        if len(prepared.data) != len(data):
            log.info(
                "shrank %s from %d to %d bytes", job["event_id"], len(data), len(prepared.data)
            )
        return prepared

    def _superseded_media(self, job: dict) -> str | None:
        event_id = job.get("event_id")
        if not event_id:
            return None
        superseded = self.store.superseded(event_id)
        if superseded == "redacted" or (superseded and job.get("custom_emoji")):
            return superseded
        return None

    async def _send_media(self, job: dict) -> bool:
        """Send prepared media; True when it was put back to wait for preparation. Media redacted
        while it waited is dropped."""
        if reason := self._superseded_media(job):
            log.debug("dropping media %s, %s since", job.get("event_id"), reason)
            job["sent"].set()
            return False
        ready: asyncio.Future = job["ready"]
        if not ready.done():
            await asyncio.wait({ready}, timeout=MEDIA_WAIT)
        if not ready.done():
            # Let later messages pass; the media rejoins the queue once ready.
            ready.add_done_callback(lambda _: self._queue.put_nowait(job))
            return True
        try:
            if reason := self._superseded_media(job):
                log.debug("dropping media %s, %s while preparing", job.get("event_id"), reason)
                return False
            await self._deliver_media(job, ready.result())
        finally:
            job["sent"].set()
        return False

    async def _sender_prefix(self, room_id: str, sender: str) -> str:
        name = await self._display_name(room_id, sender)
        source = f"[{source_tag(sender)}] "
        label = self._labels.get(room_id)
        tag = f"[{label}] " if self._tagged and label else ""
        return f"{source}{tag}{name}"

    def _handle_media_error(self, job: dict, exc: QQError, action: str) -> bool:
        """Return True when the failure triggered a re-encode or retry, False if permanent."""
        if exc.code == QQ_FORMAT_REFUSED and not job.get("force"):
            log.info("QQ refused the format of %s; re-encoding", job["event_id"])
            retry = {
                key: value
                for key, value in job.items()
                if key not in ("ready", "sent")
            }
            self._enqueue_media({**retry, "force": True})
            return True
        log.warning(
            "cannot %s media to %s: %s (trace %s)",
            action,
            job["group"],
            exc,
            exc.trace_id,
        )
        if _transient(exc):
            self._retry_later(job)
            return True
        return False

    async def _deliver_media(self, job: dict, result: Prepared | str) -> None:
        reference = job.get("reference")
        if reference is None and job.get("reference_event"):
            reference = self._reference_for_event(job["reference_event"])
            if reference is None:
                log.debug(
                    "skipping reaction; target %s was not delivered to QQ",
                    job["reference_event"],
                )
                return
        elif reference is None and not job.get("reaction"):
            reply_to = job.get("reply_to") or self._reply_event_id(job["content"])
            reference = (
                self._reference_for_event(reply_to)
                if reply_to and self.store.superseded(reply_to) != "redacted"
                else None
            )

        custom_caption = job.get("caption")
        fallback_text = job.get("fallback_text")
        text_required = bool(job.get("text_required") and fallback_text)
        media_caption = ""
        if (
            custom_caption is None
            and not job.get("sticker")
            and not job.get("custom_emoji")
            and not job.get("skip_media_caption")
        ):
            media_caption = _media_caption(job["content"])
            if media_caption and self.cfg.twitter and twitter.find(media_caption):
                media_caption = twitter.rewrite(media_caption)

        speaker = ""
        if custom_caption is None:
            prefix = await self._sender_prefix(job["room"], job["sender"])
            needs_caption = (
                isinstance(result, str)
                or result.file_type != QQ_AUDIO
                or bool(media_caption)
            )
            reply_name = (
                await self._reply_target_name(
                    job["room"],
                    job["sender"],
                    job["content"],
                    sticker=bool(job.get("sticker")),
                )
                if needs_caption
                else None
            )
            speaker = f"{prefix} → {reply_name}" if reply_name else prefix

        caption_parts: list[str] = []
        if media_caption:
            caption_parts = chunk(f"{speaker}: {media_caption}")

        if isinstance(result, str):
            if fallback_text:
                failure_parts = [fallback_text]
            elif job.get("sticker"):
                notice = _sticker_label(job["content"].get("body"), reason=result)
                failure_parts = [chunk(f"{speaker}: {notice}")[0]]
            else:
                kind = str(job["content"].get("msgtype", "m.file"))[2:]
                notice = f"[{kind} {result}]"
                failure_parts = (
                    chunk(f"{speaker}: {media_caption}\n{notice}")
                    if media_caption
                    else [chunk(f"{speaker}: {notice}")[0]]
                )
            for idx, part in enumerate(failure_parts):
                await self._send_text(
                    {
                        "group": job["group"],
                        "room": job["room"],
                        "event_id": job["event_id"],
                        "text": part,
                        "reference": reference if idx == 0 else None,
                        "fallback_unquoted": not job.get("reaction"),
                        "pending": job.get("pending"),
                    }
                )
                if job.get("pending") in self._keep:
                    return
            return
        if not await self._wait_limits(job["group"]):
            self._retry_later(job)
            return
        emoji_only_fallback = (
            chunk(f"{speaker}: {job['content'].get('body') or '[custom emoji]'}")[0]
            if job.get("emoji_only") and speaker
            else None
        )
        # QQ's official SDK passes `content` alongside `media` for image/video/file
        # (msg_type 7) to embed text and media in one message, and omits it for voice.
        separate_caption_parts: list[str] = []
        if custom_caption is not None:
            caption: str | None = custom_caption
        elif caption_parts and result.file_type != QQ_AUDIO and len(caption_parts) == 1:
            caption = caption_parts[0]
            fallback_text = caption_parts[0]
            text_required = True
        else:
            caption = chunk(f"{speaker}:")[0] if result.file_type != QQ_AUDIO else None
            if caption_parts:
                separate_caption_parts = caption_parts
        try:
            file_info = await self.qq.upload_group_media(
                job["group"], result.data, result.filename, result.file_type
            )
        except QQError as exc:
            if not self._handle_media_error(job, exc, "upload"):
                permanent_fallback = fallback_text if text_required else emoji_only_fallback
                if permanent_fallback:
                    await self._send_text(
                        {
                            "group": job["group"],
                            "room": job["room"],
                            "event_id": job["event_id"],
                            "text": permanent_fallback,
                            "reference": reference,
                            "fallback_unquoted": not job.get("reaction"),
                            "pending": job.get("pending"),
                        }
                    )
                elif separate_caption_parts:
                    for idx, part in enumerate(separate_caption_parts):
                        await self._send_text(
                            {
                                "group": job["group"],
                                "room": job["room"],
                                "event_id": job["event_id"],
                                "text": part,
                                "reference": reference if idx == 0 else None,
                                "fallback_unquoted": True,
                                "pending": job.get("pending"),
                            }
                        )
                        if job.get("pending") in self._keep:
                            return
            return

        if text_required:
            attempts: list[tuple[str | None, str | None]] = [(caption, reference)]
            if reference is not None and not job.get("reaction"):
                attempts.append((caption, None))
            response: dict | None = None
            for index, (send_content, send_ref) in enumerate(attempts):
                try:
                    response = await self.qq.send_group_media(
                        job["group"], file_info, content=send_content, reference=send_ref
                    )
                    break
                except QQError as exc:
                    if _transient(exc) or exc.code == QQ_FORMAT_REFUSED:
                        if not self._handle_media_error(job, exc, "send"):
                            await self._send_text(
                                {
                                    "group": job["group"],
                                    "room": job["room"],
                                    "event_id": job["event_id"],
                                    "text": fallback_text,
                                    "reference": reference,
                                    "fallback_unquoted": not job.get("reaction"),
                                    "pending": job.get("pending"),
                                }
                            )
                        return
                    if index < len(attempts) - 1:
                        log.info(
                            "QQ refused media send for %s (caption=%s, reference=%s): %s; retrying",
                            job["event_id"],
                            bool(send_content),
                            bool(send_ref),
                            exc,
                        )
                        continue
                    log.info(
                        "QQ refused captioned media for %s (%s); sending text and image separately",
                        job["event_id"],
                        exc,
                    )
                    sent_ok = await self._send_text(
                        {
                            "group": job["group"],
                            "room": job["room"],
                            "event_id": job["event_id"],
                            "text": fallback_text,
                            "reference": reference,
                            "fallback_unquoted": not job.get("reaction"),
                            "pending": job.get("pending"),
                        }
                    )
                    if not sent_ok or job.get("pending") in self._keep:
                        return
                    for bare_attempt in range(3):
                        try:
                            response = await self.qq.send_group_media(
                                job["group"], file_info, content=None, reference=None
                            )
                            break
                        except QQError as media_exc:
                            if media_exc.code == QQ_FORMAT_REFUSED and not job.get("force"):
                                retry = {
                                    key: value
                                    for key, value in job.items()
                                    if key
                                    not in (
                                        "ready",
                                        "sent",
                                        "caption",
                                        "fallback_text",
                                        "text_required",
                                    )
                                }
                                self._enqueue_media(
                                    {**retry, "force": True, "skip_media_caption": True}
                                )
                                return
                            if _transient(media_exc) and bare_attempt < 2:
                                await asyncio.sleep(
                                    3 * (bare_attempt + 1)
                                    if media_exc.rate_limited
                                    else 0.5
                                )
                                continue
                            log.warning(
                                "cannot send uncaptioned media for %s: %s",
                                job["event_id"],
                                media_exc,
                            )
                            return
                    break
            assert response is not None
            self._remember_sent(job["group"], job["room"], job["event_id"], response)
            log.info("sent media to QQ group %s: %s", job["group"], response.get("id"))
            return

        attempts = [(caption, reference)]
        if caption is not None and reference is not None:
            attempts.append((caption, None))
            attempts.append((None, reference))
            attempts.append((None, None))
        elif reference is not None or caption is not None:
            attempts.append((None, None))
        response = None
        for index, (send_content, send_ref) in enumerate(attempts):
            try:
                response = await self.qq.send_group_media(
                    job["group"], file_info, content=send_content, reference=send_ref
                )
                break
            except QQError as exc:
                if (
                    index == len(attempts) - 1
                    or _transient(exc)
                    or exc.code == QQ_FORMAT_REFUSED
                ):
                    if not self._handle_media_error(job, exc, "send"):
                        if emoji_only_fallback:
                            await self._send_text(
                                {
                                    "group": job["group"],
                                    "room": job["room"],
                                    "event_id": job["event_id"],
                                    "text": emoji_only_fallback,
                                    "reference": reference,
                                    "fallback_unquoted": True,
                                    "pending": job.get("pending"),
                                }
                            )
                        elif separate_caption_parts:
                            for idx, part in enumerate(separate_caption_parts):
                                await self._send_text(
                                    {
                                        "group": job["group"],
                                        "room": job["room"],
                                        "event_id": job["event_id"],
                                        "text": part,
                                        "reference": reference if idx == 0 else None,
                                        "fallback_unquoted": True,
                                        "pending": job.get("pending"),
                                    }
                                )
                                if job.get("pending") in self._keep:
                                    return
                    return
                log.info(
                    "QQ refused media send for %s (caption=%s, reference=%s): %s; retrying",
                    job["event_id"],
                    bool(send_content),
                    bool(send_ref),
                    exc,
                )
        assert response is not None
        self._remember_sent(job["group"], job["room"], job["event_id"], response)
        log.info("sent media to QQ group %s: %s", job["group"], response.get("id"))
        for idx, part in enumerate(separate_caption_parts):
            await self._send_text(
                {
                    "group": job["group"],
                    "room": job["room"],
                    "event_id": job["event_id"],
                    "text": part,
                    "reference": reference if idx == 0 and result.file_type == QQ_AUDIO else None,
                    "fallback_unquoted": True,
                    "pending": job.get("pending"),
                }
            )
            if job.get("pending") in self._keep:
                return

    def _remember_sent(
        self, group: str, room: str, event_id: str, response: dict
    ) -> None:
        if not room or event_id.startswith("command-"):
            return
        qq_id = response.get("id")
        if not qq_id:
            return
        ref_idx = (response.get("ext_info") or {}).get("ref_idx")
        self.store.add_message(qq_id, group, room, event_id, ref_idx, "matrix")

    # -- QQ -> Matrix -----------------------------------------------------

    async def on_qq_event(self, event_type: str, data: dict) -> None:
        if not isinstance(data, dict):
            log.info("QQ event %s: %s", _safe_log_str(str(event_type), 64), _summarize_qq_payload(data))
            return
        if event_type in ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            author = data.get("author") if isinstance(data.get("author"), dict) else {}
            extra_keys = sorted(
                _safe_log_str(str(k), 40)
                for k in data
                if k not in _STANDARD_QQ_MESSAGE_KEYS
            )[:8]
            scene = data.get("message_scene")
            scene_source = (
                _safe_log_str(scene["source"], 40)
                if isinstance(scene, dict) and isinstance(scene.get("source"), str)
                else None
            )
            scene_ext = scene.get("ext") if isinstance(scene, dict) else None
            scene_ext_keys = sorted(
                {
                    _safe_log_str(item.partition("=")[0], 40)
                    for item in scene_ext[:32]
                    if isinstance(item, str)
                    and "=" in item
                    and item.partition("=")[0] not in ("msg_idx", "ref_msg_idx")
                    and not any(
                        part in item.partition("=")[0].lower()
                        for part in _SENSITIVE_QQ_KEY_PARTS
                    )
                }
                if isinstance(scene_ext, list)
                else ()
            )[:8]
            extra_parts: list[str] = []
            if scene_source and scene_source != "default":
                extra_parts.append(f"scene={scene_source}")
            if scene_ext_keys:
                extra_parts.append(f"ext_keys={scene_ext_keys}")
            if extra_keys:
                extra_parts.append(f"extra_keys={extra_keys}")
            extra = _safe_log_str(" " + " ".join(extra_parts), 200) if extra_parts else ""
            raw_type = data.get("message_type")
            msg_type = (
                raw_type
                if isinstance(raw_type, (int, str)) and not isinstance(raw_type, bool)
                else (None if raw_type is None else f"<{type(raw_type).__name__}>")
            )
            log.info(
                "QQ %s group=%s author=%s type=%s%s",
                event_type,
                _safe_log_scalar(data.get("group_openid")),
                _safe_log_scalar(author.get("username")),
                _safe_log_scalar(msg_type, 20),
                extra,
            )
            await self._on_group_message(data)
        elif event_type == "GROUP_ADD_ROBOT":
            log.info("added to QQ group %s", _safe_log_scalar(data.get("group_openid")))
        elif event_type == "GROUP_DEL_ROBOT":
            log.info("removed from QQ group %s", _safe_log_scalar(data.get("group_openid")))
        else:
            log.info(
                "QQ event %s: %s",
                _safe_log_str(str(event_type), 64),
                _summarize_qq_payload(data),
            )

    async def _on_group_message(self, data: dict) -> None:
        group = data.get("group_openid")
        author = data.get("author") or {}
        if not group or author.get("bot"):
            return
        if group != self.cfg.qq.group:
            log.info("ignoring QQ group %s, which is not the bridged group", group)
            return
        member_id = author.get("member_openid") or author.get("id")
        if not member_id:
            return
        name = author.get("username") or str(member_id)[:8]
        self.store.set_member(group, member_id, name, author.get("member_role"))
        content = str(data.get("content") or "")
        command_text = strip_leading_mentions(content)
        if command_text.startswith("!qq"):
            await self._command(group, command_text, str(data.get("id") or "") or None)
            return
        room_id = self._incoming
        if room_id is None:
            log.warning(
                "QQ message for %s cannot be posted: %s is not resolved",
                group,
                self.cfg.qq.incoming,
            )
            return
        ghost = await self._ghost(group, room_id, member_id, name)
        qq_id = str(data.get("id") or "")
        if qq_id and self.store.event_for_qq(qq_id):
            log.debug("dropping redelivered QQ message %s", qq_id)
            return
        ref_idx = self._scene_index(data, "msg_idx")
        reply_to = next(
            (
                event
                for quoted in self._quoted_indexes(data)
                if (event := self.store.event_for_ref(group, quoted))
            ),
            None,
        )
        sent: list[str] = []
        attachments = [
            item for item in data.get("attachments") or [] if isinstance(item, dict)
        ]
        image_attachments = sum(
            1
            for item in attachments
            if str(item.get("content_type") or "").startswith("image/")
        )
        sticker_image = image_attachments > 0
        forwarded_attachments = extract_forwarded_attachments(
            content, data.get("mentions")
        )
        if forwarded_attachments:
            existing_urls = {
                normalize_media_url_key(str(item.get("url") or ""))
                for item in attachments
                if item.get("url")
            }
            for item in forwarded_attachments:
                key = normalize_media_url_key(str(item.get("url") or ""))
                if key not in existing_urls:
                    existing_urls.add(key)
                    attachments.append(item)

        face_images = await self._resolve_faces(content)
        body, html_body = render_qq_text(
            content,
            data.get("mentions"),
            self.domain,
            face_images,
            sticker_image=sticker_image,
        )
        fallback_body, fallback_html = ("", "")
        sticker_fallback = ""
        if sticker_image:
            fallback_body, fallback_html = render_qq_text(
                content, data.get("mentions"), self.domain, face_images
            )
            sticker_fallback = sticker_labels(content)
        text_sent = False
        if body:
            parts = chunk(body, MATRIX_TEXT_LIMIT)
            for index, part in enumerate(parts):
                payload: dict = {"msgtype": "m.text", "body": part}
                # Markup cannot be mapped onto the parts, so it is kept when the text needs one message.
                if html_body and len(parts) == 1:
                    payload["format"] = "org.matrix.custom.html"
                    payload["formatted_body"] = html_body
                if reply_to and index == 0:
                    payload["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
                try:
                    event_id = await self.matrix.send_message(room_id, ghost, payload)
                    sent.append(event_id)
                    text_sent = True
                except MatrixError as exc:
                    log.warning("cannot send to %s as %s: %s", room_id, ghost, exc)
                    break

        image_delivered = 0
        for attachment in attachments:
            att_reply = None if (attachment.get("trusted_qq_media") and text_sent) else reply_to
            try:
                event_id = await self._send_attachment(
                    room_id, ghost, attachment, reply_to=att_reply
                )
                if event_id:
                    if str(attachment.get("content_type") or "").startswith("image/"):
                        image_delivered += 1
                    if not sent:
                        sent.append(event_id)
            except Exception as exc:
                log.warning("cannot relay attachment: %s", exc)

        if sticker_fallback and image_delivered < image_attachments:
            fallback = sticker_fallback if text_sent else fallback_body
            fallback_parts = chunk(fallback, MATRIX_TEXT_LIMIT)
            for index, part in enumerate(fallback_parts):
                payload = {"msgtype": "m.text", "body": part}
                if not text_sent and fallback_html and len(fallback_parts) == 1:
                    payload["format"] = "org.matrix.custom.html"
                    payload["formatted_body"] = fallback_html
                if reply_to and index == 0:
                    payload["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
                try:
                    event_id = await self.matrix.send_message(room_id, ghost, payload)
                    sent.append(event_id)
                except Exception as exc:
                    log.warning(
                        "cannot send sticker fallback to %s as %s: %s",
                        room_id,
                        ghost,
                        exc,
                    )
                    break

        if qq_id:
            self.store.add_message(
                qq_id, group, room_id, sent[0] if sent else None, ref_idx, "qq"
            )
            links = twitter.find(body) if self.cfg.twitter else []
            if links:
                self._queue_twitter(
                    {
                        "group": group,
                        "link": links[0],
                        "reply_id": qq_id or None,
                    }
                )

    # -- link expansion ---------------------------------------------------

    async def _twitter_loop(self) -> None:
        while True:
            job = await self._twitter.get()
            delivered = False
            try:
                delivered = await self._expand_tweet(job)
            except Exception:
                log.exception("failed to expand a tweet")
            if not delivered:
                self._retry_later(job)
            self._finish(job)

    async def _expand_tweet(self, job: dict) -> bool:
        """Expand one link; False asks the pending-event retry to keep it."""
        link: twitter.Link = job["link"]
        group = job["group"]
        if self.store.tweet_recent(group, link.status, 600):
            log.debug("tweet %s was expanded recently", link.status)
            return True
        post = await twitter.fetch(self.matrix.session, link)
        if post is None:
            # A deleted or protected post will not appear on a retry either.
            return True
        delivered = await self._send_tweet_post(post, group, job.get("reply_id"))
        if delivered:
            self.store.mark_tweet(group, link.status)
        return delivered

    async def _send_tweet_post(
        self, post: twitter.Post, group: str, reply_id: str | None = None
    ) -> bool:
        """Send the post; True once its text reached QQ, whatever the media did.

        A QQ-origin post answers the member's message with `msg_id` and a fresh
        `msg_seq`, because the group API rejects proactive pushes and ignores
        `message_reference`.
        """
        text = twitter.format_post(post)
        seq = 1
        delivered = False
        for part in chunk(text):
            if reply_id and seq > 5:
                break
            if not await self._wait_limits(group):
                return delivered
            for attempt in range(3):
                try:
                    await self.qq.send_group_text(
                        group,
                        part,
                        msg_id=reply_id,
                        msg_seq=seq,
                    )
                except QQError as exc:
                    if exc.rate_limited and attempt < 2:
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    # A retry would repeat the chunks already in the group, so
                    # settle once any text arrived and let the rest go.
                    log.warning("cannot send an expanded tweet to %s: %s", group, exc)
                    return delivered
                break
            else:
                return delivered
            delivered = True
            if reply_id:
                seq += 1
        if not delivered:
            return False
        log.info("expanded tweet %s for %s", post.link.status, group)
        if post.sensitive:
            return True
        for attachment in twitter.attachments(post):
            if reply_id and seq > 5:
                break
            downloaded = await twitter.download(self.matrix.session, attachment)
            if downloaded is None:
                continue
            data, content_type = downloaded
            if attachment.kind == "image":
                msgtype = "m.image"
                mimetype = content_type
                suffix = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }.get(content_type, ".jpg")
                filename = f"tweet{suffix}"
            else:
                msgtype = "m.video"
                mimetype = "video/mp4"
                filename = "tweet.mp4"
            try:
                prepared = await prepare(data, msgtype, mimetype, filename)
            except Unfit as exc:
                log.info(
                    "cannot prepare tweet %s %s: %s",
                    attachment.kind,
                    attachment.url,
                    exc,
                )
                continue
            if not await self._wait_limits(group):
                return True
            for attempt in range(2):
                try:
                    info = await self.qq.upload_group_media(
                        group, prepared.data, prepared.filename, prepared.file_type
                    )
                    await self.qq.send_group_media(
                        group, info, msg_id=reply_id, msg_seq=seq
                    )
                except QQError as exc:
                    if exc.code == QQ_FORMAT_REFUSED and attempt == 0:
                        log.info(
                            "QQ refused the format of tweet %s %s; re-encoding",
                            attachment.kind,
                            attachment.url,
                        )
                        try:
                            prepared = await prepare(
                                data, msgtype, mimetype, filename, force=True
                            )
                        except Unfit as unfit:
                            log.info(
                                "cannot re-encode tweet %s %s: %s",
                                attachment.kind,
                                attachment.url,
                                unfit,
                            )
                            break
                        continue
                    log.warning(
                        "cannot send tweet %s to %s: %s",
                        attachment.kind,
                        group,
                        exc,
                    )
                    break
                break
            if reply_id:
                seq += 1
        return True

    async def _resolve_faces(self, content: str) -> dict[int, str]:
        if "<faceType=" not in content:
            return {}
        images: dict[int, str] = {}
        for face_id in set(extract_face_ids(content)):
            mxc = await self._face_mxc(face_id)
            if mxc:
                images[face_id] = mxc
        return images

    async def _face_mxc(self, face_id: int) -> str | None:
        cached = self.store.face_mxc(face_id)
        if cached:
            return cached
        for template in FACE_MIRRORS:
            url = template.format(face_id=face_id)
            try:
                async with self.matrix.session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        continue
                    content_type = response.headers.get("content-type", "")
                    if not content_type.startswith("image/"):
                        continue
                    data = await response.read()
            except Exception as exc:
                log.debug("cannot download QQ face %s: %s", face_id, exc)
                continue
            if len(data) < 100:
                continue
            try:
                mxc = await self.matrix.upload(
                    data, "image/png", f"qq-face-{face_id}.png", self.bot_mxid
                )
            except MatrixError as exc:
                log.debug("cannot upload QQ face %s: %s", face_id, exc)
                return None
            self.store.set_face(face_id, mxc)
            return mxc
        return None

    async def _ghost(
        self, group: str, room_id: str, member_id: str, name: str
    ) -> str:
        localpart = f"qq_{str(member_id).lower()}"
        mxid = f"@{localpart}:{self.domain}"
        known = self._ghosts.get(mxid)
        if known is None:
            try:
                await self.matrix.register_user(localpart, name)
            except Exception as exc:
                log.debug("cannot register %s: %s", mxid, exc)
            try:
                await self.matrix.set_displayname(mxid, name)
            except Exception as exc:
                log.debug("cannot set display name for %s: %s", mxid, exc)
            self._ghosts[mxid] = name
        elif known != name:
            try:
                await self.matrix.set_displayname(mxid, name)
            except Exception:
                pass
            self._ghosts[mxid] = name
        if self._avatar and mxid not in self._avatars:
            try:
                await self.matrix.set_avatar(mxid, self._avatar)
                self._avatars.add(mxid)
            except Exception as exc:
                log.debug("cannot set avatar for %s: %s", mxid, exc)
        key = (mxid, room_id)
        if key not in self._joined:
            try:
                await self.matrix.join(room_id, mxid)
                self._joined.add(key)
            except MatrixError as exc:
                log.warning("cannot join %s to %s: %s", mxid, room_id, exc)
        return mxid

    async def _send_attachment(
        self,
        room_id: str,
        ghost: str,
        attachment: dict,
        *,
        reply_to: str | None = None,
    ) -> str | None:
        url = attachment.get("url") or attachment.get("voice_wav_url")
        if not url:
            return None
        content_type = str(attachment.get("content_type") or "")
        if content_type == "voice":
            content_type = "audio/wav" if attachment.get("voice_wav_url") else "audio/silk"
        if not content_type:
            content_type = "application/octet-stream"
        trusted_only = bool(attachment.get("trusted_qq_media"))
        current_url = str(url)
        data = b""
        resp_content_type = ""
        for _hop in range(3):
            if trusted_only and not is_trusted_qq_media_url(current_url):
                raise RuntimeError("refusing untrusted forwarded attachment URL")
            get_kwargs = {"allow_redirects": False} if trusted_only else {}
            async with self.matrix.session.get(current_url, **get_kwargs) as response:
                if trusted_only and response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location") or ""
                    if not location or not is_trusted_qq_media_url(location):
                        raise RuntimeError("refusing redirect to untrusted attachment URL")
                    current_url = location
                    continue
                if response.status >= 400:
                    raise RuntimeError(f"attachment download failed: HTTP {response.status}")
                resp_content_type = (
                    (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                )
                data = await response.read()
                break
        else:
            raise RuntimeError("too many redirects while downloading attachment")
        if not data:
            raise RuntimeError("attachment download returned empty body")
        if len(data) > MAX_MEDIA_DOWNLOAD:
            raise RuntimeError("attachment is too large")
        if resp_content_type and (
            resp_content_type.startswith(("image/", "video/", "audio/"))
            or content_type == "application/octet-stream"
        ):
            content_type = resp_content_type
        filename = str(attachment.get("filename") or "file")
        mxc = await self.matrix.upload(data, content_type, filename, ghost)
        if content_type.startswith("image/"):
            msgtype = "m.image"
        elif content_type.startswith("video/"):
            msgtype = "m.video"
        elif content_type.startswith("audio/"):
            msgtype = "m.audio"
        else:
            msgtype = "m.file"
        info: dict = {"mimetype": content_type, "size": len(data)}
        if attachment.get("width"):
            info["w"] = int(attachment["width"])
        if attachment.get("height"):
            info["h"] = int(attachment["height"])
        payload: dict = {
            "msgtype": msgtype,
            "body": filename,
            "url": mxc,
            "info": info,
        }
        if reply_to:
            payload["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        if msgtype == "m.audio" and attachment.get("voice_wav_url"):
            payload["org.matrix.msc3245.voice"] = {}
        return await self.matrix.send_message(room_id, ghost, payload)

    @staticmethod
    def _scene_index(data: dict, key: str) -> str | None:
        """One `key=REFIDX_…` entry of `message_scene.ext`: `msg_idx` names the message itself,
        `ref_msg_idx` the message it quotes. Field meanings and parsing follow Tencent's
        openclaw-qqbot `src/types.ts` and its SDK @tencent-connect/qqbot-nodejs 1.0.6,
        `parseRefIndices` in `src/protocol/utils/text-parsing.ts`."""
        for item in (data.get("message_scene") or {}).get("ext") or []:
            if isinstance(item, str) and item.startswith(key + "="):
                return item[len(key) + 1 :].strip() or None
        return None

    @classmethod
    def _quoted_indexes(cls, data: dict) -> list[str]:
        """The indexes a QQ reply may quote, best first: for a quote message (type 103) its first
        element, which holds the quoted message and which the SDK ranks above the scene, then
        the scene's `ref_msg_idx`."""
        found: list[str] = []
        elements = data.get("msg_elements") or []
        if data.get("message_type") == 103 and elements and isinstance(elements[0], dict):
            element = str(elements[0].get("msg_idx") or "").strip()
            if element:
                found.append(element)
        scene = cls._scene_index(data, "ref_msg_idx")
        if scene and scene not in found:
            found.append(scene)
        return found

    async def _command(
        self, group: str, content: str, msg_id: str | None = None
    ) -> None:
        parts = content.split()
        command = parts[1] if len(parts) > 1 else ""
        if command == "id":
            self._reply(group, f"group_openid: {group}", msg_id)
        elif command == "status":
            self._reply(group, "qq-bridge is running", msg_id)

    def _reply(self, group: str, text: str, msg_id: str | None = None) -> None:
        self._queue.put_nowait(
            {
                "kind": "text",
                "group": group,
                "room": "",
                "event_id": f"command-{uuid.uuid4().hex}",
                "text": text,
                "reference": None,
                "msg_id": msg_id,
            }
        )

    # -- testing ----------------------------------------------------------

    async def inject_qq_message(
        self, group: str, member_id: str, name: str, text: str
    ) -> None:
        await self._on_group_message(
            {
                "id": f"sim-{uuid.uuid4().hex}",
                "group_openid": group,
                "author": {
                    "member_openid": member_id,
                    "username": name,
                    "bot": False,
                },
                "content": text,
                "mentions": [],
            }
        )
