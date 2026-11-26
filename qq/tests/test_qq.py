import asyncio

from qq_bridge.config import QQConfig
from qq_bridge.qq import QQClient


def _client():
    return QQClient(QQConfig(app_id="", app_secret="", group="G", incoming="#in"), object())


def test_group_text_carries_a_passive_reply():
    client = _client()
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    client.request = request
    asyncio.run(
        client.send_group_text(
            "G", "hello", msg_id="Q7", msg_seq=2
        )
    )
    payload = calls[0][2]["json"]
    assert payload["msg_id"] == "Q7"
    assert payload["msg_seq"] == 2
    assert payload["content"] == "hello"


def test_group_media_carries_a_passive_reply():
    client = _client()
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    client.request = request
    asyncio.run(
        client.send_group_media("G", "FILE", msg_id="Q7", msg_seq=4)
    )
    payload = calls[0][2]["json"]
    assert payload["msg_id"] == "Q7"
    assert payload["msg_seq"] == 4
    assert payload["media"] == {"file_info": "FILE"}
    assert "content" not in payload


def test_group_media_carries_content_when_given():
    client = _client()
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    client.request = request
    asyncio.run(client.send_group_media("G", "FILE", content="[D] Alice:"))
    payload = calls[0][2]["json"]
    assert payload == {
        "msg_type": 7,
        "media": {"file_info": "FILE"},
        "content": "[D] Alice:",
    }


def test_group_media_carries_reference_when_given():
    client = _client()
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    client.request = request
    asyncio.run(
        client.send_group_media(
            "G", "FILE", content="[D] mkpoli → ItMarki:", reference="REF1"
        )
    )
    payload = calls[0][2]["json"]
    assert payload == {
        "msg_type": 7,
        "media": {"file_info": "FILE"},
        "content": "[D] mkpoli → ItMarki:",
        "message_reference": {"message_id": "REF1"},
    }


def test_group_text_without_a_reply_has_no_passive_fields():
    client = _client()
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    client.request = request
    asyncio.run(client.send_group_text("G", "hello"))
    payload = calls[0][2]["json"]
    assert "msg_id" not in payload
    assert "msg_seq" not in payload


def test_gateway_closes_zombie_ws_on_missed_ack_and_handles_resume_and_server_ping():
    import json
    from types import SimpleNamespace
    import aiohttp

    class FakeWS:
        def __init__(self, script, *, auto_ack: bool = False):
            self.sent = []
            self.closed = False
            self._queue: asyncio.Queue = asyncio.Queue()
            self._script = list(script)
            self._auto_ack = auto_ack
            self.heartbeats_acked = 0
            self.two_heartbeats = asyncio.Event()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def receive_json(self, timeout=None):
            return self._script.pop(0)

        async def send_json(self, payload):
            self.sent.append(payload)
            if payload.get("op") in (2, 6):
                for item in self._script:
                    self._queue.put_nowait(item)
                self._script.clear()
            elif payload.get("op") == 1 and self._auto_ack:
                self.heartbeats_acked += 1
                self._queue.put_nowait(text_msg({"op": 11, "d": None}))
                if self.heartbeats_acked >= 2:
                    self.two_heartbeats.set()

        async def close(self):
            if not self.closed:
                self.closed = True
                self._queue.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=""))

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self._queue.get()
            if item.type == aiohttp.WSMsgType.CLOSED:
                raise StopAsyncIteration
            return item

    def text_msg(obj):
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(obj))

    # First connection: READY (s=1), server-initiated op:1 (must reply with d=1),
    # one normal event (s=2), then periodic op:1 (d=2) with NO op:11 ACK -> zombie close.
    ws1 = FakeWS(
        [
            {"op": 10, "d": {"heartbeat_interval": 30}},
            text_msg({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1", "user": {"username": "Bot", "id": "1"}}}),
            text_msg({"op": 1, "d": None}),
            text_msg({"op": 0, "s": 2, "t": "GROUP_MESSAGE_CREATE", "d": {"id": "M1"}}),
        ]
    )
    # Second connection: RESUMED (must not be dispatched to handler), auto-ACKs
    # outbound heartbeats and stays open across multiple heartbeat intervals.
    ws2 = FakeWS(
        [
            {"op": 10, "d": {"heartbeat_interval": 30}},
            text_msg({"op": 0, "s": 3, "t": "RESUMED", "d": {}}),
        ],
        auto_ack=True,
    )

    sockets = [ws1, ws2]
    connect_calls = 0

    class FakeSession:
        def ws_connect(self, url, **kwargs):
            nonlocal connect_calls
            connect_calls += 1
            return sockets.pop(0)

    client = QQClient(QQConfig(app_id="a", app_secret="s", group="G", incoming="#in"), FakeSession())

    async def fake_token(force=False):
        return "TOK"

    async def fake_gw():
        return "wss://example.invalid/ws"

    client.access_token = fake_token
    client.gateway_url = fake_gw

    received = []

    async def handler(event_type, data):
        received.append((event_type, data))

    async def run_test():
        task = asyncio.create_task(client.run(handler))
        await asyncio.wait_for(ws2.two_heartbeats.wait(), timeout=2.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await client.close()

    asyncio.run(run_test())
    assert connect_calls == 2
    assert ws1.closed is True
    assert ws2.closed is False
    # First connection sent identify (op 2), server-ping reply (d=1), then periodic heartbeat (d=2).
    assert ws1.sent[0]["op"] == 2
    heartbeats_ws1 = [msg for msg in ws1.sent if msg["op"] == 1]
    assert heartbeats_ws1 == [{"op": 1, "d": 1}, {"op": 1, "d": 2}]
    # Second connection resumed session S1 at seq 2 and survived multiple ACKed heartbeats.
    assert ws2.sent[0] == {"op": 6, "d": {"token": "QQBot TOK", "session_id": "S1", "seq": 2}}
    assert ws2.heartbeats_acked >= 2
    # RESUMED was handled internally; only GROUP_MESSAGE_CREATE reached handler.
    assert received == [("GROUP_MESSAGE_CREATE", {"id": "M1"})]
