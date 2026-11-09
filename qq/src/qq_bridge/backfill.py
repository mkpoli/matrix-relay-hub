"""One-time replay of room history into the QQ group.

The QQ bot sends at most a few dozen messages a day, so history goes out as
batches: several room messages per QQ message, each line stamped with the time
and room it came from. Events already carried to QQ, whether live or in an
earlier run, are skipped, so the command resumes where it stopped.
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
from .formatting import chunk, strip_reply_fallback
from .matrix import MatrixClient, MatrixError
from .qq import QQClient, QQError
from .ratelimit import RateLimiter
from .relay import is_edit
from .store import Store

log = logging.getLogger(__name__)

HISTORY_TIMEZONE = ZoneInfo("Asia/Shanghai")
BATCH_LIMIT = 1700
MEDIA_LABELS = {"m.image": "圖片", "m.video": "影片", "m.audio": "音頻", "m.file": "檔案"}
PREAMBLE = "字形大圖譜 · 社群聊天記錄"


@dataclass(frozen=True)
class Item:
    event_id: str
    room_id: str
    label: str
    timestamp: int
    line: str


@dataclass(frozen=True)
class Batch:
    text: str
    items: tuple[Item, ...]


class Names:
    """Member display names, read once per room and sender."""

    def __init__(self, matrix: MatrixClient) -> None:
        self.matrix = matrix
        self._cache: dict[tuple[str, str], str] = {}

    async def get(self, room_id: str, user_id: str) -> str:
        key = (room_id, user_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            member = await self.matrix.member(room_id, user_id)
        except MatrixError:
            member = None
        name = (member or {}).get("displayname")
        if not name:
            name = user_id[1:].split(":", 1)[0]
        self._cache[key] = name
        return name


def _stamp(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp / 1000, HISTORY_TIMEZONE).strftime(
        "%m-%d %H:%M"
    )


def _indent_continuation(text: str) -> str:
    first, *rest = text.split("\n")
    lines = [first.rstrip()]
    lines.extend(f"  {part}".rstrip() for part in rest)
    return "\n".join(lines)


async def _item_for(
    event: dict,
    room_id: str,
    label: str,
    cfg: Config,
    store: Store,
    names: Names,
    cutoff: int,
) -> Item | None:
    """Turn one room event into a digest line, or None when it is not replayed."""
    if event.get("type") != "m.room.message":
        return None
    event_id = event.get("event_id")
    timestamp = int(event.get("origin_server_ts") or 0)
    sender = str(event.get("sender") or "")
    if not event_id or not sender or timestamp > cutoff:
        return None
    if sender == cfg.matrix.bot_mxid or sender.startswith("@qq_"):
        return None
    if any(re.fullmatch(pattern, sender) for pattern in cfg.ignore_senders):
        return None
    content = event.get("content") or {}
    if is_edit(content):
        return None
    msgtype = content.get("msgtype")
    if cfg.ignore_notices and msgtype == "m.notice":
        return None
    if msgtype in MEDIA_LABELS:
        body = f"[{MEDIA_LABELS[msgtype]}]"
    elif msgtype in ("m.text", "m.emote"):
        body = strip_reply_fallback(str(content.get("body") or "")).strip()
        if not body:
            return None
    else:
        return None
    if store.relayed_qq(event_id) or store.qq_for_event(event_id):
        return None
    name = await names.get(room_id, sender)
    prefix = f"[{_stamp(timestamp)}] [{label}] "
    if msgtype == "m.emote":
        line = f"{prefix}* {name} {body}"
    else:
        line = f"{prefix}{name}: {body}"
    return Item(
        event_id=event_id,
        room_id=room_id,
        label=label,
        timestamp=timestamp,
        line=_indent_continuation(line),
    )


async def collect(cfg: Config, store: Store, matrix: MatrixClient) -> list[Item]:
    """Read every bridged room and return the events QQ has not seen yet."""
    cutoff = int(time.time() * 1000)
    names = Names(matrix)
    items: list[Item] = []
    for room in cfg.rooms:
        try:
            room_id = await matrix.resolve_alias(room.alias)
            await matrix.join(room_id, cfg.matrix.bot_mxid)
        except MatrixError as exc:
            log.warning("cannot read %s: %s", room.alias, exc)
            continue
        found = 0
        async for event in matrix.iter_history(room_id, cfg.matrix.bot_mxid):
            item = await _item_for(event, room_id, room.label, cfg, store, names, cutoff)
            if item is not None:
                items.append(item)
                found += 1
        log.info("%s: %d messages to replay", room.alias, found)
    items.sort(key=lambda item: (item.timestamp, item.event_id))
    return items


def make_batches(items: list[Item], limit: int = BATCH_LIMIT) -> list[Batch]:
    """Pack digest lines into QQ messages of at most `limit` characters."""
    body_limit = limit - len(PREAMBLE) - 2
    batches: list[Batch] = []
    lines: list[str] = []
    used: list[Item] = []

    def flush() -> None:
        if not lines:
            return
        text = "\n".join(lines)
        if not batches:
            text = f"{PREAMBLE}\n{text}"
        batches.append(Batch(text=text, items=tuple(used)))
        lines.clear()
        used.clear()

    for item in items:
        for piece in chunk(item.line, body_limit):
            base = len(PREAMBLE) + 1 if not batches else 0
            size = base + len("\n".join(lines)) + (1 if lines else 0) + len(piece)
            if lines and size > limit:
                flush()
            lines.append(piece)
            used.append(item)
    flush()
    return batches


async def run(
    cfg: Config,
    store: Store,
    matrix: MatrixClient,
    qq: QQClient,
    *,
    dry_run: bool = False,
    delay: float = 2.0,
) -> int:
    items = await collect(cfg, store, matrix)
    if not items:
        log.info("nothing to replay")
        return 0
    batches = make_batches(items)
    log.info("replaying %d messages in %d batches", len(items), len(batches))
    if dry_run:
        for index, batch in enumerate(batches, 1):
            print(f"--- batch {index}/{len(batches)} ({len(batch.text)} chars) ---")
            print(batch.text)
        return 0
    limiter = RateLimiter(20, 1000)
    sent = 0
    for index, batch in enumerate(batches, 1):
        if not await limiter.acquire():
            log.warning("QQ quota reached before batch %d/%d", index, len(batches))
            return 1
        try:
            response = await qq.send_group_text(cfg.qq.group, batch.text)
        except QQError as exc:
            log.warning(
                "batch %d/%d failed: %s; rerun to continue", index, len(batches), exc
            )
            return 1
        qq_id = str(response.get("id") or "")
        for item in batch.items:
            store.mark_relayed(item.event_id, qq_id, cfg.qq.group, item.room_id)
        sent += 1
        log.info("batch %d/%d sent as %s", index, len(batches), qq_id or "?")
        await asyncio.sleep(delay)
    log.info("replayed %d messages in %d batches", len(items), sent)
    return 0
