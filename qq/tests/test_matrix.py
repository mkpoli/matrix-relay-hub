"""Matrix transaction handling in the application service."""

import asyncio
import json
from types import SimpleNamespace

from qq_bridge.matrix import AppService
from qq_bridge.store import Store


class FakeRequest:
    def __init__(self, body, txn_id="txn"):
        self.headers = {"Authorization": "Bearer hs-token"}
        self.query = {}
        self.match_info = {"txn_id": txn_id}
        self.body = body

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def build(tmp_path, queue):
    store = Store(tmp_path / "state.sqlite3")
    service = AppService(SimpleNamespace(hs_token="hs-token"), store, queue)
    return service, store


def test_malformed_body_leaves_transaction_unconsumed(tmp_path):
    queue = asyncio.Queue()
    service, store = build(tmp_path, queue)
    bad = asyncio.run(
        service.transaction(FakeRequest(json.JSONDecodeError("bad", "", 0)))
    )
    retry = asyncio.run(
        service.transaction(FakeRequest({"events": [{"event_id": "$e"}]}))
    )
    assert bad.status == 400
    assert retry.status == 200
    assert queue.qsize() == 1
    store.close()


def test_invalid_events_field_is_rejected_without_consuming(tmp_path):
    queue = asyncio.Queue()
    service, store = build(tmp_path, queue)
    for body in ([], {"events": "no"}, {"events": [42]}):
        response = asyncio.run(service.transaction(FakeRequest(body)))
        assert response.status == 400
    retry = asyncio.run(
        service.transaction(FakeRequest({"events": [{"event_id": "$e"}]}))
    )
    assert retry.status == 200
    assert queue.qsize() == 1
    store.close()


def test_replayed_transaction_is_queued_once(tmp_path):
    queue = asyncio.Queue()
    service, store = build(tmp_path, queue)
    body = {"events": [{"event_id": "$e"}]}
    first = asyncio.run(service.transaction(FakeRequest(body)))
    second = asyncio.run(service.transaction(FakeRequest(body)))
    assert (first.status, second.status) == (200, 200)
    assert queue.qsize() == 1
    store.close()


class FakeContent:
    def __init__(self, pieces):
        self.pieces = pieces

    async def iter_chunked(self, size):
        for piece in self.pieces:
            yield piece


class FakeResponse:
    def __init__(self, pieces, content_length=None):
        self.content = FakeContent(pieces)
        self.content_length = content_length

    async def read(self):
        return b"".join(self.content.pieces)


def test_download_cap_stops_reading_past_the_limit():
    import pytest
    from qq_bridge.matrix import MediaTooLarge, _read_capped

    assert asyncio.run(_read_capped(FakeResponse([b"ab", b"cd"]), 4)) == b"abcd"
    with pytest.raises(MediaTooLarge):
        asyncio.run(_read_capped(FakeResponse([b"ab", b"cd", b"e"]), 4))
    with pytest.raises(MediaTooLarge):
        asyncio.run(_read_capped(FakeResponse([], content_length=5), 4))
