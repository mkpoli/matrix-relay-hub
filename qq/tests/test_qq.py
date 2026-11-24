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
