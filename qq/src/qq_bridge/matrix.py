"""Matrix appservice client and HTTP endpoint."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import uuid
from urllib.parse import quote

import aiohttp
from aiohttp import web

from .config import MatrixConfig
from .store import Store

log = logging.getLogger(__name__)


class MatrixError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Matrix HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class MediaTooLarge(MatrixError):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(413, f"media over {max_bytes} bytes")


async def _read_capped(response: aiohttp.ClientResponse, max_bytes: int | None) -> bytes:
    if max_bytes is None:
        return await response.read()
    if (response.content_length or 0) > max_bytes:
        raise MediaTooLarge(max_bytes)
    data = bytearray()
    async for piece in response.content.iter_chunked(1 << 16):
        data += piece
        if len(data) > max_bytes:
            raise MediaTooLarge(max_bytes)
    return bytes(data)


def _q(value: str) -> str:
    return quote(value, safe="")


class MatrixClient:
    def __init__(self, cfg: MatrixConfig, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.session = session
        self.base = cfg.homeserver

    async def request(
        self,
        method: str,
        path: str,
        *,
        user_id: str | None = None,
        json_body: dict | None = None,
        data: bytes | None = None,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        url = self.base + path
        request_headers = {"Accept": "application/json"}
        request_headers.update(headers or {})
        request_headers["Authorization"] = f"Bearer {self.cfg.as_token}"
        query = dict(params or {})
        if user_id:
            query["user_id"] = user_id
        async with self.session.request(
            method, url, json=json_body, data=data, headers=request_headers, params=query
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise MatrixError(response.status, text)
            if not text:
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {}

    async def resolve_alias(self, alias: str) -> str:
        data = await self.request(
            "GET", f"/_matrix/client/v3/directory/room/{_q(alias)}"
        )
        return data["room_id"]

    async def join(self, room_id: str, user_id: str) -> None:
        await self.request(
            "POST",
            f"/_matrix/client/v3/rooms/{_q(room_id)}/join",
            user_id=user_id,
            json_body={},
        )

    async def messages(
        self,
        room_id: str,
        user_id: str,
        *,
        from_token: str | None = None,
        limit: int = 100,
    ) -> dict:
        """One page of room history, newest first, as the given user."""
        params = {"dir": "b", "limit": limit}
        if from_token:
            params["from"] = from_token
        return await self.request(
            "GET",
            f"/_matrix/client/v3/rooms/{_q(room_id)}/messages",
            user_id=user_id,
            params=params,
        )

    async def iter_history(self, room_id: str, user_id: str):
        """Every event in a room, newest first."""
        token = None
        while True:
            page = await self.messages(room_id, user_id, from_token=token, limit=200)
            events = page.get("chunk") or []
            for event in events:
                yield event
            token = page.get("end")
            if not events or not token:
                return

    async def register_user(self, localpart: str, displayname: str) -> str:
        try:
            data = await self.request(
                "POST",
                "/_matrix/client/v3/register",
                params={"kind": "user"},
                json_body={
                    "username": localpart,
                    "displayname": displayname,
                    "inhibit_login": True,
                },
            )
        except MatrixError as exc:
            if "M_USER_IN_USE" in exc.body:
                return f"@{localpart}:{self.cfg.domain}"
            raise
        return data["user_id"]

    async def set_displayname(self, user_id: str, displayname: str) -> None:
        await self.request(
            "PUT",
            f"/_matrix/client/v3/profile/{_q(user_id)}/displayname",
            user_id=user_id,
            json_body={"displayname": displayname},
        )

    async def set_avatar(self, user_id: str, avatar_url: str) -> None:
        await self.request(
            "PUT",
            f"/_matrix/client/v3/profile/{_q(user_id)}/avatar_url",
            user_id=user_id,
            json_body={"avatar_url": avatar_url},
        )

    async def send_message(
        self, room_id: str, user_id: str, content: dict
    ) -> str:
        txn = uuid.uuid4().hex
        data = await self.request(
            "PUT",
            f"/_matrix/client/v3/rooms/{_q(room_id)}/send/m.room.message/{txn}",
            user_id=user_id,
            json_body=content,
        )
        return data.get("event_id", "")

    async def upload(
        self, data: bytes, content_type: str, filename: str, user_id: str
    ) -> str:
        url = f"{self.base}/_matrix/media/v3/upload"
        headers = {
            "Authorization": f"Bearer {self.cfg.as_token}",
            "Content-Type": content_type or "application/octet-stream",
        }
        params = {"filename": filename, "user_id": user_id}
        async with self.session.post(
            url, data=data, headers=headers, params=params
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise MatrixError(response.status, text)
            return json.loads(text)["content_uri"]

    async def download(self, mxc: str, max_bytes: int | None = None) -> bytes:
        """Fetch media; more than `max_bytes` raises MediaTooLarge unread."""
        if not mxc.startswith("mxc://"):
            raise ValueError(f"not an mxc uri: {mxc!r}")
        server, media_id = mxc[len("mxc://") :].split("/", 1)
        headers = {"Authorization": f"Bearer {self.cfg.as_token}"}
        urls = (
            f"{self.base}/_matrix/client/v1/media/download/{_q(server)}/{_q(media_id)}",
            f"{self.base}/_matrix/media/v3/download/{_q(server)}/{_q(media_id)}",
        )
        for index, url in enumerate(urls):
            async with self.session.get(url, headers=headers) as response:
                if response.status == 404 and index == 0:
                    continue
                if response.status >= 400:
                    raise MatrixError(response.status, await response.text())
                return await _read_capped(response, max_bytes)
        raise MatrixError(404, "media not found")

    async def member(self, room_id: str, user_id: str) -> dict | None:
        try:
            return await self.request(
                "GET",
                f"/_matrix/client/v3/rooms/{_q(room_id)}/state/m.room.member/{_q(user_id)}",
            )
        except MatrixError as exc:
            if exc.status == 404:
                return None
            raise

    async def is_encrypted(self, room_id: str) -> bool:
        try:
            await self.request(
                "GET",
                f"/_matrix/client/v3/rooms/{_q(room_id)}/state/m.room.encryption",
            )
            return True
        except MatrixError as exc:
            if exc.status == 404:
                return False
            raise

    async def redact(
        self, room_id: str, event_id: str, user_id: str, reason: str = ""
    ) -> None:
        txn = uuid.uuid4().hex
        await self.request(
            "PUT",
            f"/_matrix/client/v3/rooms/{_q(room_id)}/redact/{_q(event_id)}/{txn}",
            user_id=user_id,
            json_body={"reason": reason},
        )


class AppService:
    """Receives transactions from the homeserver and queues their events."""

    def __init__(
        self,
        cfg: MatrixConfig,
        store: Store,
        queue: asyncio.Queue,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.queue = queue
        self.runner: web.AppRunner | None = None

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        token = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
        if not token:
            token = request.query.get("access_token", "")
        return bool(token) and hmac.compare_digest(token, self.cfg.hs_token)

    async def start(self) -> None:
        app = web.Application()
        for prefix in ("/_matrix/app/v1", "/_matrix/app/r0"):
            app.router.add_get(f"{prefix}/ping", self.ping)
            app.router.add_put(f"{prefix}/transactions/{{txn_id}}", self.transaction)
            app.router.add_get(f"{prefix}/users/{{user_id}}", self.user)
            app.router.add_get(f"{prefix}/rooms/{{room_alias}}", self.room)
            app.router.add_get(f"{prefix}/thirdparty/user/{{user}}", self.not_found)
            app.router.add_get(f"{prefix}/thirdparty/location/{{location}}", self.not_found)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(
            self.runner, self.cfg.listen_host, self.cfg.listen_port
        )
        await site.start()
        log.info(
            "appservice listening on %s:%s",
            self.cfg.listen_host,
            self.cfg.listen_port,
        )

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def ping(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
        return web.json_response({})

    async def transaction(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
        txn_id = request.match_info["txn_id"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"errcode": "M_BAD_JSON"}, status=400)
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list) or not all(
            isinstance(event, dict) for event in events
        ):
            return web.json_response({"errcode": "M_BAD_JSON"}, status=400)
        seqs = self.store.accept_transaction(txn_id, events)
        if seqs is None:
            return web.json_response({})
        for seq, event in zip(seqs, events):
            self.queue.put_nowait((seq, event))
        log.debug("transaction %s: %d events", txn_id, len(events))
        return web.json_response({})

    async def user(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
        user_id = request.match_info["user_id"]
        if user_id.startswith("@qq_") or user_id == self.cfg.bot_mxid:
            return web.json_response({})
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

    async def room(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

    async def not_found(self, request: web.Request) -> web.Response:
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)
