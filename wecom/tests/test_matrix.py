import asyncio
from pathlib import Path

import aiohttp

from aiohttp import web

from wecom_bridge.config import MatrixConfig
from wecom_bridge.matrix import AppService, MatrixClient
from wecom_bridge.store import Store


def test_appservice_endpoints(tmp_path: Path):
    async def scenario():
        cfg = MatrixConfig(
            homeserver="http://hs:8008",
            domain="example.org",
            as_token="as-secret",
            hs_token="hs-secret",
            bot_mxid="@wecombot:example.org",
            listen_host="127.0.0.1",
            listen_port=0,
        )
        store = Store(tmp_path / "state.sqlite3")
        queue: asyncio.Queue = asyncio.Queue()
        app = AppService(cfg, store, queue)
        await app.start()
        port = next(iter(app.runner.sites))._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        headers = {"Authorization": "Bearer hs-secret"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/_matrix/app/v1/ping", json={}) as resp:
                    assert resp.status == 403
                async with session.post(
                    f"{base}/_matrix/app/v1/ping", headers=headers, json={"transaction_id": "p1"}
                ) as resp:
                    assert resp.status == 200
                async with session.get(f"{base}/_matrix/app/v1/ping", headers=headers) as resp:
                    assert resp.status == 200
                async with session.get(
                    f"{base}/_matrix/app/v1/users/@wecom_alice:example.org", headers=headers
                ) as resp:
                    assert resp.status == 200
                async with session.get(
                    f"{base}/_matrix/app/v1/users/@other:example.org", headers=headers
                ) as resp:
                    assert resp.status == 404
                async with session.put(
                    f"{base}/_matrix/app/v1/transactions/txn-1",
                    headers=headers,
                    json={"events": [{"event_id": "$1", "type": "m.room.message"}]},
                ) as resp:
                    assert resp.status == 200
                # Duplicate txn_id is acknowledged without queueing twice.
                async with session.put(
                    f"{base}/_matrix/app/v1/transactions/txn-1",
                    headers=headers,
                    json={"events": [{"event_id": "$1", "type": "m.room.message"}]},
                ) as resp:
                    assert resp.status == 200
                assert queue.qsize() == 1
        finally:
            await app.stop()
            store.close()

    asyncio.run(scenario())


def test_matrix_client_register_user_sets_appservice_type():
    async def scenario():
        seen_register: list[tuple[dict, dict]] = []

        async def register_handler(request: web.Request) -> web.Response:
            seen_register.append((dict(request.query), await request.json()))
            return web.json_response({"user_id": "@wecom_alice:example.org"})

        app = web.Application()
        app.router.add_post("/_matrix/client/v3/register", register_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        cfg = MatrixConfig(
            homeserver=f"http://127.0.0.1:{port}",
            domain="example.org",
            as_token="as-secret",
            hs_token="hs-secret",
            bot_mxid="@wecombot:example.org",
        )
        try:
            async with aiohttp.ClientSession() as session:
                client = MatrixClient(cfg, session)
                uid = await client.register_user("wecom_alice", "Alice")
                assert uid == "@wecom_alice:example.org"
        finally:
            await runner.cleanup()

        assert seen_register == [
            (
                {"kind": "user"},
                {
                    "type": "m.login.application_service",
                    "username": "wecom_alice",
                    "displayname": "Alice",
                    "inhibit_login": True,
                },
            )
        ]

    asyncio.run(scenario())
