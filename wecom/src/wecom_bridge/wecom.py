"""WeCom AI Bot WebSocket gateway and Group Robot Webhook clients."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import math
import uuid
from collections.abc import Awaitable, Callable

import aiohttp

from .config import WeComConfig
from .crypto import DecryptError, decrypt_file
from .formatting import parse_content_disposition

log = logging.getLogger(__name__)

# Official WeCom AI Bot WebSocket commands (WecomTeam/aibot-node-sdk).
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_PING = "ping"
CMD_MSG_CALLBACK = "aibot_msg_callback"
CMD_EVENT_CALLBACK = "aibot_event_callback"
CMD_SEND_MSG = "aibot_send_msg"
CMD_UPLOAD_INIT = "aibot_upload_media_init"
CMD_UPLOAD_CHUNK = "aibot_upload_media_chunk"
CMD_UPLOAD_FINISH = "aibot_upload_media_finish"

CHUNK_SIZE = 512 * 1024
MAX_CHUNKS = 100
HEARTBEAT_SECONDS = 30.0
MAX_MISSED_PONGS = 2
ACK_TIMEOUT_SECONDS = 15.0
CONNECT_WAIT_SECONDS = 10.0
MAX_AUTH_FAILURES = 5

RATE_LIMIT_ERRCODES = frozenset({45009, 45033, 846616})
TRANSIENT_ERRCODES = frozenset({-1, 45009, 45033, 846616})


class WeComError(Exception):
    def __init__(
        self,
        code: int | str,
        message: str,
        *,
        status: int | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(f"WeCom error {code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self._transient = transient

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 or self.code in RATE_LIMIT_ERRCODES

    @property
    def transient(self) -> bool:
        return (
            self._transient
            or self.rate_limited
            or self.code in TRANSIENT_ERRCODES
            or (self.status or 0) >= 500
        )


class WeComAuthError(WeComError):
    """Raised when WeCom rejects bot_id/secret authentication."""


def _req_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class WeComAIBotClient:
    """Bidirectional client for WeCom's AI Bot WebSocket gateway."""

    def __init__(
        self,
        cfg: WeComConfig,
        session: aiohttp.ClientSession,
        *,
        heartbeat_interval: float = HEARTBEAT_SECONDS,
        ack_timeout: float = ACK_TIMEOUT_SECONDS,
        reconnect_base_delay: float = 1.0,
        max_auth_failures: int = MAX_AUTH_FAILURES,
    ) -> None:
        self.cfg = cfg
        self.session = session
        self.heartbeat_interval = heartbeat_interval
        self.ack_timeout = ack_timeout
        self.reconnect_base_delay = reconnect_base_delay
        self.max_auth_failures = max_auth_failures
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ready = asyncio.Event()
        self._stopped = False
        self._kicked = False
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._send_lock = asyncio.Lock()
        self._events: asyncio.Queue[dict] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._ready.is_set() and self._ws is not None and not self._ws.closed

    async def wait_ready(self, timeout: float = CONNECT_WAIT_SECONDS) -> None:
        if self.connected:
            return
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError as exc:
            raise WeComError("not_connected", "WeCom WebSocket is not connected", transient=True) from exc

    async def run(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        """Connect, authenticate, keep heartbeats alive, and dispatch inbound callbacks."""
        self._stopped = False
        self._kicked = False
        self._worker = asyncio.create_task(self._dispatch_loop(handler))
        delay = self.reconnect_base_delay
        auth_failures = 0
        try:
            while not self._stopped and not self._kicked:
                authenticated = False
                try:
                    authenticated = await self._connect_once()
                except asyncio.CancelledError:
                    raise
                except WeComAuthError as exc:
                    auth_failures += 1
                    log.error(
                        "WeCom AI Bot authentication failed (%d/%d): %s",
                        auth_failures,
                        self.max_auth_failures,
                        exc,
                    )
                    if auth_failures >= self.max_auth_failures:
                        raise
                except Exception as exc:
                    log.warning("WeCom WebSocket error: %s", exc)
                if authenticated:
                    delay = self.reconnect_base_delay
                    auth_failures = 0
                if self._stopped or self._kicked:
                    break
                log.info("reconnecting to WeCom WebSocket in %.1fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 30.0)
        finally:
            await self.close()

    async def _dispatch_loop(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        while True:
            frame = await self._events.get()
            try:
                await handler(frame)
            except Exception:
                log.exception("failed to handle WeCom callback")

    async def _connect_once(self) -> bool:
        log.info("connecting to WeCom AI Bot gateway %s", self.cfg.ws_url)
        authenticated = False
        async with self.session.ws_connect(self.cfg.ws_url, heartbeat=None) as ws:
            self._ws = ws

            async def _lifecycle() -> None:
                nonlocal authenticated
                try:
                    req_id = _req_id(CMD_SUBSCRIBE)
                    try:
                        await self._rpc(
                            CMD_SUBSCRIBE,
                            {"bot_id": self.cfg.bot_id, "secret": self.cfg.secret},
                            req_id=req_id,
                            allow_unready=True,
                        )
                    except WeComError as exc:
                        if not exc.transient:
                            raise WeComAuthError(exc.code, exc.message) from exc
                        raise
                    authenticated = True
                    self._ready.set()
                    log.info("WeCom AI Bot authenticated")
                    missed_pongs = 0
                    while not ws.closed:
                        await asyncio.sleep(self.heartbeat_interval)
                        if missed_pongs >= MAX_MISSED_PONGS:
                            log.warning(
                                "WeCom heartbeat timed out after %d missed pongs",
                                missed_pongs,
                            )
                            await ws.close()
                            return
                        missed_pongs += 1
                        try:
                            ping_ack = await self._rpc(
                                CMD_PING, None, req_id=_req_id(CMD_PING)
                            )
                            if ping_ack.get("errcode", 0) == 0:
                                missed_pongs = 0
                        except Exception as exc:
                            log.warning("WeCom ping failed: %s", exc)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if not ws.closed:
                        await ws.close()
                    raise

            lifecycle_task = asyncio.create_task(_lifecycle())
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            frame = json.loads(msg.data)
                        except json.JSONDecodeError:
                            log.warning("ignoring invalid JSON frame from WeCom")
                            continue
                        if not isinstance(frame, dict):
                            continue
                        self._handle_frame(frame)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
                if not lifecycle_task.done():
                    with contextlib.suppress(TimeoutError, Exception):
                        await asyncio.wait_for(asyncio.shield(lifecycle_task), timeout=1.0)
                if lifecycle_task.done() and not lifecycle_task.cancelled():
                    exc = lifecycle_task.exception()
                    if exc is not None:
                        raise exc
            finally:
                self._ready.clear()
                lifecycle_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await lifecycle_task
                self._fail_pending("WeCom WebSocket disconnected")
                self._ws = None
        return authenticated

    def _handle_frame(self, frame: dict) -> None:
        cmd = frame.get("cmd")
        headers = frame.get("headers") if isinstance(frame.get("headers"), dict) else {}
        req_id = str(headers.get("req_id") or "")

        if cmd == CMD_MSG_CALLBACK:
            self._events.put_nowait(frame)
            return
        if cmd == CMD_EVENT_CALLBACK:
            body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
            event = body.get("event") if isinstance(body.get("event"), dict) else {}
            if event.get("eventtype") == "disconnected_event":
                log.error(
                    "WeCom disconnected this bot because another instance connected with the same bot_id; stopping reconnects"
                )
                self._kicked = True
                if self._ws and not self._ws.closed:
                    self._close_task = asyncio.create_task(self._ws.close())
                return
            self._events.put_nowait(frame)
            return

        if req_id and req_id in self._pending:
            future = self._pending.pop(req_id)
            if not future.done():
                future.set_result(frame)
            return
        log.debug("ignoring unmatched WeCom frame: %s", frame)

    def _fail_pending(self, reason: str) -> None:
        err = WeComError("disconnected", reason, transient=True)
        for req_id, future in list(self._pending.items()):
            self._pending.pop(req_id, None)
            if not future.done():
                future.set_exception(err)

    async def _rpc(
        self,
        cmd: str,
        body: dict | None,
        *,
        req_id: str | None = None,
        allow_unready: bool = False,
    ) -> dict:
        if not allow_unready:
            await self.wait_ready()
        ws = self._ws
        if ws is None or ws.closed:
            raise WeComError("not_connected", "WeCom WebSocket is closed", transient=True)
        rid = req_id or _req_id(cmd)
        payload: dict = {"cmd": cmd, "headers": {"req_id": rid}}
        if body is not None:
            payload["body"] = body
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict] = loop.create_future()
        self._pending[rid] = future
        try:
            async with self._send_lock:
                await ws.send_json(payload)
            ack = await asyncio.wait_for(future, timeout=self.ack_timeout)
        except TimeoutError as exc:
            self._pending.pop(rid, None)
            raise WeComError("timeout", f"timed out waiting for {cmd} ack", transient=True) from exc
        except WeComError:
            self._pending.pop(rid, None)
            raise
        except Exception as exc:
            self._pending.pop(rid, None)
            raise WeComError("ws_send", str(exc), transient=True) from exc
        errcode = ack.get("errcode", 0)
        if errcode != 0:
            raise WeComError(errcode, str(ack.get("errmsg") or "WeCom rejected command"))
        return ack

    async def send_markdown(self, chat_id: str, content: str) -> dict:
        return await self._rpc(
            CMD_SEND_MSG,
            {
                "chatid": chat_id,
                "msgtype": "markdown",
                "markdown": {"content": content},
            },
        )

    async def upload_media(self, media_type: str, filename: str, data: bytes) -> str:
        """Upload temporary media via WeCom's 3-step WebSocket chunk protocol."""
        total_size = len(data)
        total_chunks = max(1, math.ceil(total_size / CHUNK_SIZE))
        if total_chunks > MAX_CHUNKS:
            raise WeComError("too_large", f"file needs {total_chunks} chunks (max {MAX_CHUNKS})")
        md5 = hashlib.md5(data).hexdigest()
        init_ack = await self._rpc(
            CMD_UPLOAD_INIT,
            {
                "type": media_type,
                "filename": filename,
                "total_size": total_size,
                "total_chunks": total_chunks,
                "md5": md5,
            },
        )
        upload_id = (init_ack.get("body") or {}).get("upload_id")
        if not upload_id:
            raise WeComError("upload_init", "missing upload_id in init ack")

        for chunk_index in range(total_chunks):
            start = chunk_index * CHUNK_SIZE
            chunk_bytes = data[start : start + CHUNK_SIZE]
            encoded = base64.b64encode(chunk_bytes).decode("ascii")
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    await self._rpc(
                        CMD_UPLOAD_CHUNK,
                        {
                            "upload_id": upload_id,
                            "chunk_index": chunk_index,
                            "base64_data": encoded,
                        },
                    )
                    last_error = None
                    break
                except WeComError as exc:
                    last_error = exc
                    if not exc.transient or attempt == 2:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
            if last_error is not None:
                raise last_error

        finish_ack = await self._rpc(CMD_UPLOAD_FINISH, {"upload_id": upload_id})
        media_id = (finish_ack.get("body") or {}).get("media_id")
        if not media_id:
            raise WeComError("upload_finish", "missing media_id in finish ack")
        return str(media_id)

    async def send_media(
        self,
        chat_id: str,
        media_type: str,
        media_id: str,
        *,
        title: str | None = None,
    ) -> dict:
        media_body: dict = {"media_id": media_id}
        if media_type == "video" and title:
            media_body["title"] = title[:128]
        return await self._rpc(
            CMD_SEND_MSG,
            {
                "chatid": chat_id,
                "msgtype": media_type,
                media_type: media_body,
            },
        )

    async def download_media(
        self,
        url: str,
        aes_key: str | None = None,
        *,
        max_bytes: int = 40 * 1024 * 1024,
    ) -> tuple[bytes, str | None, str]:
        """Download and optionally AES-256-CBC decrypt inbound WeCom media."""
        async with self.session.get(url) as response:
            if response.status >= 400:
                raise WeComError(
                    response.status,
                    f"media download HTTP {response.status}",
                    status=response.status,
                )
            if (response.content_length or 0) > max_bytes:
                raise WeComError("media_too_large", f"media exceeds {max_bytes} bytes")
            buf = bytearray()
            async for piece in response.content.iter_chunked(1 << 16):
                buf += piece
                if len(buf) > max_bytes:
                    raise WeComError("media_too_large", f"media exceeds {max_bytes} bytes")
            filename = parse_content_disposition(response.headers.get("Content-Disposition"))
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        raw = bytes(buf)
        if aes_key:
            try:
                raw = decrypt_file(raw, aes_key)
            except DecryptError as exc:
                raise WeComError("decrypt_failed", str(exc)) from exc
        return raw, filename, content_type

    async def close(self) -> None:
        self._stopped = True
        self._ready.clear()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None
        if self._close_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._close_task
            self._close_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._fail_pending("WeCom client closed")


class WeComWebhookClient:
    """Outbound-only client for WeCom Group Robot Webhooks."""

    def __init__(self, cfg: WeComConfig, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.session = session

    async def _post_json(self, payload: dict) -> dict:
        url = self.cfg.resolved_webhook_url
        if not url:
            raise WeComError("config", "webhook_url is not configured")
        async with self.session.post(url, json=payload) as response:
            text = await response.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {}
            if response.status >= 400:
                raise WeComError(
                    data.get("errcode", response.status) if isinstance(data, dict) else response.status,
                    str(data.get("errmsg", text) if isinstance(data, dict) else text),
                    status=response.status,
                )
            if not isinstance(data, dict) or "errcode" not in data:
                raise WeComError(
                    "invalid_response",
                    f"expected JSON object with errcode from webhook: {text[:200]}",
                    status=response.status,
                    transient=True,
                )
            if data["errcode"] != 0:
                raise WeComError(
                    data["errcode"],
                    str(data.get("errmsg", "webhook error")),
                    status=response.status,
                )
            return data

    async def send_markdown(self, content: str) -> dict:
        return await self._post_json({"msgtype": "markdown", "markdown": {"content": content}})

    async def send_text(self, content: str) -> dict:
        return await self._post_json({"msgtype": "text", "text": {"content": content}})

    async def send_image(self, data: bytes) -> dict:
        return await self._post_json(
            {
                "msgtype": "image",
                "image": {
                    "base64": base64.b64encode(data).decode("ascii"),
                    "md5": hashlib.md5(data).hexdigest(),
                },
            }
        )

    async def upload_file(self, filename: str, data: bytes) -> str:
        url = self.cfg.resolved_upload_url
        if not url:
            raise WeComError("config", "webhook key is required to upload files")
        form = aiohttp.FormData()
        form.add_field("media", data, filename=filename, content_type="application/octet-stream")
        async with self.session.post(url, data=form) as response:
            text = await response.text()
            try:
                body = json.loads(text) if text else {}
            except json.JSONDecodeError:
                body = {}
            if response.status >= 400:
                raise WeComError(
                    body.get("errcode", response.status) if isinstance(body, dict) else response.status,
                    str(body.get("errmsg", text) if isinstance(body, dict) else text),
                    status=response.status,
                )
            if not isinstance(body, dict) or "errcode" not in body:
                raise WeComError(
                    "invalid_response",
                    f"expected JSON object with errcode from upload_media: {text[:200]}",
                    status=response.status,
                    transient=True,
                )
            if body["errcode"] != 0:
                raise WeComError(
                    body["errcode"],
                    str(body.get("errmsg", "upload_media error")),
                    status=response.status,
                )
            media_id = body.get("media_id")
            if not media_id:
                raise WeComError("upload_media", "missing media_id in upload_media response", transient=True)
            return str(media_id)

    async def send_file(self, media_id: str) -> dict:
        return await self._post_json({"msgtype": "file", "file": {"media_id": media_id}})

    async def close(self) -> None:
        return None
