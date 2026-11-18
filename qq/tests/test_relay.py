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
        self.media = []
        self.text_replies = []
        self.media_replies = []
        self.deleted = []
        self.delete_error = None

    async def send_group_text(
        self, group, content, *, reference=None, msg_id=None, msg_seq=1
    ):
        self.sent.append((group, content, reference))
        self.text_replies.append((msg_id, msg_seq))
        return {
            "id": f"Q{len(self.sent)}",
            "ext_info": {"ref_idx": f"REF{len(self.sent)}"},
        }

    async def upload_group_media(self, group, data, filename, file_type):
        return "FILEINFO"

    async def send_group_media(self, group, file_info, *, msg_id=None, msg_seq=1):
        self.media.append((group, file_info))
        self.media_replies.append((msg_id, msg_seq))
        return {"id": "QM"}

    async def delete_group_message(self, group, message_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(message_id)


class FakeResponse:
    headers = {"content-type": "image/png"}

    def __init__(self, status=200):
        self.status = status

    async def read(self):
        return b"image"

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, status=200):
        self.status = status

    def get(self, url, **kwargs):
        return FakeResponse(self.status)


class FakeMatrix:
    session = object()

    def __init__(self):
        self.sent = []
        self.session = FakeSession()

    async def member(self, room_id, user_id):
        return {"displayname": "Alice"}

    async def register_user(self, localpart, displayname):
        return f"@{localpart}:example.org"

    async def set_displayname(self, user_id, displayname):
        return None

    async def send_message(self, room_id, user_id, content):
        self.sent.append((room_id, user_id, content))
        return f"$m{len(self.sent)}"

    async def join(self, room_id, user_id):
        pass

    async def download(self, url, max_bytes=None):
        return b"image"

    async def register_user(self, localpart, displayname):
        return f"@{localpart}:example.org"

    async def set_displayname(self, user_id, displayname):
        pass

    async def set_avatar(self, user_id, avatar_url):
        pass

    async def upload(self, data, content_type, filename, user_id):
        return "mxc://example.org/upload"


def make_relay(tmp_path, qq, rooms=None, *, relay_reactions=True, twitter=True):
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
        relay_reactions=relay_reactions,
        twitter=twitter,
    )
    store = Store(cfg.state_path)
    relay = Relay(cfg, store, FakeMatrix(), qq)
    relay._labels = {ROOM: rooms[0].label}
    if len(rooms) > 1:
        relay._labels[OTHER] = rooms[-1].label
    return relay, store


def text_event(body="hello", sender="@alice:example.org", **extra):
    content = {"msgtype": "m.text", "body": body, **extra}
    return {
        "room_id": ROOM,
        "sender": sender,
        "type": "m.room.message",
        "event_id": "$e",
        "content": content,
    }


def reaction_event(
    target="$orig",
    key="👍",
    *,
    event_id="$react",
    sender="@alice:example.org",
    room_id=ROOM,
    **extra_content,
):
    return {
        "room_id": room_id,
        "sender": sender,
        "type": "m.reaction",
        "event_id": event_id,
        "content": {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target,
                "key": key,
            },
            **extra_content,
        },
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
    assert job["text"] == "[M] [in] Alice: hello"
    assert job["group"] == "G"
    assert job["event_id"] == "$e"
    store.close()


