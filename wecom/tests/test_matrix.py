import asyncio
from pathlib import Path

import aiohttp

from wecom_bridge.config import MatrixConfig
from wecom_bridge.matrix import AppService
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
                async with session.get(f"{base}/_matrix/app/v1/ping") as resp:
                    assert resp.status == 403
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
