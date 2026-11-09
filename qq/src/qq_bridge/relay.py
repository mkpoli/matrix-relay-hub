"""Relay logic between QQ groups and Matrix rooms."""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import logging
import re
import time
import uuid
from pathlib import Path

from .config import Config
from .formatting import (
    chunk,
    extract_face_ids,
    render_qq_text,
    strip_leading_mentions,
    strip_reply_fallback,
)
from .matrix import MatrixClient, MatrixError
from .qq import QQClient, QQError
from .ratelimit import RateLimiter
from .store import Store

log = logging.getLogger(__name__)

MAX_MEDIA_DOWNLOAD = 40 * 1024 * 1024
# QQ takes at most 5 MB of binary per direct upload; base64 inflates by a third.
MAX_QQ_UPLOAD = 3_500_000
# QQ refuses to recall a group message more than two minutes after it was sent.
RECALL_WINDOW = 120

QQ_FILE_TYPES = {"m.image": 1, "m.video": 2, "m.audio": 3, "m.file": 4}

FACE_MIRRORS = (
    "https://face.viki.moe/png/{face_id}.png",
    "https://cdn.jsdelivr.net/gh/vikiboss/face@main/face/png/{face_id}.png",
)


def edit_target(content: dict) -> str | None:
    """The event a Matrix edit replaces, or None for ordinary content."""
    relates = content.get("m.relates_to") or {}
    if relates.get("rel_type") != "m.replace":
        return None
    return relates.get("event_id") or None


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
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self._rooms: dict[str, str] = {}
        self._ghosts: dict[str, str] = {}
        self._avatar: str | None = None
        self._avatars: set[str] = set()
        self._joined: set[tuple[str, str]] = set()
        self._names: dict[tuple[str, str], tuple[str, float]] = {}
        self._unbound: set[str] = set()
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._global = RateLimiter(60)
        self._group_limiters: dict[str, RateLimiter] = {}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self._ensure_bot()
        await self._ensure_avatar()
        for bridge in self.cfg.bridges:
            try:
                await self._resolve_bridge(bridge)
            except Exception as exc:
                log.warning("cannot set up %s yet: %s", bridge.room, exc)
        self._tasks = [
            asyncio.create_task(self._consume_matrix()),
            asyncio.create_task(self._send_loop()),
            asyncio.create_task(self._retry_rooms()),
        ]

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

    async def _resolve_bridge(self, bridge) -> None:
        room_id = await self.matrix.resolve_alias(bridge.room)
        self._rooms[bridge.room] = room_id
        if bridge.group:
            self.store.bind(room_id, bridge.group, bridge.code)
        else:
            self.store.set_code(room_id, bridge.code)
        try:
            await self.matrix.join(room_id, self.bot_mxid)
        except MatrixError as exc:
            log.warning("cannot join %s as %s: %s", bridge.room, self.bot_mxid, exc)
        if await self.matrix.is_encrypted(room_id):
            log.warning(
                "%s is encrypted; QQ messages can be written but not read",
                bridge.room,
            )
        log.info("bridging %s (%s) with code %s", bridge.room, room_id, bridge.code)

    async def _retry_rooms(self) -> None:
        while True:
            await asyncio.sleep(60)
            for bridge in self.cfg.bridges:
                if bridge.room in self._rooms:
                    continue
                try:
                    await self._resolve_bridge(bridge)
                except Exception:
                    pass

    # -- Matrix -> QQ -----------------------------------------------------

    async def _consume_matrix(self) -> None:
        while True:
            event = await self.incoming.get()
            try:
                await self.handle_matrix_event(event)
            except Exception:
                log.exception("failed to relay a Matrix event")

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
        if event_type == "m.reaction":
            await self._reaction(event)
            return
        if event_type != "m.room.message":
            return
        if self._ignored(sender):
            return
        group = self.store.group_for_room(room_id)
        if not group:
            return
        content = event.get("content") or {}
        replace_id = edit_target(content)
        if replace_id:
            content = content.get("m.new_content") or {}
            if not content:
                log.debug("ignoring an edit without m.new_content in %s", room_id)
                return
            event_id = replace_id
        elif "m.new_content" in content:
            log.debug("ignoring an edit without a target in %s", room_id)
            return
        else:
            event_id = event.get("event_id", "")
        msgtype = content.get("msgtype")
        if msgtype == "m.notice" and self.cfg.ignore_notices:
            return

        if msgtype in ("m.text", "m.emote"):
            body = strip_reply_fallback(str(content.get("body", "")))
            if not body.strip():
                return
            name = await self._display_name(room_id, sender)
            text = f"* {name} {body}" if msgtype == "m.emote" else f"{name}: {body}"
            reference = self._reply_reference(event) if not replace_id else None
            for index, part in enumerate(chunk(text)):
                job = {
                    "kind": "text",
                    "group": group,
                    "room": room_id,
                    "event_id": event_id,
                    "text": part,
                    "reference": reference if index == 0 else None,
                }
                if index == 0 and replace_id:
                    job["edit_of"] = replace_id
                self._queue.put_nowait(job)
        elif msgtype in QQ_FILE_TYPES:
            job = {
                "kind": "media",
                "group": group,
                "room": room_id,
                "event_id": event_id,
                "sender": sender,
                "content": content,
            }
            if replace_id:
                job["edit_of"] = replace_id
            self._queue.put_nowait(job)

    def _ignored(self, sender: str) -> bool:
        if sender == self.bot_mxid or sender.startswith("@qq_"):
            return True
        for pattern in self.cfg.ignore_senders:
            if re.fullmatch(pattern, sender):
                return True
        return False

    def _reply_reference(self, event: dict) -> str | None:
        relates = (event.get("content") or {}).get("m.relates_to") or {}
        target = (relates.get("m.in_reply_to") or {}).get("event_id")
        if not target:
            return None
        return self.store.reference_for_event(target)

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
        for row in self.store.messages_for_event(target):
            if not row["qq_id"]:
                continue
            try:
                await self.qq.delete_group_message(row["group_openid"], row["qq_id"])
            except QQError as exc:
                log.info("cannot recall QQ message %s: %s", row["qq_id"], exc)
            else:
                self.store.remove_message(row["qq_id"])

    async def _reaction(self, event: dict) -> None:
        """QQ's bot API has no group reactions, so post the emoji as a reply."""
        room_id = event.get("room_id")
        sender = event.get("sender")
        if self._ignored(sender):
            return
        group = self.store.group_for_room(room_id)
        if not group:
            return
        relates = (event.get("content") or {}).get("m.relates_to") or {}
        target = relates.get("event_id")
        key = relates.get("key")
        event_id = event.get("event_id", "")
        if relates.get("rel_type") != "m.annotation" or not target or not key:
            return
        if not event_id:
            return
        name = await self._display_name(room_id, sender)
        self._queue.put_nowait(
            {
                "kind": "text",
                "group": group,
                "room": room_id,
                "event_id": event_id,
                "text": f"{name}: {key}",
                "reference": None,
                "react_to": target,
            }
        )

    # -- outbound queue ---------------------------------------------------

    async def _send_loop(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job["kind"] == "text":
                    await self._send_text(job)
                else:
                    await self._send_media(job)
            except Exception:
                log.exception("failed to send to QQ")

    async def _wait_limits(self, group: str) -> bool:
        limiter = self._group_limiters.setdefault(group, RateLimiter(20, 1000))
        if not await limiter.acquire():
            log.warning("daily QQ message quota for %s is used up", group)
            return False
        if not await self._global.acquire():
            return False
        return True

    async def _replace_previous(self, group: str, event_id: str) -> str | None:
        """Recall the QQ copies of a Matrix message and return a quote target.

        Messages past QQ's recall window stay up and get quoted by the
        replacement instead.
        """
        reference: str | None = None
        now = time.time()
        for row in reversed(self.store.messages_for_event(event_id)):
            qq_id = row["qq_id"]
            if not qq_id:
                continue
            if now - row["ts"] <= RECALL_WINDOW:
                try:
                    await self.qq.delete_group_message(group, qq_id)
                except QQError as exc:
                    log.info("cannot recall QQ message %s: %s", qq_id, exc)
                else:
                    self.store.remove_message(qq_id)
                    continue
            if reference is None:
                reference = row["ref_idx"] or qq_id
        return reference

    async def _send_text(self, job: dict) -> None:
        group = job["group"]
        reference = job.get("reference")
        if job.get("react_to"):
            reference = self.store.reference_for_event(job["react_to"])
            if not reference:
                log.debug("dropping a reaction to unbridged %s", job["react_to"])
                return
        if not await self._wait_limits(group):
            return
        if job.get("edit_of"):
            reference = await self._replace_previous(group, job["edit_of"])
        for attempt in range(4):
            try:
                response = await self.qq.send_group_text(
                    group,
                    job["text"],
                    reference=reference,
                    msg_id=job.get("msg_id"),
                )
            except QQError as exc:
                if reference and not exc.rate_limited:
                    log.info("cannot quote %s, sending without it: %s", reference, exc)
                    reference = None
                    continue
                if exc.rate_limited and attempt < 2:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                log.warning("cannot send to %s: %s", group, exc)
                return
            self._remember_sent(group, job["room"], job["event_id"], response)
            log.info("sent to QQ group %s: %s", group, response.get("id"))
            return

    async def _send_media(self, job: dict) -> None:
        content = job["content"]
        msgtype = content.get("msgtype")
        url = content.get("url") or (content.get("file") or {}).get("url")
        if not url:
            return
        try:
            data = await self.matrix.download(url)
        except Exception as exc:
            log.warning("cannot download %s: %s", url, exc)
            return
        if len(data) > MAX_QQ_UPLOAD:
            name = await self._display_name(job["room"], job["sender"])
            notice = {
                "group": job["group"],
                "room": job["room"],
                "event_id": job["event_id"],
                "text": f"{name}: [{msgtype[2:]} too large for QQ]",
                "reference": None,
            }
            if job.get("edit_of"):
                notice["edit_of"] = job["edit_of"]
            await self._send_text(notice)
            return
        file_type = QQ_FILE_TYPES.get(msgtype, 4)
        filename = str(content.get("body") or f"file{file_type}")
        if not await self._wait_limits(job["group"]):
            return
        try:
            file_info = await self.qq.upload_group_media(
                job["group"], data, filename, file_type
            )
        except QQError as exc:
            log.warning(
                "cannot upload media for %s: %s (trace %s)",
                job["group"],
                exc,
                exc.trace_id,
            )
            return
        reference = None
        if job.get("edit_of"):
            reference = await self._replace_previous(job["group"], job["edit_of"])
        try:
            response = await self.qq.send_group_media(
                job["group"], file_info, reference=reference
            )
        except QQError as exc:
            if reference and not exc.rate_limited:
                log.info("cannot quote %s, sending media without it: %s", reference, exc)
                try:
                    response = await self.qq.send_group_media(job["group"], file_info)
                except QQError as inner:
                    log.warning(
                        "cannot send media to %s: %s (trace %s)",
                        job["group"],
                        inner,
                        inner.trace_id,
                    )
                    return
            else:
                log.warning(
                    "cannot send media to %s: %s (trace %s)",
                    job["group"],
                    exc,
                    exc.trace_id,
                )
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
        room_id = self.store.room_for_group(group)
        if not room_id:
            if group not in self._unbound:
                self._unbound.add(group)
                log.info(
                    "QQ group %s is not bound to a room; send !qq bind <code> there",
                    group,
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

        face_images = await self._resolve_faces(content)
        body, html_body = render_qq_text(
            content, data.get("mentions"), self.domain, face_images
        )
        if body:
            payload: dict = {"msgtype": "m.text", "body": body}
            if html_body:
                payload["format"] = "org.matrix.custom.html"
                payload["formatted_body"] = html_body
            if reply_to:
                payload["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
            try:
                event_id = await self.matrix.send_message(room_id, ghost, payload)
                sent.append(event_id)
            except MatrixError as exc:
                log.warning("cannot send to %s as %s: %s", room_id, ghost, exc)

        for attachment in data.get("attachments") or []:
            try:
                event_id = await self._send_attachment(room_id, ghost, attachment)
                if event_id and not sent:
                    sent.append(event_id)
            except Exception as exc:
                log.warning("cannot relay attachment: %s", exc)

        if qq_id:
            self.store.add_message(
                qq_id, group, room_id, sent[0] if sent else None, ref_idx, "qq"
            )

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
        self, room_id: str, ghost: str, attachment: dict
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
        elif command == "bind" and len(parts) > 2:
            code = parts[2]
            if self.store.room_for_group(group):
                self._reply(group, "this group is already bound", msg_id)
                return
            room_id = self.store.room_for_code(code)
            if not room_id:
                self._reply(group, f"unknown code: {code}", msg_id)
                return
            self.store.bind(room_id, group, code)
            log.info("bound QQ group %s to %s", group, room_id)
            self._reply(group, "bound", msg_id)
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
