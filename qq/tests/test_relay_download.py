import asyncio

import pytest

from qq_bridge.fit import Unfit
from qq_bridge.matrix import MediaTooLarge
from test_relay import FakeMatrix, FakeQQ, make_relay


def test_media_too_large_to_download_becomes_a_notice(tmp_path):
    class HugeMatrix(FakeMatrix):
        async def download(self, url, max_bytes=None):
            raise MediaTooLarge(max_bytes)

    relay, store = make_relay(tmp_path, FakeQQ())
    relay.matrix = HugeMatrix()
    job = {"event_id": "$v", "content": {"msgtype": "m.video", "url": "mxc://x/y"}}
    with pytest.raises(Unfit, match="too large"):
        asyncio.run(relay._prepare_media(job))
    store.close()
