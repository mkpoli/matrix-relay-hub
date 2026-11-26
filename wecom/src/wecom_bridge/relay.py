"""Relay logic between Matrix rooms and WeCom (AI Bot WebSocket or Webhook)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

import aiohttp

from .config import Config
from .fit import (
    TOO_LARGE,
    UNREADABLE,
    WECOM_MEDIA_MSGTYPES,
    Prepared,
    Unfit,
    prepare,
    sniff_mime,
    sweep_temp,
)
from .formatting import (
    chunk_utf8,
    format_reaction_key,
    ghost_localpart,
    render_matrix_with_quote,
    sanitize_filename,
    strip_bot_mention,
    strip_reply_fallback,
)
from .matrix import MatrixClient, MatrixError, MediaTooLarge
from .ratelimit import RateLimiter
from .sources import source_tag
from .store import Store
from .wecom import WeComAIBotClient, WeComError, WeComWebhookClient

log = logging.getLogger(__name__)

MAX_MEDIA_DOWNLOAD = 40 * 1024 * 1024
MAX_MATRIX_MEDIA = 64 * 1024 * 1024
MATRIX_TEXT_LIMIT = 2000
MEDIA_WAIT = 20.0
PENDING_MAX_AGE = 24 * 3600
PENDING_ATTEMPTS = 8
PENDING_RETRY = 15 * 60


def is_edit(content: dict) -> bool:
    if "m.new_content" in content:
        return True
    return (content.get("m.relates_to") or {}).get("rel_type") == "m.replace"


def _media_caption(content: dict) -> str:
    filename = str(content.get("filename") or "").strip()
    if not filename:
        return ""
    body = strip_reply_fallback(str(content.get("body") or "")).strip()
    if not body or body == filename:
        return ""
    return body


def _sticker_label(body: object, *, reason: str | None = None) -> str:
    label = " ".join(str(body or "").split())
    if label:
        if len(label) > 200:
            label = label[:200] + "..."
        return f"[sticker: {label}]"
    return f"[sticker {reason}]" if reason else "[sticker]"


def _utf16_chunks(text: str, limit: int = MATRIX_TEXT_LIMIT) -> list[str]:
    parts: list[str] = []
    remaining = text
    while sum(2 if ord(c) > 0xFFFF else 1 for c in remaining) > limit:
        units = 0
        cut = 0
        for index, char in enumerate(remaining):
            units += 2 if ord(char) > 0xFFFF else 1
            if units > limit:
                break
            cut = index + 1
        break_at = remaining.rfind("\n", 0, cut)
        if break_at < cut // 2:
            break_at = remaining.rfind(" ", 0, cut)
        if break_at < cut // 2:
            break_at = cut
        parts.append(remaining[:break_at].rstrip())
        remaining = remaining[break_at:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


class Relay:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        matrix: MatrixClient,
        wecom: WeComAIBotClient | WeComWebhookClient,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.matrix = matrix
        self.wecom = wecom
        self.domain = cfg.matrix.domain
        self.bot_mxid = cfg.matrix.bot_mxid
        self.incoming: asyncio.Queue[tuple[int | None, dict]] = asyncio.Queue()
        self._current: int | None = None
        self._outstanding: dict[int, int] = {}
        self._keep: set[int] = set()
        self._claimed: set[int] = set()
        self._rooms: dict[str, str] = {}
        self._labels: dict[str, str] = {}
        self._incoming_room: str | None = None
        self._tagged = len(cfg.rooms) > 1
        self._ghosts: dict[str, str] = {}
        self._joined: set[tuple[str, str]] = set()
        self._names: dict[tuple[str, str], tuple[str, float]] = {}
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._images: asyncio.Queue[dict] = asyncio.Queue()
        self._videos: asyncio.Queue[dict] = asyncio.Queue()
        # Webhook limit is 20/min; keep headroom at 18/min. AI Bot WS allows higher throughput.
        self._limiter = RateLimiter(18 if cfg.wecom.mode == "webhook" else 50)
        self._tasks: list[asyncio.Task] = []

    @property
    def _text_byte_limit(self) -> int:
        if self.cfg.wecom.mode == "webhook" and self.cfg.wecom.msgtype == "text":
            return 2000
        return 4000

    async def start(self) -> None:
        await asyncio.to_thread(sweep_temp)
        await self._ensure_bot()
        for room in self.cfg.rooms:
            try:
                await self._resolve_room(room)
            except Exception as exc:
                log.warning("cannot set up %s yet: %s", room.alias, exc)
        self._tasks = [
            asyncio.create_task(self._consume_matrix()),
            asyncio.create_task(self._send_loop()),
            asyncio.create_task(self._prepare_loop(self._images)),
            asyncio.create_task(self._prepare_loop(self._videos)),
            asyncio.create_task(self._retry_rooms()),
        ]

    def resume_pending(self) -> None:
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
                "gave up on %d Matrix events that never reached WeCom",
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
                log.exception("cannot read pending events")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _ensure_bot(self) -> None:
        try:
            await self.matrix.register_user(self.cfg.matrix.bot_localpart, "WeCom")
        except Exception as exc:
            log.debug("bot user already present or not creatable: %s", exc)

    async def _resolve_room(self, room) -> None:
        room_id = await self.matrix.resolve_alias(room.alias)
        self._rooms[room.alias] = room_id
        self._labels[room_id] = room.label
        if room.alias == self.cfg.wecom.incoming:
            self._incoming_room = room_id
        try:
            await self.matrix.join(room_id, self.bot_mxid)
        except MatrixError as exc:
            log.warning("cannot join %s as %s: %s", room.alias, self.bot_mxid, exc)
        if await self.matrix.is_encrypted(room_id):
            log.warning("%s is encrypted; WeCom bridge cannot read encrypted events", room.alias)
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

    # -- Matrix -> WeCom --------------------------------------------------

    async def _consume_matrix(self) -> None:
        while True:
            seq, event = await self.incoming.get()
            if seq is not None:
                self._claimed.discard(seq)
                if seq in self._outstanding:
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

    def _put(self, job: dict) -> None:
        if self._current is not None and "pending" not in job:
            job["pending"] = self._current
        seq = job.get("pending")
        if seq is not None:
            self._outstanding[seq] = self._outstanding.get(seq, 0) + 1
        self._queue.put_nowait(job)

    def _finish(self, job: dict) -> None:
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

    def _ignored(self, sender: str) -> bool:
        if sender == self.bot_mxid or sender.startswith("@wecom_"):
            return True
        for pattern in self.cfg.ignore_senders:
            if re.fullmatch(pattern, sender):
                return True
        return False

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
            target = event.get("redacts") or (event.get("content") or {}).get("redacts")
            if isinstance(target, str) and target:
                self.store.supersede(target, "redacted")
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
                self._retry_later()
            return
        if not self.cfg.wecom.can_send:
            log.debug("WeCom outbound target is not configured; dropping %s", event.get("event_id"))
            return
        if event_type == "m.reaction":
            await self._handle_reaction(event, room_id, sender, label)
            return

        content = event.get("content") or {}
        sticker = event_type == "m.sticker"
        if sticker:
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
            return

        sent = self.store.sent_count(event_id) if event_id else 0
        if msgtype in ("m.text", "m.emote", "m.notice"):
            body = strip_reply_fallback(str(content.get("body") or "")).strip()
            if not body:
                return
            prefix = await self._sender_prefix(room_id, sender, label)
            reply_name = await self._reply_target_name(room_id, sender, content)
            speaker = f"{prefix} → {reply_name}" if reply_name else prefix
            line = f"{speaker} {body}" if msgtype == "m.emote" else f"{speaker}: {body}"
            parts = chunk_utf8(line, self._text_byte_limit)
            for index, part in enumerate(parts):
                if index < sent:
                    continue
                self._put(
                    {
                        "kind": "text",
                        "room": room_id,
                        "event_id": event_id,
                        "text": part,
                    }
                )
        elif msgtype in WECOM_MEDIA_MSGTYPES:
            if sent > 0:
                return
            prefix = await self._sender_prefix(room_id, sender, label)
            reply_name = await self._reply_target_name(room_id, sender, content)
            speaker = f"{prefix} → {reply_name}" if reply_name else prefix
            caption = "" if sticker else _media_caption(content)
            self._enqueue_media(
                {
                    "kind": "media",
                    "room": room_id,
                    "event_id": event_id,
                    "speaker": speaker,
                    "caption": caption,
                    "content": content,
                    "sticker": sticker,
                }
            )

    async def _handle_reaction(
        self, event: dict, room_id: str, sender: str, label: str
    ) -> None:
        event_id = event.get("event_id", "")
        if event_id and self.store.sent_count(event_id):
            return
        content = event.get("content") or {}
        relates = content.get("m.relates_to") or {}
        if relates.get("rel_type") != "m.annotation":
            return
        emoji = format_reaction_key(relates.get("key"), content=content, relates=relates)
        if not emoji:
            return
        prefix = await self._sender_prefix(room_id, sender, label)
        text = chunk_utf8(f"{prefix} reacted with {emoji}", self._text_byte_limit)[0]
        self._put({"kind": "text", "room": room_id, "event_id": event_id, "text": text})

    async def _handle_edit(
        self, event: dict, room_id: str, sender: str, label: str, content: dict
    ) -> None:
        target = (content.get("m.relates_to") or {}).get("event_id")
        new = content.get("m.new_content")
        edit_id = event.get("event_id", "")
        if not isinstance(target, str) or not isinstance(new, dict) or not edit_id:
            return
        if self.store.edit_done(edit_id) or self.store.superseded(target) == "redacted":
            return
        msgtype = new.get("msgtype")
        if msgtype not in ("m.text", "m.emote"):
            return
        body = strip_reply_fallback(str(new.get("body") or "")).strip()
        if not body:
            return
        prefix = await self._sender_prefix(room_id, sender, label)
        line = f"{prefix} ✎: {body}"
        self.store.supersede(target, "edited")
        sent = self.store.sent_count(edit_id)
        parts = chunk_utf8(line, self._text_byte_limit)
        for index, part in enumerate(parts):
            if index < sent:
                continue
            self._put(
                {
                    "kind": "edit",
                    "room": room_id,
                    "event_id": edit_id,
                    "target": target,
                    "text": part,
                    "final_part": index == len(parts) - 1,
                }
            )

    async def _sender_prefix(self, room_id: str, sender: str, label: str) -> str:
        name = await self._display_name(room_id, sender)
        source = f"[{source_tag(sender)}] "
        tag = f"[{label}] " if self._tagged else ""
        return f"{source}{tag}{name}"

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

    async def _reply_target_name(
        self, room_id: str, sender: str, content: dict
    ) -> str | None:
        relates = content.get("m.relates_to")
        if not isinstance(relates, dict):
            return None
        in_reply_to = relates.get("m.in_reply_to")
        if not isinstance(in_reply_to, dict):
            return None
        target = in_reply_to.get("event_id")
        if not isinstance(target, str) or not target:
            return None
        try:
            original = await self.matrix.event(room_id, target, self.bot_mxid)
        except (MatrixError, aiohttp.ClientError, asyncio.TimeoutError):
            return None
        if not isinstance(original, dict):
            return None
        target_sender = original.get("sender")
        if not isinstance(target_sender, str) or not target_sender or target_sender == sender:
            return None
        try:
            name = await self._display_name(room_id, target_sender)
        except (MatrixError, aiohttp.ClientError, asyncio.TimeoutError):
            return None
        return " ".join(name.split()) or None

    def _enqueue_media(self, job: dict) -> None:
        loop = asyncio.get_running_loop()
        job = {**job, "ready": loop.create_future()}
        self._put(job)
        lane = self._videos if job["content"].get("msgtype") == "m.video" else self._images
        lane.put_nowait(job)

    async def _prepare_loop(self, queue: asyncio.Queue[dict]) -> None:
        while True:
            job = await queue.get()
            try:
                prepared = await self._prepare_media(job)
            except Exception as exc:
                if not job["ready"].done():
                    job["ready"].set_exception(exc)
            else:
                if not job["ready"].done():
                    job["ready"].set_result(prepared)

    async def _prepare_media(self, job: dict) -> Prepared:
        content = job["content"]
        url = content.get("url")
        if not isinstance(url, str) or not url.startswith("mxc://"):
            raise Unfit(UNREADABLE)
        msgtype = str(content.get("msgtype") or "m.file")
        info = content.get("info") if isinstance(content.get("info"), dict) else {}
        mimetype = str(info.get("mimetype") or "")
        filename = sanitize_filename(
            content.get("filename") or content.get("body"),
            "file.bin",
        )
        try:
            raw = await self.matrix.download(url, max_bytes=MAX_MATRIX_MEDIA)
        except MediaTooLarge as exc:
            raise Unfit(TOO_LARGE) from exc
        return await prepare(raw, msgtype, mimetype, filename, mode=self.cfg.wecom.mode)

    async def _send_loop(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._dispatch_outbound(job)
            except Exception:
                log.exception("unexpected error in WeCom send loop")
            finally:
                self._finish(job)

    async def _send_text(self, text: str) -> None:
        await self._limiter.acquire()
        if isinstance(self.wecom, WeComWebhookClient):
            if self.cfg.wecom.msgtype == "text":
                await self.wecom.send_text(text)
            else:
                await self.wecom.send_markdown(text)
        else:
            await self.wecom.send_markdown(self.cfg.wecom.chat_id, text)

    async def _send_prepared_media(self, prepared: Prepared) -> None:
        await self._limiter.acquire()
        if isinstance(self.wecom, WeComWebhookClient):
            if prepared.media_type == "image":
                await self.wecom.send_image(prepared.data)
            else:
                media_id = await self.wecom.upload_file(prepared.filename, prepared.data)
                await self.wecom.send_file(media_id)
        else:
            media_id = await self.wecom.upload_media(
                prepared.media_type, prepared.filename, prepared.data
            )
            await self.wecom.send_media(
                self.cfg.wecom.chat_id,
                prepared.media_type,
                media_id,
                title=prepared.filename if prepared.media_type == "video" else None,
            )

    async def _dispatch_outbound(self, job: dict) -> None:
        kind = job["kind"]
        event_id = job.get("event_id", "")
        chat_id = self.cfg.wecom.chat_id or "webhook"
        if kind in ("text", "edit"):
            if kind == "text" and event_id and self.store.superseded(event_id):
                return
            try:
                await self._send_text(job["text"])
            except WeComError as exc:
                log.warning("WeCom text send failed: %s", exc)
                if exc.transient:
                    self._retry_later(job)
                return
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("network error sending text to WeCom: %s", exc)
                self._retry_later(job)
                return
            if event_id:
                self.store.record_outbound(event_id, chat_id, job["room"])
            if kind == "edit" and job.get("final_part", True):
                self.store.mark_edit_done(event_id, job["target"])
            return

        if kind == "media":
            if event_id and self.store.superseded(event_id):
                return
            ready: asyncio.Future[Prepared] = job["ready"]
            try:
                prepared = await asyncio.wait_for(asyncio.shield(ready), timeout=MEDIA_WAIT)
            except TimeoutError:
                log.info("media preparation exceeded %.0fs; waiting in background", MEDIA_WAIT)
                try:
                    prepared = await ready
                except Exception as exc:
                    await self._handle_media_failure(job, exc)
                    return
            except Exception as exc:
                await self._handle_media_failure(job, exc)
                return

            speaker = job["speaker"]
            caption = job.get("caption") or ""
            header = f"{speaker}: {caption}" if caption else f"{speaker} sent {prepared.filename}"
            try:
                for part in chunk_utf8(header, self._text_byte_limit):
                    await self._send_text(part)
                await self._send_prepared_media(prepared)
            except WeComError as exc:
                log.warning("WeCom media send failed: %s", exc)
                if exc.transient:
                    self._retry_later(job)
                return
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("network error sending media to WeCom: %s", exc)
                self._retry_later(job)
                return
            if event_id:
                self.store.record_outbound(event_id, chat_id, job["room"])

    async def _handle_media_failure(self, job: dict, exc: Exception) -> None:
        if isinstance(exc, Unfit):
            notice = (
                f"{job['speaker']}: {_sticker_label(job['content'].get('body'), reason=str(exc))}"
                if job.get("sticker")
                else f"{job['speaker']}: [{job['content'].get('msgtype', 'media')} {exc}]"
            )
            try:
                await self._send_text(notice)
            except Exception as send_exc:
                log.warning("cannot send media fallback notice: %s", send_exc)
                self._retry_later(job)
                return
            event_id = job.get("event_id", "")
            if event_id:
                self.store.record_outbound(event_id, self.cfg.wecom.chat_id or "webhook", job["room"])
        elif isinstance(exc, (MatrixError, aiohttp.ClientError, asyncio.TimeoutError)):
            log.warning("transient error downloading Matrix media: %s", exc)
            self._retry_later(job)
        else:
            log.warning("unrecoverable media preparation error: %s", exc)

    # -- WeCom -> Matrix --------------------------------------------------

    async def _ensure_ghost(self, userid: str, room_id: str) -> str:
        display = self.cfg.members.get(userid) or userid
        mxid = self._ghosts.get(userid)
        if not mxid:
            localpart = ghost_localpart(userid)
            mxid = await self.matrix.register_user(localpart, display)
            self._ghosts[userid] = mxid
            with contextlib.suppress(MatrixError):
                await self.matrix.set_displayname(mxid, display)
        if (mxid, room_id) not in self._joined:
            await self.matrix.join(room_id, mxid)
            self._joined.add((mxid, room_id))
        return mxid

    async def on_wecom_frame(self, frame: dict) -> None:
        if frame.get("cmd") != "aibot_msg_callback":
            return
        body = frame.get("body")
        if not isinstance(body, dict):
            return
        msgid = str(body.get("msgid") or "")
        chattype = str(body.get("chattype") or "")
        sender = str((body.get("from") or {}).get("userid") or "")
        chat_id = str(body.get("chatid") or (sender if chattype == "single" else ""))
        if not sender or not chat_id:
            return
        if not self.cfg.wecom.chat_id:
            log.info(
                "discovered WeCom %s chat_id=%s from userid=%s (set wecom.chat_id to bridge)",
                chattype or "unknown",
                chat_id,
                sender,
            )
            return
        if chat_id != self.cfg.wecom.chat_id:
            log.debug("ignoring WeCom callback for unconfigured chat_id=%s", chat_id)
            return
        room_id = self._incoming_room
        if not room_id:
            log.warning("incoming Matrix room %s not resolved yet", self.cfg.wecom.incoming)
            return
        if msgid and not self.store.claim_inbound(msgid, chat_id, room_id):
            log.debug("ignoring duplicate WeCom msgid %s", msgid)
            return
        try:
            event_id = await self._relay_wecom_body(body, room_id, sender, chattype == "group")
        except Exception:
            if msgid:
                self.store.release_inbound(msgid)
            raise
        if msgid and event_id:
            self.store.set_inbound_event(msgid, event_id)

    async def _relay_wecom_body(
        self, body: dict, room_id: str, userid: str, is_group: bool
    ) -> str:
        mxid = await self._ensure_ghost(userid, room_id)
        msgtype = str(body.get("msgtype") or "")
        quote = body.get("quote") if isinstance(body.get("quote"), dict) else None
        last_event_id = ""

        if msgtype == "text":
            raw_text = str((body.get("text") or {}).get("content") or "")
            cleaned = strip_bot_mention(raw_text, self.cfg.wecom.bot_name, is_group=is_group)
            if not cleaned and not quote:
                return ""
            last_event_id = await self._send_matrix_text(room_id, mxid, cleaned, quote)
        elif msgtype == "voice":
            raw_text = str((body.get("voice") or {}).get("content") or "").strip()
            text = f"[voice] {raw_text}" if raw_text else "[voice]"
            last_event_id = await self._send_matrix_text(room_id, mxid, text, quote)
        elif msgtype in ("image", "file", "video"):
            if quote:
                last_event_id = await self._send_matrix_text(room_id, mxid, "", quote)
            payload = body.get(msgtype) if isinstance(body.get(msgtype), dict) else {}
            last_event_id = await self._relay_wecom_media(
                room_id, mxid, msgtype, payload
            ) or last_event_id
        elif msgtype == "mixed":
            items = (body.get("mixed") or {}).get("msg_item") or []
            pending_quote = quote
            for item in items:
                if not isinstance(item, dict):
                    continue
                kind = item.get("msgtype")
                if kind == "text":
                    raw_text = str((item.get("text") or {}).get("content") or "")
                    cleaned = strip_bot_mention(
                        raw_text, self.cfg.wecom.bot_name, is_group=is_group
                    )
                    if cleaned or pending_quote:
                        last_event_id = await self._send_matrix_text(
                            room_id, mxid, cleaned, pending_quote
                        )
                        pending_quote = None
                elif kind == "image":
                    if pending_quote:
                        last_event_id = await self._send_matrix_text(
                            room_id, mxid, "", pending_quote
                        )
                        pending_quote = None
                    img = item.get("image") if isinstance(item.get("image"), dict) else {}
                    last_event_id = (
                        await self._relay_wecom_media(room_id, mxid, "image", img)
                        or last_event_id
                    )
        else:
            log.info("unsupported WeCom inbound msgtype %r", msgtype)
        return last_event_id

    async def _send_matrix_text(
        self, room_id: str, mxid: str, text: str, quote: dict | None
    ) -> str:
        plain, formatted = render_matrix_with_quote(text, quote)
        if not plain:
            return ""
        parts = _utf16_chunks(plain, MATRIX_TEXT_LIMIT)
        last_event_id = ""
        for index, part in enumerate(parts):
            content: dict = {"msgtype": "m.text", "body": part}
            if index == 0 and formatted and len(parts) == 1:
                content["format"] = "org.matrix.custom.html"
                content["formatted_body"] = formatted
            last_event_id = await self.matrix.send_message(room_id, mxid, content)
        return last_event_id

    async def _relay_wecom_media(
        self, room_id: str, mxid: str, kind: str, media_payload: dict
    ) -> str:
        url = media_payload.get("url")
        if not isinstance(url, str) or not url or not isinstance(self.wecom, WeComAIBotClient):
            return ""
        aes_key = media_payload.get("aeskey")
        try:
            data, header_name, content_type = await self.wecom.download_media(
                url,
                aes_key if isinstance(aes_key, str) else None,
                max_bytes=MAX_MEDIA_DOWNLOAD,
            )
        except (WeComError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("failed to fetch WeCom %s media: %s", kind, exc)
            return await self.matrix.send_message(
                room_id,
                mxid,
                {"msgtype": "m.text", "body": f"[{kind} could not be fetched from WeCom]"},
            )

        detected_mime, ext = sniff_mime(data, content_type or "application/octet-stream")
        if kind == "image" and not detected_mime.startswith("image/"):
            detected_mime = "image/jpeg"
            ext = ext or ".jpg"
        elif kind == "video" and not detected_mime.startswith("video/"):
            detected_mime = "video/mp4"
            ext = ext or ".mp4"
        default_name = f"wecom_{kind}{ext or '.bin'}"
        filename = sanitize_filename(header_name, default_name)
        mxc = await self.matrix.upload(data, detected_mime, filename, mxid)
        matrix_msgtype = {"image": "m.image", "video": "m.video"}.get(kind, "m.file")
        return await self.matrix.send_message(
            room_id,
            mxid,
            {
                "msgtype": matrix_msgtype,
                "body": filename,
                "filename": filename,
                "url": mxc,
                "info": {"mimetype": detected_mime, "size": len(data)},
            },
        )

    async def inject_wecom_message(
        self, chat_id: str, userid: str, text: str
    ) -> str:
        """Inject a synthetic inbound WeCom message (used by `simulate` CLI)."""
        frame = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "simulate"},
            "body": {
                "msgid": f"sim-{time.time_ns()}",
                "chattype": "group",
                "chatid": chat_id,
                "from": {"userid": userid},
                "msgtype": "text",
                "text": {"content": text},
            },
        }
        await self.on_wecom_frame(frame)
        return frame["body"]["msgid"]
