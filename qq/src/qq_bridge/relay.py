"""Relay logic between the QQ group and the Matrix rooms it mirrors."""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import logging
import re
import time
import uuid
from pathlib import Path

from . import twitter
from .config import Config
from .fit import QQ_FILE_TYPES, TOO_LARGE, UNREADABLE, Prepared, Unfit, prepare, sweep_temp
from .formatting import (
    chunk,
    extract_face_ids,
    format_reaction_key,
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


def _transient(exc: QQError) -> bool:
    """A refusal that may pass: QQ overloaded, rate limited or failing."""
    return exc.rate_limited or (exc.status or 0) >= 500


def is_edit(content: dict) -> bool:
    """Matrix sends edits as ordinary messages carrying m.new_content."""
    if "m.new_content" in content:
        return True
    return (content.get("m.relates_to") or {}).get("rel_type") == "m.replace"


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
        if event_type == "m.sticker":
            # A sticker has no msgtype; send it as the image or video it holds.
            mimetype = str((content.get("info") or {}).get("mimetype") or "")
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
        # A long text posts several QQ messages; after a restart only the parts
        # not yet posted are sent.
        sent = self.store.sent_count(event_id) if event_id else 0
        if event_id and (
            self.store.relayed_qq(event_id)
            or (sent and msgtype not in ("m.text", "m.emote"))
        ):
            log.debug("ignoring an event already carried to QQ: %s", event_id)
            return

        if msgtype in ("m.text", "m.emote"):
            body = strip_reply_fallback(str(content.get("body", "")))
            if not body.strip():
                return
            links = twitter.find(body) if self.cfg.twitter else []
            if links:
                body = twitter.rewrite(body)
            name = await self._display_name(room_id, sender)
            source = f"[{source_tag(sender)}] "
            tag = f"[{label}] " if self._tagged else ""
            text = (
                f"{source}{tag}* {name} {body}"
                if msgtype == "m.emote"
                else f"{source}{tag}{name}: {body}"
            )
            reference = self._reply_reference(event)
            for index, part in enumerate(chunk(text)):
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
        emoji = format_reaction_key(relates.get("key"), content=content, relates=relates)
        if not emoji:
            return
        reference = self._reference_for_event(target)
        reference_event = None
        if reference is None:
            if not self._inflight_events.get(target):
                log.debug("ignoring reaction to unbridged event %s", target)
                return
            reference_event = target
        name = await self._display_name(room_id, sender)
        source = f"[{source_tag(sender)}] "
        tag = f"[{label}] " if self._tagged else ""
        text = chunk(f"{source}{tag}{name} reacted with {emoji}")[0]
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

    def _reply_reference(self, event: dict) -> str | None:
        relates = (event.get("content") or {}).get("m.relates_to") or {}
        target = (relates.get("m.in_reply_to") or {}).get("event_id")
        if not target:
            return None
        return self._reference_for_event(target)

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
        await self._recall(target)

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
        known = (
            self.store.qq_messages_for_event(target)
            or self.store.superseded(target) == "edited"
            or self._inflight_events.get(target)
        )
        if not known:
            log.debug("ignoring an edit of %s, which is not on QQ", target)
            return
        original = await self.matrix.event(room_id, target, self.bot_mxid)
        if not original or original.get("sender") != sender:
            log.info("ignoring an edit of %s by someone other than its author", target)
            return
        body = str(new.get("body") or "")
        if not body.strip():
            return
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
                "text": line(name),
                "marked": line(f"{name} ✎"),
            }
        )

    async def _send_edit(self, job: dict) -> None:
        """Recall the original's QQ copy, then send the edited text in its place. A recall that
        failed for now keeps the edit for retry; one QQ refused for good leaves the old copy and
        marks the new text as an edit."""
        refused, transient = await self._recall(job["event_id"])
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
        self.store.mark_edit_done(job["edit_id"], job["event_id"])
        log.info("relayed an edit of %s (%s)", job["event_id"], "kept" if refused else "recalled")

    # -- outbound queue ---------------------------------------------------

    async def _send_loop(self) -> None:
        while True:
            job = await self._queue.get()
            if job.get("pending") in self._keep:
                # An earlier part of this event failed; parts must reach QQ in
                # order, so the rest waits for the event's retry.
                self._finish(job)
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

    async def _send_text(self, job: dict) -> None:
        event_id = job.get("event_id")
        if event_id and not job.get("edit") and self.store.superseded(event_id):
            log.debug("dropping a queued part of %s, edited or redacted since", event_id)
            return
        reference = job.get("reference")
        if reference is None and job.get("reference_event"):
            reference = self._reference_for_event(job["reference_event"])
            if reference is None:
                log.debug(
                    "skipping reaction; target %s was not delivered to QQ",
                    job["reference_event"],
                )
                return
        if not await self._wait_limits(job["group"]):
            self._retry_later(job)
            return
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
                log.warning("cannot send to %s: %s", job["group"], exc)
                if _transient(exc):
                    self._retry_later(job)
                return
            self._remember_sent(job["group"], job["room"], job["event_id"], response)
            log.info("sent to QQ group %s: %s", job["group"], response.get("id"))
            return

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
        prepared = await prepare(
            data,
            str(content.get("msgtype")),
            str(info.get("mimetype") or ""),
            str(content.get("filename") or content.get("body") or "file"),
            force=bool(job.get("force")),
        )
        if len(prepared.data) != len(data):
            log.info(
                "shrank %s from %d to %d bytes", job["event_id"], len(data), len(prepared.data)
            )
        return prepared

    async def _send_media(self, job: dict) -> bool:
        """Send prepared media; True when it was put back to wait for preparation."""
        ready: asyncio.Future = job["ready"]
        if not ready.done():
            await asyncio.wait({ready}, timeout=MEDIA_WAIT)
        if not ready.done():
            # Let later messages pass; the media rejoins the queue once ready.
            ready.add_done_callback(lambda _: self._queue.put_nowait(job))
            return True
        try:
            await self._deliver_media(job, ready.result())
        finally:
            job["sent"].set()
        return False

    async def _deliver_media(self, job: dict, result: Prepared | str) -> None:
        if isinstance(result, str):
            name = await self._display_name(job["room"], job["sender"])
            kind = str(job["content"].get("msgtype", "m.file"))[2:]
            await self._send_text(
                {
                    "group": job["group"],
                    "room": job["room"],
                    "event_id": job["event_id"],
                    "text": f"[{source_tag(job['sender'])}] {name}: [{kind} {result}]",
                    "reference": None,
                    "pending": job.get("pending"),
                }
            )
            return
        if not await self._wait_limits(job["group"]):
            self._retry_later(job)
            return
        try:
            file_info = await self.qq.upload_group_media(
                job["group"], result.data, result.filename, result.file_type
            )
            response = await self.qq.send_group_media(job["group"], file_info)
        except QQError as exc:
            if exc.code == QQ_FORMAT_REFUSED and not job.get("force"):
                log.info("QQ refused the format of %s; re-encoding", job["event_id"])
                retry = {
                    key: value
                    for key, value in job.items()
                    if key not in ("ready", "sent")
                }
                self._enqueue_media({**retry, "force": True})
                return
            log.warning(
                "cannot send media to %s: %s (trace %s)",
                job["group"],
                exc,
                exc.trace_id,
            )
            if _transient(exc):
                self._retry_later(job)
            return
        self._remember_sent(job["group"], job["room"], job["event_id"], response)
        log.info("sent media to QQ group %s: %s", job["group"], response.get("id"))

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
        if event_type in ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            author = data.get("author") or {}
            log.info(
                "QQ %s group=%s author=%s",
                event_type,
                data.get("group_openid"),
                author.get("username"),
            )
            await self._on_group_message(data)
        elif event_type == "GROUP_ADD_ROBOT":
            log.info("added to QQ group %s", data.get("group_openid"))
        elif event_type == "GROUP_DEL_ROBOT":
            log.info("removed from QQ group %s", data.get("group_openid"))
        else:
            log.debug("QQ event %s", event_type)

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
        ref_idx = self._ref_idx(data)
        reply_to = self.store.event_for_ref(group, ref_idx) if ref_idx else None
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
            try:
                event_id = await self._send_attachment(
                    room_id, ghost, attachment, reply_to=reply_to
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
        """Send the post; True once its text reached QQ, whatever the pictures did.

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
        for url in twitter.image_urls(post):
            if reply_id and seq > 5:
                break
            downloaded = await twitter.download(self.matrix.session, url)
            if downloaded is None:
                continue
            data, content_type = downloaded
            suffix = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }.get(content_type, ".jpg")
            try:
                prepared = await prepare(data, "m.image", content_type, f"tweet{suffix}")
            except Unfit as exc:
                log.info("cannot prepare tweet picture %s: %s", url, exc)
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
                        log.info("QQ refused the format of tweet picture %s; re-encoding", url)
                        try:
                            prepared = await prepare(
                                data, "m.image", content_type, f"tweet{suffix}", force=True
                            )
                        except Unfit as unfit:
                            log.info("cannot re-encode tweet picture %s: %s", url, unfit)
                            break
                        continue
                    log.warning("cannot send tweet picture to %s: %s", group, exc)
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
        async with self.matrix.session.get(url) as response:
            if response.status >= 400:
                raise RuntimeError(f"attachment download failed: HTTP {response.status}")
            data = await response.read()
        if len(data) > MAX_MEDIA_DOWNLOAD:
            raise RuntimeError("attachment is too large")
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
    def _ref_idx(data: dict) -> str | None:
        for item in (data.get("message_scene") or {}).get("ext") or []:
            if isinstance(item, str) and item.startswith("msg_idx="):
                return item[len("msg_idx=") :]
        return None

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
