import asyncio
import base64
import hashlib
import json

import aiohttp
from aiohttp import web
import pytest

from wecom_bridge.config import WeComConfig
from wecom_bridge.crypto import encrypt_file
from wecom_bridge.wecom import (
    CHUNK_SIZE,
    CMD_EVENT_CALLBACK,
    CMD_MSG_CALLBACK,
    CMD_PING,
    CMD_SEND_MSG,
    CMD_SUBSCRIBE,
    CMD_UPLOAD_CHUNK,
    CMD_UPLOAD_FINISH,
    CMD_UPLOAD_INIT,
    WeComAIBotClient,
    WeComError,
    WeComWebhookClient,
)


def _aes_key() -> str:
    return base64.b64encode(bytes(range(32))).decode("ascii").rstrip("=")


def test_aibot_ws_auth_send_upload_download_and_disconnect():
    async def scenario():
        received_frames: list[dict] = []
        uploaded_chunks: dict[int, bytes] = {}
        sent_messages: list[dict] = []
        chunk_failures = {"count": 0}
        encrypted_blob = encrypt_file(b"\x89PNG\r\n\x1a\nfake-png-body", _aes_key())
        ws_server_ref: dict[str, web.WebSocketResponse] = {}

        async def ws_handler(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            ws_server_ref["ws"] = ws
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                frame = json.loads(msg.data)
                cmd = frame.get("cmd")
                req_id = frame["headers"]["req_id"]
                if cmd == CMD_SUBSCRIBE:
                    assert frame["body"] == {"bot_id": "bot1", "secret": "sec1"}
                    await ws.send_json({"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"})
                    # Push an inbound message callback right after auth.
                    await ws.send_json(
                        {
                            "cmd": CMD_MSG_CALLBACK,
                            "headers": {"req_id": "cb-1"},
                            "body": {
                                "msgid": "m-1",
                                "chattype": "group",
                                "chatid": "chat-1",
                                "from": {"userid": "zhangsan"},
                                "msgtype": "text",
                                "text": {"content": "@Bot hi"},
                            },
                        }
                    )
                elif cmd == CMD_PING:
                    await ws.send_json({"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"})
                elif cmd == CMD_SEND_MSG:
                    sent_messages.append(frame["body"])
                    await ws.send_json({"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"})
                elif cmd == CMD_UPLOAD_INIT:
                    await ws.send_json(
                        {
                            "headers": {"req_id": req_id},
                            "errcode": 0,
                            "errmsg": "ok",
                            "body": {"upload_id": "up-1"},
                        }
                    )
                elif cmd == CMD_UPLOAD_CHUNK:
                    idx = frame["body"]["chunk_index"]
                    if idx == 0 and chunk_failures["count"] == 0:
                        chunk_failures["count"] += 1
                        await ws.send_json(
                            {"headers": {"req_id": req_id}, "errcode": 846616, "errmsg": "busy"}
                        )
                        continue
                    uploaded_chunks[idx] = base64.b64decode(frame["body"]["base64_data"])
                    await ws.send_json({"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"})
                elif cmd == CMD_UPLOAD_FINISH:
                    await ws.send_json(
                        {
                            "headers": {"req_id": req_id},
                            "errcode": 0,
                            "errmsg": "ok",
                            "body": {"type": "file", "media_id": "media-xyz"},
                        }
                    )
            return ws

        async def media_handler(request: web.Request) -> web.Response:
            return web.Response(
                body=encrypted_blob,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": "attachment; filename*=UTF-8''%E5%9B%BE.png",
                },
            )

        app = web.Application()
        app.router.add_get("/ws", ws_handler)
        app.router.add_get("/media", media_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        cfg = WeComConfig(
            mode="aibot",
            bot_id="bot1",
            secret="sec1",
            chat_id="chat-1",
            incoming="#main:example.org",
            ws_url=f"ws://127.0.0.1:{port}/ws",
        )
        got_callback = asyncio.Event()

        async def on_frame(frame: dict) -> None:
            received_frames.append(frame)
            got_callback.set()

        async with aiohttp.ClientSession() as session:
            client = WeComAIBotClient(cfg, session, heartbeat_interval=0.2, ack_timeout=3.0)
            task = asyncio.create_task(client.run(on_frame))
            try:
                await client.wait_ready(timeout=3.0)
                await asyncio.wait_for(got_callback.wait(), timeout=3.0)
                assert received_frames[0]["body"]["msgid"] == "m-1"

                await client.send_markdown("chat-1", "hello **wecom**")
                assert sent_messages[0] == {
                    "chatid": "chat-1",
                    "msgtype": "markdown",
                    "markdown": {"content": "hello **wecom**"},
                }

                # Upload > 1 chunk (512 KiB + 10 bytes) to exercise chunking + retry.
                payload = b"A" * CHUNK_SIZE + b"0123456789"
                media_id = await client.upload_media("file", "big.bin", payload)
                assert media_id == "media-xyz"
                assert sorted(uploaded_chunks) == [0, 1]
                assert uploaded_chunks[0] + uploaded_chunks[1] == payload

                await client.send_media("chat-1", "file", media_id)
                assert sent_messages[1] == {
                    "chatid": "chat-1",
                    "msgtype": "file",
                    "file": {"media_id": "media-xyz"},
                }

                # Download + AES-256-CBC decrypt inbound media.
                plain, fname, _ = await client.download_media(
                    f"http://127.0.0.1:{port}/media", _aes_key()
                )
                assert plain == b"\x89PNG\r\n\x1a\nfake-png-body"
                assert fname == "图.png"

                # Simulate server kicking this connection via disconnected_event.
                await ws_server_ref["ws"].send_json(
                    {
                        "cmd": CMD_EVENT_CALLBACK,
                        "headers": {"req_id": "ev-1"},
                        "body": {"event": {"eventtype": "disconnected_event"}},
                    }
                )
                await asyncio.wait_for(task, timeout=3.0)
                assert client._kicked is True
            finally:
                if not task.done():
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                await client.close()
                await runner.cleanup()

    asyncio.run(scenario())


def test_webhook_client_send_and_upload():
    async def scenario():
        seen_sends: list[dict] = []
        seen_uploads: list[tuple[str, bytes]] = []

        async def send_handler(request: web.Request) -> web.Response:
            assert request.query["key"] == "k-test"
            body = await request.json()
            if body.get("msgtype") == "text" and body["text"]["content"] == "ratelimit":
                return web.json_response({"errcode": 45009, "errmsg": "freq limit"})
            seen_sends.append(body)
            return web.json_response({"errcode": 0, "errmsg": "ok"})

        async def upload_handler(request: web.Request) -> web.Response:
            assert request.query["key"] == "k-test"
            assert request.query["type"] == "file"
            reader = await request.multipart()
            part = await reader.next()
            assert part.name == "media"
            data = await part.read()
            seen_uploads.append((part.filename, data))
            return web.json_response({"errcode": 0, "errmsg": "ok", "media_id": "wh-media-1"})

        app = web.Application()
        app.router.add_post("/cgi-bin/webhook/send", send_handler)
        app.router.add_post("/cgi-bin/webhook/upload_media", upload_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        cfg = WeComConfig(
            mode="webhook",
            webhook_key="k-test",
            api_base=f"http://127.0.0.1:{port}",
        )
        async with aiohttp.ClientSession() as session:
            client = WeComWebhookClient(cfg, session)
            await client.send_markdown("hi **team**")
            await client.send_text("plain hi")
            img_bytes = b"\x89PNG\r\n\x1a\n123456"
            await client.send_image(img_bytes)
            media_id = await client.upload_file("doc.txt", b"hello document")
            await client.send_file(media_id)

            with pytest.raises(WeComError) as exc_info:
                await client.send_text("ratelimit")
            assert exc_info.value.rate_limited is True
            assert exc_info.value.transient is True

        await runner.cleanup()

        assert seen_sends[0] == {"msgtype": "markdown", "markdown": {"content": "hi **team**"}}
        assert seen_sends[1] == {"msgtype": "text", "text": {"content": "plain hi"}}
        assert seen_sends[2] == {
            "msgtype": "image",
            "image": {
                "base64": base64.b64encode(img_bytes).decode("ascii"),
                "md5": hashlib.md5(img_bytes).hexdigest(),
            },
        }
        assert seen_sends[3] == {"msgtype": "file", "file": {"media_id": "wh-media-1"}}
        assert seen_uploads == [("doc.txt", b"hello document")]

    asyncio.run(scenario())
