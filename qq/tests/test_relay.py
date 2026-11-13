import asyncio

from qq_bridge.config import Config, MatrixConfig, QQConfig, RoomConfig
from qq_bridge.formatting import chunk
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

    async def download(self, url):
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



def _pending(store):
    return [row[0] for row in store.conn.execute("SELECT seq FROM pending ORDER BY seq")]


async def _run_loops(relay, until):
    """Run the Matrix consumer and the send loop until `until()` holds, then stop
    them the way a shutdown does."""
    tasks = [
        asyncio.create_task(relay._consume_matrix()),
        asyncio.create_task(relay._send_loop()),
    ]
    for _ in range(200):
        await asyncio.sleep(0)
        if until():
            break
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_event_cut_off_by_a_restart_is_sent_afterwards(tmp_path):
    class HangingQQ(FakeQQ):
        async def send_group_text(self, *args, **kwargs):
            await asyncio.Event().wait()

    first, store = make_relay(tmp_path, HangingQQ())
    (seq,) = store.accept_transaction("t1", [text_event()])
    first.incoming.put_nowait((seq, text_event()))
    # The send is under way when the bridge stops.
    asyncio.run(_run_loops(first, lambda: first._queue.empty() and first._outstanding))
    assert _pending(store) == [seq]

    qq = FakeQQ()
    second = Relay(first.cfg, store, FakeMatrix(), qq)
    second._labels = dict(first._labels)

    async def after_restart():
        second._claim_pending(idle=0)
        await _run_loops(second, lambda: qq.sent)

    asyncio.run(after_restart())
    assert [content for _, content, _ in qq.sent] == ["Alice: hello"]
    assert _pending(store) == []
    store.close()


def test_long_text_resumes_after_the_parts_already_sent(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    event = text_event("x" * 4000)
    (seq,) = store.accept_transaction("t1", [event])
    store.add_message("Q0", "G", ROOM, "$e", None, "matrix")  # part 1 went out
    relay.incoming.put_nowait((seq, event))
    asyncio.run(_run_loops(relay, lambda: not _pending(store)))
    parts = chunk("Alice: " + "x" * 4000)
    assert [content for _, content, _ in qq.sent] == parts[1:]
    assert _pending(store) == []
    store.close()


def test_event_already_sent_is_not_sent_again(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    (seq,) = store.accept_transaction("t1", [text_event()])
    store.add_message("Q1", "G", ROOM, "$e", None, "matrix")
    relay.incoming.put_nowait((seq, text_event()))
    asyncio.run(_run_loops(relay, lambda: not _pending(store)))
    assert qq.sent == []
    assert _pending(store) == []
    store.close()


def _failing_qq(status):
    from qq_bridge.qq import QQError

    class FailingQQ(FakeQQ):
        async def send_group_text(self, *args, **kwargs):
            raise QQError(status, "failure", status=status)

    return FailingQQ()


def test_outage_keeps_the_event_for_retry(tmp_path):
    relay, store = make_relay(tmp_path, _failing_qq(502))
    (seq,) = store.accept_transaction("t1", [text_event()])
    relay.incoming.put_nowait((seq, text_event()))
    asyncio.run(_run_loops(relay, lambda: relay._keep and not relay._outstanding))
    asyncio.run(_run_loops(relay, lambda: False))
    assert _pending(store) == [seq]
    store.close()


def test_permanent_refusal_clears_the_event(tmp_path):
    relay, store = make_relay(tmp_path, _failing_qq(400))
    (seq,) = store.accept_transaction("t1", [text_event()])
    relay.incoming.put_nowait((seq, text_event()))
    asyncio.run(_run_loops(relay, lambda: not _pending(store)))
    assert _pending(store) == []
    store.close()


def test_long_text_is_finished_after_its_last_part(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    event = text_event("x" * 4000)
    (seq,) = store.accept_transaction("t1", [event])

    async def go():
        relay.incoming.put_nowait((seq, event))
        consumer = asyncio.create_task(relay._consume_matrix())
        for _ in range(10):
            await asyncio.sleep(0)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        assert relay._queue.qsize() > 1
        relay._finish(relay._queue.get_nowait())
        assert _pending(store) == [seq]
        while not relay._queue.empty():
            relay._finish(relay._queue.get_nowait())
        assert _pending(store) == []

    asyncio.run(go())
    store.close()


def test_later_parts_wait_when_an_earlier_part_fails(tmp_path):
    from qq_bridge.qq import QQError

    class FirstFails(FakeQQ):
        async def send_group_text(self, group, content, **kwargs):
            if not self.sent and not getattr(self, "failed", False):
                self.failed = True
                raise QQError(502, "bad gateway", status=502)
            return await super().send_group_text(group, content, **kwargs)

    qq = FirstFails()
    relay, store = make_relay(tmp_path, qq)
    event = text_event("x" * 4000)
    (seq,) = store.accept_transaction("t1", [event])
    relay.incoming.put_nowait((seq, event))
    asyncio.run(_run_loops(relay, lambda: relay._queue.empty() and not relay._outstanding))
    assert qq.sent == [] and _pending(store) == [seq]

    async def retry():
        relay._claim_pending(idle=0)
        await _run_loops(relay, lambda: not _pending(store))

    asyncio.run(retry())
    assert [content for _, content, _ in qq.sent] == chunk("Alice: " + "x" * 4000)
    store.close()


def test_failed_notice_keeps_its_event(tmp_path):
    relay, store = make_relay(tmp_path, _failing_qq(503))
    event = {**text_event(), "content": {"msgtype": "m.image", "body": "a.png"}}
    (seq,) = store.accept_transaction("t1", [event])

    async def go():
        relay._current = seq
        relay._enqueue_media({"kind": "media", "group": "G", "room": ROOM, "event_id": "$e",
                              "sender": "@alice:example.org", "content": event["content"]})
        relay._current = None
        relay._outstanding.setdefault(seq, 1)
        job = relay._queue.get_nowait()
        job["ready"].set_result("not accepted by QQ")
        await relay._send_media(job)
        relay._finish(job)

    asyncio.run(go())
    assert _pending(store) == [seq]
    store.close()


def test_claimed_event_is_not_queued_twice(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    store.accept_transaction("t1", [text_event()])

    async def go():
        relay._claim_pending(idle=0)
        relay._claim_pending(idle=0)

    asyncio.run(go())
    assert relay.incoming.qsize() == 1
    store.close()