def test_single_room_has_no_label(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    asyncio.run(relay.handle_matrix_event(text_event()))
    assert relay._queue.get_nowait()["text"] == "[M] Alice: hello"
    store.close()


def test_each_network_gets_its_tag(tmp_path):
    relay, store = make_relay(
        tmp_path,
        FakeQQ(),
        rooms=(
            RoomConfig(alias="#in:example.org", label="in"),
            RoomConfig(alias="#two:example.org", label="two"),
        ),
    )
    for sender, tag in (
        ("@discord_123:example.org", "D"),
        ("@telegram_123:example.org", "T"),
        ("@irc_alice:example.org", "I"),
        ("@alice:example.org", "M"),
    ):
        asyncio.run(relay.handle_matrix_event(text_event(sender=sender)))
        assert relay._queue.get_nowait()["text"] == f"[{tag}] [in] Alice: hello"
    store.close()


def test_join_emote_says_which_network(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    asyncio.run(
        relay.handle_matrix_event(
            text_event(
                body="joined the server",
                sender="@discord_9:example.org",
                msgtype="m.emote",
            )
        )
    )
    assert relay._queue.get_nowait()["text"] == "[D] * Alice joined the server"
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
    assert qq.sent[-1][1] == "[M] Alice: [video too long for QQ]"
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
    assert [content for _, content, _ in qq.sent] == ["[M] Alice: hello"]
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
    parts = chunk("[M] Alice: " + "x" * 4000)
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
    assert [content for _, content, _ in qq.sent] == chunk("[M] Alice: " + "x" * 4000)
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


def test_matrix_reaction_becomes_a_quoted_reply(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    asyncio.run(relay.handle_matrix_event(reaction_event(target="$orig", key="👍")))
    job = relay._queue.get_nowait()
    assert job["text"] == "[M] Alice reacted with 👍"
    assert job["reference"] == "REF9"
    assert job["event_id"] == "$react"
    store.close()


def test_matrix_reaction_to_qq_message_uses_qq_reference(tmp_path):
    relay, store = make_relay(
        tmp_path,
        FakeQQ(),
        rooms=(
            RoomConfig(alias="#in:example.org", label="in"),
            RoomConfig(alias="#two:example.org", label="two"),
        ),
    )
    store.add_message("QQ1", "G", ROOM, "$qq_ev", "IDX1", "qq")
    asyncio.run(
        relay.handle_matrix_event(
            reaction_event(
                target="$qq_ev",
                key="❤️",
                sender="@discord_123:example.org",
            )
        )
    )
    job = relay._queue.get_nowait()
    assert job["text"] == "[D] [in] Alice reacted with ❤️"
    assert job["reference"] == "IDX1"
    store.close()


def test_reaction_removal_recalls_the_quoted_reply(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    (seq,) = store.accept_transaction("t1", [reaction_event(target="$orig", key="😂")])
    relay.incoming.put_nowait((seq, reaction_event(target="$orig", key="😂")))
    asyncio.run(_run_loops(relay, lambda: not _pending(store)))
    assert qq.sent == [("G", "[M] Alice reacted with 😂", "REF9")]

    redaction = {
        "room_id": ROOM,
        "sender": "@alice:example.org",
        "type": "m.room.redaction",
        "redacts": "$react",
        "content": {},
    }
    asyncio.run(relay.handle_matrix_event(redaction))
    assert qq.deleted == ["Q1"]
    store.close()


def test_reaction_to_an_unbridged_message_is_skipped(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    asyncio.run(relay.handle_matrix_event(reaction_event(target="$missing", key="👍")))
    assert relay._queue.empty()
    store.close()


def test_ghost_reaction_is_skipped(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    for sender in ("@qqbot:example.org", "@qq_abc:example.org"):
        asyncio.run(
            relay.handle_matrix_event(
                reaction_event(target="$orig", key="👍", sender=sender)
            )
        )
    assert relay._queue.empty()
    store.close()


def test_reaction_disabled_in_config_is_skipped(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ(), relay_reactions=False)
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    asyncio.run(relay.handle_matrix_event(reaction_event(target="$orig", key="👍")))
    assert relay._queue.empty()
    store.close()


def test_custom_emoji_mxc_reaction_uses_fallback(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    asyncio.run(
        relay.handle_matrix_event(
            reaction_event(
                target="$orig",
                key="mxc://example.org/custom",
                sender="@telegram_99:example.org",
            )
        )
    )
    job = relay._queue.get_nowait()
    assert job["text"] == "[T] Alice reacted with [custom emoji]"
    assert job["reference"] == "REF9"
    store.close()


def test_reaction_in_same_burst_as_target_resolves_reference(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    msg = text_event("hello")
    react = reaction_event(target="$e", key="🎉")
    seq1, seq2 = store.accept_transaction("t1", [msg, react])
    relay.incoming.put_nowait((seq1, msg))
    relay.incoming.put_nowait((seq2, react))
    asyncio.run(_run_loops(relay, lambda: not _pending(store)))
    assert qq.sent == [
        ("G", "[M] Alice: hello", None),
        ("G", "[M] Alice reacted with 🎉", "REF1"),
    ]
    assert relay._inflight_events == {}
    store.close()


def test_reaction_in_same_burst_is_skipped_when_target_fails(tmp_path):
    from qq_bridge.qq import QQError

    class FirstFails(FakeQQ):
        async def send_group_text(self, group, content, **kwargs):
            if content == "[M] Alice: hello":
                raise QQError(400, "bad request", status=400)
            return await super().send_group_text(group, content, **kwargs)

    qq = FirstFails()
    relay, store = make_relay(tmp_path, qq)
    msg = text_event("hello")
    react = reaction_event(target="$e", key="🎉")
    seq1, seq2 = store.accept_transaction("t1", [msg, react])
    relay.incoming.put_nowait((seq1, msg))
    relay.incoming.put_nowait((seq2, react))
    asyncio.run(_run_loops(relay, lambda: not _pending(store)))
    assert qq.sent == []
    assert relay._inflight_events == {}
    store.close()


def test_duplicate_reaction_is_not_sent_again(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    store.add_message("Q10", "G", ROOM, "$react", "REF10", "matrix")
    asyncio.run(relay.handle_matrix_event(reaction_event(target="$orig", key="👍")))
    assert relay._queue.empty()
    store.close()


def test_config_loads_relay_reactions(tmp_path):
    from qq_bridge.config import load

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[matrix]
homeserver = "http://hs"
domain = "example.org"
as_token = "as"
hs_token = "hs"
bot_mxid = "@qqbot:example.org"

[qq]
group = "G"
incoming = "#in:example.org"

[[room]]
alias = "#in:example.org"
label = "in"
""",
        encoding="utf-8",
    )
    assert load(cfg_path).relay_reactions is True
    cfg_path.write_text(
        "relay_reactions = false\n" + cfg_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert load(cfg_path).relay_reactions is False


def qq_group_message(content, *, msg_id="Q1", ref_idx=None):
    data = {
        "group_openid": "G",
        "id": msg_id,
        "author": {"member_openid": "M1", "username": "Alice"},
        "content": content,
    }
    if ref_idx:
        data["message_scene"] = {"ext": [f"msg_idx={ref_idx}"]}
    return data


def test_long_qq_text_goes_out_as_several_messages(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    relay._incoming = ROOM
    asyncio.run(relay._on_group_message(qq_group_message("x" * 4500)))

    payloads = [content for _, _, content in relay.matrix.sent]
    assert len(payloads) == 3
    assert "".join(payload["body"] for payload in payloads) == "x" * 4500
    assert all("formatted_body" not in payload for payload in payloads)
    store.close()


def test_only_the_first_part_quotes_the_replied_message(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    relay._incoming = ROOM
    store.add_message("Q0", "G", ROOM, "$parent", "REF1", "qq")
    asyncio.run(relay._on_group_message(qq_group_message("y" * 4500, ref_idx="REF1")))

    payloads = [content for _, _, content in relay.matrix.sent]
    assert payloads[0]["m.relates_to"] == {"m.in_reply_to": {"event_id": "$parent"}}
    assert all("m.relates_to" not in payload for payload in payloads[1:])
    store.close()


def test_a_short_qq_text_keeps_its_markup_and_one_transaction(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    relay._incoming = ROOM
    asyncio.run(relay._on_group_message(qq_group_message("hello <@all>")))

    (payload,) = [content for _, _, content in relay.matrix.sent]
    assert payload["formatted_body"]
    assert payload["body"] == "hello @全体成员"


def sticker_event(**overrides):
    data = {
        "id": "Q1",
        "group_openid": "G",
        "author": {"member_openid": "u1", "username": "渚", "bot": False},
        "content": '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">',
        "attachments": [
            {
                "url": "https://example.org/sticker.png",
                "content_type": "image/png",
                "filename": "sticker.png",
            }
        ],
    }
    data.update(overrides)
    return data


def test_sticker_image_is_relayed_without_the_face_text(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay._incoming = ROOM

    asyncio.run(relay._on_group_message(sticker_event()))

    assert [payload["msgtype"] for _, _, payload in relay.matrix.sent] == ["m.image"]
    assert all("\U0001f632" not in str(payload) for _, _, payload in relay.matrix.sent)
    assert store.event_for_qq("Q1") == "$m1"
    store.close()


def test_sticker_is_not_lost_when_its_image_fails(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay._incoming = ROOM
    relay.matrix.session = FakeSession(status=404)

    asyncio.run(relay._on_group_message(sticker_event()))

    assert [payload["msgtype"] for _, _, payload in relay.matrix.sent] == ["m.text"]
    assert relay.matrix.sent[0][2]["body"] == "[表情]"
    assert store.event_for_qq("Q1") == "$m1"
    store.close()


def test_sticker_reply_keeps_its_reply_relation(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay._incoming = ROOM
    store.add_message("Q0", "G", ROOM, "$parent", "IDX1", "matrix")

    asyncio.run(
        relay._on_group_message(
            sticker_event(message_scene={"ext": ["msg_idx=IDX1"]})
        )
    )

    payload = relay.matrix.sent[0][2]
    assert payload["msgtype"] == "m.image"
    assert payload["m.relates_to"] == {"m.in_reply_to": {"event_id": "$parent"}}
    store.close()


def test_sticker_label_follows_text_when_the_image_fails(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay._incoming = ROOM
    relay.matrix.session = FakeSession(status=404)

    asyncio.run(
        relay._on_group_message(
            sticker_event(
                content='look <faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
            )
        )
    )

    assert [payload["body"] for _, _, payload in relay.matrix.sent] == [
        "look",
        "[表情]",
    ]
    store.close()


def test_sticker_image_does_not_add_a_label_when_it_succeeds(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay._incoming = ROOM

    asyncio.run(
        relay._on_group_message(
            sticker_event(
                content='look <faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
            )
        )
    )

    assert [payload["msgtype"] for _, _, payload in relay.matrix.sent] == [
        "m.text",
        "m.image",
    ]
    store.close()


def test_sticker_label_is_sent_when_another_image_succeeds(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay._incoming = ROOM

    class RoutingSession:
        def get(self, url, **kwargs):
            return FakeResponse(404 if "sticker" in url else 200)

    relay.matrix.session = RoutingSession()
    data = sticker_event()
    data["attachments"].append(
        {
            "url": "https://example.org/photo.png",
            "content_type": "image/png",
            "filename": "photo.png",
        }
    )

    asyncio.run(relay._on_group_message(data))

    assert [(payload["msgtype"], payload["body"]) for _, _, payload in relay.matrix.sent] == [
        ("m.image", "photo.png"),
        ("m.text", "[表情]"),
    ]
    store.close()


def test_fallback_timeout_does_not_duplicate_delivered_text(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay._incoming = ROOM
    relay.matrix.session = FakeSession(status=404)
    original = relay.matrix.send_message
    calls = 0

    async def timeout_on_fallback(room_id, user_id, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError
        return await original(room_id, user_id, content)

    relay.matrix.send_message = timeout_on_fallback

    asyncio.run(
        relay._on_group_message(
            sticker_event(
                content='look <faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
            )
        )
    )

    assert store.event_for_qq("Q1") == "$m1"
    store.close()


def test_long_text_keeps_the_sticker_label_after_an_image_failure(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay._incoming = ROOM
    relay.matrix.session = FakeSession(status=404)

    asyncio.run(
        relay._on_group_message(
            sticker_event(
                content='x' * 4500
                + ' <faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
            )
        )
    )

    payloads = [payload for _, _, payload in relay.matrix.sent]
    assert [payload["msgtype"] for payload in payloads] == [
        "m.text",
        "m.text",
        "m.text",
        "m.text",
    ]
    assert "".join(payload["body"] for payload in payloads[:-1]).replace(" ", "") == "x" * 4500
    assert payloads[-1]["body"] == "[表情]"
    store.close()


def _tweet_post(status="123"):
    from qq_bridge import twitter

    link = twitter.find(f"https://x.com/Alice/status/{status}")[0]
    return twitter.Post(
        handle="Alice",
        name="Alice",
        text="the post",
        created=None,
        link=link,
        media=(),
        quote=None,
        poll=(),
        sensitive=False,
        replying_to=None,
    )


def test_twitter_link_is_rewritten_and_expanded(tmp_path, monkeypatch):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)

    async def fake_fetch(session, link):
        return _tweet_post(link.status)

    monkeypatch.setattr("qq_bridge.relay.twitter.fetch", fake_fetch)
    asyncio.run(
        relay.handle_matrix_event(
            text_event(body="see https://x.com/Alice/status/123")
        )
    )
    text = relay._queue.get_nowait()["text"]
    assert "https://fixupx.com/Alice/status/123" in text
    assert "https://x.com/Alice" not in text

    job = relay._twitter.get_nowait()
    job["event_id"] = None
    asyncio.run(relay._expand_tweet(job))
    assert qq.sent[-1] == ("G", "[Twitter] Alice (@Alice)\n\nthe post\n\nhttps://fixupx.com/Alice/status/123", None)
    store.close()


def test_twitter_expansion_is_not_repeated(tmp_path, monkeypatch):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)

    async def fake_fetch(session, link):
        return _tweet_post(link.status)

    monkeypatch.setattr("qq_bridge.relay.twitter.fetch", fake_fetch)
    asyncio.run(
        relay.handle_matrix_event(
            text_event(body="https://x.com/Alice/status/123")
        )
    )
    job = relay._twitter.get_nowait()
    job["event_id"] = None
    asyncio.run(relay._expand_tweet(job))
    asyncio.run(relay._expand_tweet(job))
    assert len(qq.sent) == 1
    store.close()


def test_twitter_can_be_switched_off(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ(), twitter=False)
    asyncio.run(
        relay.handle_matrix_event(
            text_event(body="see https://x.com/Alice/status/123")
        )
    )
    assert "https://x.com/Alice/status/123" in relay._queue.get_nowait()["text"]
    assert relay._twitter.empty()
    store.close()


def test_twitter_link_from_qq_is_expanded_as_a_reply(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    relay._incoming = ROOM
    asyncio.run(
        relay._on_group_message(
            {
                "group_openid": "G",
                "id": "Q7",
                "author": {"member_openid": "m1", "username": "Bob"},
                "content": "see https://x.com/Alice/status/123",
            }
        )
    )
    job = relay._twitter.get_nowait()
    assert job["reply_id"] == "Q7"
    assert job["link"].status == "123"
    store.close()


def test_config_loads_twitter_toggle(tmp_path):
    from qq_bridge.config import load

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[matrix]
homeserver = "http://hs"
domain = "example.org"
as_token = "as"
hs_token = "hs"
bot_mxid = "@qqbot:example.org"

[qq]
group = "G"
incoming = "#in:example.org"

[[room]]
alias = "#in:example.org"
label = "in"

[twitter]
enabled = false
""",
        encoding="utf-8",
    )
    assert load(cfg_path).twitter is False


def _photo_post():
    from qq_bridge import twitter

    link = twitter.find("https://x.com/Alice/status/123")[0]
    return twitter.Post(
        handle="Alice",
        name="Alice",
        text="the post",
        created=None,
        link=link,
        media=(
            twitter.Media(
                "photo", "https://pbs.twimg.com/media/a.png?name=orig", None
            ),
        ),
        quote=None,
        poll=(),
        sensitive=False,
        replying_to=None,
    )


def test_twitter_expansion_uploads_its_pictures(tmp_path, monkeypatch):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    async def fake_download(session, url, max_bytes=0):
        assert "name=large" in url
        return png, "image/png"

    monkeypatch.setattr("qq_bridge.relay.twitter.download", fake_download)
    asyncio.run(relay._send_tweet_post(_photo_post(), "G", "Q7"))
    assert qq.sent[0][1].startswith("[Twitter] Alice (@Alice)")
    assert qq.text_replies == [("Q7", 1)]
    assert qq.media == [("G", "FILEINFO")]
    assert qq.media_replies == [("Q7", 2)]
    store.close()


def test_sensitive_twitter_post_skips_its_pictures(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    post = _photo_post()
    post = post.__class__(**{**post.__dict__, "sensitive": True})
    asyncio.run(relay._send_tweet_post(post, "G", None))
    assert "[sensitive]" in qq.sent[0][1]
    assert qq.media == []
    store.close()


def test_twitter_expansion_is_counted_against_the_event(tmp_path, monkeypatch):
    relay, store = make_relay(tmp_path, FakeQQ())

    async def fake_fetch(session, link):
        return _tweet_post(link.status)

    monkeypatch.setattr("qq_bridge.relay.twitter.fetch", fake_fetch)
    relay._current = 1
    relay._outstanding[1] = 0
    asyncio.run(
        relay.handle_matrix_event(
            text_event(body="https://x.com/Alice/status/123")
        )
    )
    assert relay._outstanding[1] == 2
    store.close()


def test_twitter_marker_survives_a_store_reopen(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    store.mark_tweet("G", "123")
    store.close()
    reopened = Store(tmp_path / "state.sqlite3")
    assert reopened.tweet_recent("G", "123", 600) is True
    assert reopened.tweet_recent("G", "456", 600) is False
    reopened.close()


def test_twitter_send_failure_keeps_the_event_for_retry(tmp_path):
    from qq_bridge.qq import QQError

    class FailingQQ(FakeQQ):
        async def send_group_text(
            self, group, content, *, reference=None, msg_id=None, msg_seq=1
        ):
            raise QQError(500, "down", status=500)

    relay, store = make_relay(tmp_path, FailingQQ())
    assert asyncio.run(relay._send_tweet_post(_tweet_post(), "G", None)) is False
    store.close()


def test_config_rejects_a_non_boolean_twitter_toggle(tmp_path):
    from qq_bridge.config import ConfigError, load

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[matrix]
homeserver = "http://hs"
domain = "example.org"
as_token = "as"
hs_token = "hs"
bot_mxid = "@qqbot:example.org"

[qq]
group = "G"
incoming = "#in:example.org"

[[room]]
alias = "#in:example.org"
label = "in"

[twitter]
enabled = "false"
""",
        encoding="utf-8",
    )
    try:
        load(cfg_path)
    except ConfigError as exc:
        assert "twitter.enabled" in str(exc)
    else:
        raise AssertionError("a string twitter.enabled was accepted")


def test_qq_twitter_expansion_is_a_passive_reply(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    asyncio.run(relay._send_tweet_post(_tweet_post(), "G", "Q7"))
    assert qq.text_replies == [("Q7", 1)]
    store.close()


def test_matrix_twitter_expansion_is_not_a_passive_reply(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    asyncio.run(relay._send_tweet_post(_tweet_post(), "G", None))
    assert qq.text_replies == [(None, 1)]
    store.close()


def test_qq_passive_reply_stops_at_five_parts(tmp_path, monkeypatch):
    from qq_bridge import twitter

    class ManyPicturesQQ(FakeQQ):
        pass

    qq = ManyPicturesQQ()
    relay, store = make_relay(tmp_path, qq)
    link = twitter.find("https://x.com/Alice/status/123")[0]
    post = twitter.Post(
        handle="Alice",
        name="Alice",
        text="x" * 4000,
        created=None,
        link=link,
        media=tuple(
            twitter.Media("photo", f"https://pbs.twimg.com/media/{n}.jpg", None)
            for n in range(4)
        ),
        quote=None,
        poll=(),
        sensitive=False,
        replying_to=None,
    )

    async def fake_download(session, url, max_bytes=0):
        return b"image", "image/jpeg"

    monkeypatch.setattr("qq_bridge.relay.twitter.download", fake_download)
    asyncio.run(relay._send_tweet_post(post, "G", "Q7"))
    assert [seq for _, seq in qq.text_replies] == [1, 2, 3]
    assert [seq for _, seq in qq.media_replies] == [4, 5]
    store.close()
