import asyncio

from qq_bridge.config import Config, MatrixConfig, QQConfig, RoomConfig
from qq_bridge.relay import Relay
from qq_bridge.store import Store

ROOM = "!room:example.org"
OTHER = "!other:example.org"


class FakeQQ:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.delete_error = None

    async def send_group_text(self, group, content, *, reference=None, msg_id=None):
        self.sent.append((group, content, reference))
        return {
            "id": f"Q{len(self.sent)}",
            "ext_info": {"ref_idx": f"REF{len(self.sent)}"},
        }

    async def upload_group_media(self, group, data, filename, file_type):
        return "FILEINFO"

    async def send_group_media(self, group, file_info):
        return {"id": "QM"}

    async def delete_group_message(self, group, message_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(message_id)


class FakeMatrix:
    async def member(self, room_id, user_id):
        return {"displayname": "Alice"}

    async def download(self, url, max_bytes=None):
        return b"image"


def make_relay(tmp_path, qq, rooms=None):
    rooms = rooms or (RoomConfig(alias="#in:example.org", label="in"),)
    cfg = Config(
        matrix=MatrixConfig(
            homeserver="http://hs",
            domain="example.org",
            as_token="as",
            hs_token="hs",
            bot_mxid="@qqbot:example.org",
        ),
        qq=QQConfig(app_id="", app_secret="", group="G", incoming="#in:example.org"),
        rooms=rooms,
        ignore_senders=(),
        ignore_notices=True,
        state_path=tmp_path / "state.sqlite3",
    )
    store = Store(cfg.state_path)
    relay = Relay(cfg, store, FakeMatrix(), qq)
    relay._labels = {ROOM: rooms[0].label}
    if len(rooms) > 1:
        relay._labels[OTHER] = rooms[-1].label
    return relay, store


def text_event(body="hello", **extra):
    content = {"msgtype": "m.text", "body": body, **extra}
    return {
        "room_id": ROOM,
        "sender": "@alice:example.org",
        "type": "m.room.message",
        "event_id": "$e",
        "content": content,
    }


def test_text_is_queued_with_label(tmp_path):
    relay, store = make_relay(
        tmp_path,
        FakeQQ(),
        rooms=(
            RoomConfig(alias="#in:example.org", label="in"),
            RoomConfig(alias="#two:example.org", label="two"),
        ),
    )
    asyncio.run(relay.handle_matrix_event(text_event()))
    job = relay._queue.get_nowait()
    assert job["text"] == "[in] Alice: hello"
    assert job["group"] == "G"
    assert job["event_id"] == "$e"
    store.close()


def test_single_room_has_no_label(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    asyncio.run(relay.handle_matrix_event(text_event()))
    assert relay._queue.get_nowait()["text"] == "Alice: hello"
    store.close()


def test_edit_is_ignored(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    event = text_event(
        body="* fixed",
        **{
            "m.new_content": {"body": "fixed", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
        },
    )
    asyncio.run(relay.handle_matrix_event(event))
    assert relay._queue.empty()
    store.close()


def test_own_and_ghost_senders_are_ignored(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    for sender in ("@qqbot:example.org", "@qq_abc:example.org"):
        event = text_event()
        event["sender"] = sender
        asyncio.run(relay.handle_matrix_event(event))
    assert relay._queue.empty()
    store.close()


def test_unmirrored_room_is_ignored(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    event = text_event()
    event["room_id"] = OTHER
    asyncio.run(relay.handle_matrix_event(event))
    assert relay._queue.empty()
    store.close()


def test_reply_uses_the_stored_reference(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    event = text_event(**{"m.relates_to": {"m.in_reply_to": {"event_id": "$orig"}}})
    asyncio.run(relay.handle_matrix_event(event))
    assert relay._queue.get_nowait()["reference"] == "REF9"
    store.close()


def test_media_is_queued(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    event = {
        "room_id": ROOM,
        "sender": "@alice:example.org",
        "type": "m.room.message",
        "event_id": "$img",
        "content": {
            "msgtype": "m.image",
            "body": "photo.png",
            "url": "mxc://example.org/photo",
        },
    }
    asyncio.run(relay.handle_matrix_event(event))
    job = relay._queue.get_nowait()
    assert job["kind"] == "media"
    assert relay._images.get_nowait() is job
    assert relay._videos.empty()
    assert job["content"]["url"] == "mxc://example.org/photo"
    store.close()


def test_redaction_deletes_the_qq_copy(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    event = {
        "room_id": ROOM,
        "sender": "@alice:example.org",
        "type": "m.room.redaction",
        "redacts": "$orig",
        "content": {},
    }
    asyncio.run(relay.handle_matrix_event(event))
    assert qq.deleted == ["Q9"]
    store.close()


def test_send_records_the_message_and_skips_repeats(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    job = {
        "kind": "text",
        "group": "G",
        "room": ROOM,
        "event_id": "$e",
        "text": "Alice: hello",
        "reference": None,
    }
    asyncio.run(relay._send_text(job))
    assert qq.sent == [("G", "Alice: hello", None)]
    assert store.qq_for_event("$e")["qq_id"] == "Q1"
    asyncio.run(relay.handle_matrix_event(text_event()))
    assert relay._queue.empty()
    store.close()



def _run_media(relay, content, prepared, qq=None):
    """Queue one media event, settle its preparation, and send it."""

    async def go():
        relay._enqueue_media(
            {
                "kind": "media",
                "group": "G",
                "room": ROOM,
                "event_id": "$img",
                "sender": "@alice:example.org",
                "content": content,
            }
        )
        job = relay._queue.get_nowait()
        lane = relay._videos if content.get("msgtype") == "m.video" else relay._images
        lane.get_nowait()
        job["ready"].set_result(prepared)
        await relay._send_media(job)
        assert job["sent"].is_set()

    asyncio.run(go())


def test_media_that_cannot_fit_becomes_a_notice(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix = FakeMatrix()
    _run_media(relay, {"msgtype": "m.video", "url": "mxc://x/y"}, "too long for QQ")
    assert qq.sent[-1][1] == "Alice: [video too long for QQ]"
    store.close()


def test_refused_format_is_reencoded_once(tmp_path):
    from qq_bridge.fit import Prepared
    from qq_bridge.qq import QQError

    class RefusingQQ(FakeQQ):
        async def upload_group_media(self, group, data, filename, file_type):
            raise QQError(40034002, "富媒体文件格式不支持")

    relay, store = make_relay(tmp_path, RefusingQQ())

    async def go():
        relay._enqueue_media(
            {"kind": "media", "group": "G", "room": ROOM, "event_id": "$img",
             "sender": "@alice:example.org", "content": {"msgtype": "m.image"}}
        )
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"x", "a.heic", 1))
        await relay._send_media(job)
        retry = relay._queue.get_nowait()
        assert retry["force"] is True
        assert relay._images.get_nowait() is retry
        retry["ready"].set_result(Prepared(b"x", "a.jpg", 1))
        await relay._send_media(retry)
        assert relay._queue.empty()

    asyncio.run(go())
    store.close()


def test_slow_media_lets_later_messages_pass(tmp_path, monkeypatch):
    from qq_bridge.fit import Prepared

    monkeypatch.setattr("qq_bridge.relay.MEDIA_WAIT", 0.01)
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)

    async def go():
        relay._enqueue_media(
            {"kind": "media", "group": "G", "room": ROOM, "event_id": "$v",
             "sender": "@alice:example.org", "content": {"msgtype": "m.video"}}
        )
        job = relay._queue.get_nowait()
        await relay._send_media(job)
        assert relay._queue.empty() and not job["sent"].is_set()
        job["ready"].set_result(Prepared(b"v", "v.mp4", 2))
        await asyncio.sleep(0)
        assert relay._queue.get_nowait() is job

    asyncio.run(go())
    store.close()
