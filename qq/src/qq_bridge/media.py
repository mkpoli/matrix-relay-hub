"""One-time resend of room images that QQ has only seen as a placeholder.

The text replay writes `[圖片]` where an image stood, because a digest line cannot
carry media. This command sends the images themselves in time order, skipping
anything QQ already has.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .config import Config
from .matrix import MatrixClient, MatrixError
from .qq import QQClient, QQError
from .ratelimit import RateLimiter
from .relay import MAX_QQ_UPLOAD
from .store import Store

log = logging.getLogger(__name__)

HISTORY_TIMEZONE = ZoneInfo("Asia/Shanghai")
PREAMBLE = "字形大圖譜 · 補發先前的圖片"
QQ_IMAGE = 1


@dataclass(frozen=True)
class Picture:
    event_id: str
    room_id: str
    label: str
    timestamp: int
    sender: str
    content: dict


async def collect(cfg: Config, store: Store, matrix: MatrixClient) -> list[Picture]:
    cutoff = int(time.time() * 1000)
    pictures: list[Picture] = []
    for room in cfg.rooms:
        try:
            room_id = await matrix.resolve_alias(room.alias)
            await matrix.join(room_id, cfg.matrix.bot_mxid)
        except MatrixError as exc:
            log.warning("cannot read %s: %s", room.alias, exc)
            continue
        found = 0
        async for event in matrix.iter_history(room_id, cfg.matrix.bot_mxid):
            if event.get("type") != "m.room.message":
                continue
            content = event.get("content") or {}
            if content.get("msgtype") != "m.image":
                continue
            event_id = event.get("event_id")
            sender = str(event.get("sender") or "")
            timestamp = int(event.get("origin_server_ts") or 0)
            if not event_id or not sender or timestamp > cutoff:
                continue
            if sender == cfg.matrix.bot_mxid or sender.startswith("@qq_"):
                continue
            if any(re.fullmatch(pattern, sender) for pattern in cfg.ignore_senders):
                continue
            if not (content.get("url") or (content.get("file") or {}).get("url")):
                continue
            if store.qq_for_event(event_id):
                continue
            pictures.append(
                Picture(event_id, room_id, room.label, timestamp, sender, content)
            )
            found += 1
        log.info("%s: %d images to resend", room.alias, found)
    pictures.sort(key=lambda picture: (picture.timestamp, picture.event_id))
    return pictures


async def _send(
    cfg: Config, store: Store, matrix: MatrixClient, qq: QQClient, picture: Picture
) -> bool:
    content = picture.content
    url = content.get("url") or (content.get("file") or {}).get("url")
    try:
        data = await matrix.download(str(url))
    except Exception as exc:
        log.warning("cannot download %s: %s", picture.event_id, exc)
        return False
    if len(data) > MAX_QQ_UPLOAD:
        log.warning("image %s is too large for QQ", picture.event_id)
        return False
    filename = str(content.get("body") or "image.png")
    for attempt in range(3):
        try:
            file_info = await qq.upload_group_media(
                cfg.qq.group, data, filename, QQ_IMAGE
            )
            response = await qq.send_group_media(cfg.qq.group, file_info)
        except QQError as exc:
            if exc.rate_limited and attempt < 2:
                await asyncio.sleep(30 * (attempt + 1))
                continue
            log.warning("cannot send image %s: %s", picture.event_id, exc)
            return False
        qq_id = str(response.get("id") or "")
        if qq_id:
            store.add_message(
                qq_id, cfg.qq.group, picture.room_id, picture.event_id, None, "matrix"
            )
        return True
    return False


async def run(
    cfg: Config,
    store: Store,
    matrix: MatrixClient,
    qq: QQClient,
    *,
    dry_run: bool = False,
    delay: float = 3.0,
) -> int:
    pictures = await collect(cfg, store, matrix)
    if not pictures:
        log.info("no images to resend")
        return 0
    log.info("resending %d images", len(pictures))
    if dry_run:
        for picture in pictures:
            when = dt.datetime.fromtimestamp(
                picture.timestamp / 1000, HISTORY_TIMEZONE
            ).strftime("%m-%d %H:%M")
            print(when, picture.label, picture.sender, picture.event_id)
        return 0
    limiter = RateLimiter(20, 1000)
    await qq.send_group_text(cfg.qq.group, PREAMBLE)
    sent = 0
    failed = 0
    for index, picture in enumerate(pictures, 1):
        if not await limiter.acquire():
            log.warning("QQ quota reached before image %d/%d", index, len(pictures))
            return 1
        if await _send(cfg, store, matrix, qq, picture):
            sent += 1
            log.info("image %d/%d sent", index, len(pictures))
        else:
            failed += 1
        await asyncio.sleep(delay)
    log.info("resent %d images, %d failed", sent, failed)
    return 0 if failed == 0 else 1
