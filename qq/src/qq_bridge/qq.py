"""QQ official bot API and gateway client."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time

import aiohttp

from .config import QQConfig

log = logging.getLogger(__name__)

RATE_LIMIT_CODES = {1100100, 304044, 304045, 304046, 304047, 304048, 304049, 304050}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class QQError(Exception):
    def __init__(
        self,
        code: int | str,
        message: str,
        *,
        status: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(f"QQ error {code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.trace_id = trace_id

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 or self.code in RATE_LIMIT_CODES


class QQClient:
    def __init__(self, cfg: QQConfig, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.session = session
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._handler = None
        self._worker: asyncio.Task | None = None

    async def access_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if (
                not force
                and self._access_token
                and time.time() < self._expires_at - 60
            ):
                return self._access_token
            async with self.session.post(
                f"{self.cfg.api_base}/app/getAppAccessToken",
                json={"appId": self.cfg.app_id, "clientSecret": self.cfg.app_secret},
            ) as response:
                data = await response.json(content_type=None)
            if not isinstance(data, dict) or not data.get("access_token"):
                raise QQError(
                    data.get("code", "token") if isinstance(data, dict) else "token",
                    data.get("message", "no access token") if isinstance(data, dict) else "bad response",
                )
            self._access_token = data["access_token"]
            self._expires_at = time.time() + int(data.get("expires_in", 7200))
            return self._access_token

    async def request(self, method: str, path: str, **kwargs) -> dict:
        last_error: Exception | None = None
        for attempt in range(2):
            token = await self.access_token(force=attempt > 0)
            headers = {"Authorization": f"QQBot {token}"}
            async with self.session.request(
                method, f"{self.cfg.api_base}{path}", headers=headers, **kwargs
            ) as response:
                text = await response.text()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    data = {}
                if response.status == 401 and attempt == 0:
                    last_error = QQError(401, "unauthorized", status=401)
                    continue
                if response.status == 429:
                    raise QQError(429, "rate limited", status=429)
                if response.status >= 400:
                    raise QQError(
                        data.get("err_code", response.status),
                        data.get("message", text),
                        status=response.status,
                        trace_id=data.get("trace_id"),
                    )
                if isinstance(data, dict) and data.get("err_code"):
                    raise QQError(
                        data["err_code"],
                        data.get("message", ""),
                        status=response.status,
                        trace_id=data.get("trace_id"),
                    )
                return data
        raise last_error or QQError("request", "request failed")

    # -- sending ----------------------------------------------------------

    async def send_group_text(
        self,
        group_openid: str,
        content: str,
        *,
        reference: str | None = None,
        msg_id: str | None = None,
        msg_seq: int = 1,
    ) -> dict:
        payload: dict = {"content": content, "msg_type": 0}
        if reference:
            payload["message_reference"] = {"message_id": reference}
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = msg_seq
        return await self.request(
            "POST", f"/v2/groups/{group_openid}/messages", json=payload
        )

    async def upload_group_media(
        self, group_openid: str, data: bytes, filename: str, file_type: int
    ) -> str:
        """Upload a file and return its file_info. 5 MB is the binary limit."""
        payload = {
            "file_type": file_type,
            "file_name": filename,
            "file_data": await asyncio.to_thread(_b64, data),
            "srv_send_msg": False,
        }
        response = await self.request(
            "POST", f"/v2/groups/{group_openid}/files", json=payload
        )
        return response["file_info"]

    async def send_group_media(self, group_openid: str, file_info: str) -> dict:
        return await self.request(
            "POST",
            f"/v2/groups/{group_openid}/messages",
            json={"msg_type": 7, "media": {"file_info": file_info}},
        )

    async def delete_group_message(self, group_openid: str, message_id: str) -> None:
        await self.request(
            "DELETE", f"/v2/groups/{group_openid}/messages/{message_id}"
        )

    async def gateway_url(self) -> str:
        data = await self.request("GET", "/gateway")
        return data["url"]

    # -- gateway ----------------------------------------------------------

    async def run(self, handler) -> None:
        self._handler = handler
        self._worker = asyncio.create_task(self._dispatch())
        session_id: str | None = None
        seq: int | None = None
        backoff = 1
        while True:
            try:
                token = await self.access_token()
                url = await self.gateway_url()
                async with self.session.ws_connect(
                    url, heartbeat=None, max_msg_size=32 * 1024 * 1024
                ) as ws:
                    hello = await ws.receive_json(timeout=30)
                    interval = hello["d"]["heartbeat_interval"] / 1000
                    state = {"seq": seq}
                    heartbeat = asyncio.create_task(
                        self._heartbeat(ws, interval, state)
                    )
                    try:
                        if session_id and seq is not None:
                            await ws.send_json(
                                {
                                    "op": 6,
                                    "d": {
                                        "token": f"QQBot {token}",
                                        "session_id": session_id,
                                        "seq": seq,
                                    },
                                }
                            )
                            log.info("gateway resuming session %s", session_id)
                        else:
                            await ws.send_json(
                                {
                                    "op": 2,
                                    "d": {
                                        "token": f"QQBot {token}",
                                        "intents": self.cfg.intents,
                                        "shard": [0, 1],
                                        "properties": {
                                            "$os": "linux",
                                            "$browser": "qq-bridge",
                                            "$device": "qq-bridge",
                                        },
                                    },
                                }
                            )
                        async for message in ws:
                            if message.type != aiohttp.WSMsgType.TEXT:
                                if message.type in (
                                    aiohttp.WSMsgType.CLOSED,
                                    aiohttp.WSMsgType.CLOSE,
                                    aiohttp.WSMsgType.ERROR,
                                ):
                                    break
                                continue
                            payload = json.loads(message.data)
                            op = payload.get("op")
                            if op == 0:
                                seq = payload.get("s", seq)
                                state["seq"] = seq
                                event_type = payload.get("t")
                                if event_type == "READY":
                                    session_id = payload["d"].get("session_id")
                                    backoff = 1
                                    user = payload["d"].get("user", {})
                                    log.info(
                                        "gateway ready as %s (%s)",
                                        user.get("username"),
                                        user.get("id"),
                                    )
                                elif event_type:
                                    self._events.put_nowait((event_type, payload["d"]))
                            elif op == 9:
                                log.info("gateway session invalid, reconnecting")
                                session_id = None
                                seq = None
                                break
                            elif op == 7:
                                log.info("gateway asked to reconnect")
                                break
                    finally:
                        heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("gateway connection failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _heartbeat(self, ws, interval: float, state: dict) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send_json({"op": 1, "d": state["seq"]})
        except (asyncio.CancelledError, aiohttp.ClientError, ConnectionError):
            pass

    async def _dispatch(self) -> None:
        while True:
            event_type, data = await self._events.get()
            try:
                await self._handler(event_type, data)
            except Exception:
                log.exception("failed to handle %s", event_type)

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
