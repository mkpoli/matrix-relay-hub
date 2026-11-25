import asyncio

from qq_bridge.config import Config, MatrixConfig, QQConfig, RoomConfig
from qq_bridge.formatting import chunk
from qq_bridge.qq import QQError
from qq_bridge.relay import Relay
from qq_bridge.store import Store

ROOM = "!room:example.org"
OTHER = "!other:example.org"


class FakeQQ:
    def __init__(self):
        self.sent = []
        self.media = []
        self.uploads = []
        self.media_contents = []
        self.media_references = []
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
        self.uploads.append((group, filename, file_type))
        return "FILEINFO"

    async def send_group_media(
        self, group, file_info, *, content=None, reference=None, msg_id=None, msg_seq=1
    ):
        self.media.append((group, file_info))
        self.media_contents.append(content)
        self.media_references.append(reference)
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
        self.authors = dict(self.authors)
        self.names = {}

    authors = {"$orig": "@alice:example.org", "$pic": "@alice:example.org", "$long": "@alice:example.org"}

    async def event(self, room_id, event_id, user_id=None):
        author = self.authors.get(event_id)
        return {"event_id": event_id, "sender": author} if author else None

    async def member(self, room_id, user_id):
        return {"displayname": self.names.get(user_id, "Alice")}

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


def test_a_message_that_is_all_quote_reaches_qq(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    body = "> 자음\n> \n> ㄱ  1획\n> ㅋ  2획  *ㄱ에 1획을 더한 글자\n"
    asyncio.run(relay.handle_matrix_event(text_event(body, format="org.matrix.custom.html",
                                                     formatted_body="<blockquote><p>자음</p></blockquote>")))
    assert relay._queue.get_nowait()["text"].startswith("[M] Alice: > 자음")
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


def edit_event(body="fixed", target="$orig", msgtype="m.text", event_id="$edit"):
    event = text_event(
        body=f"* {body}",
        **{
            "m.new_content": {"body": body, "msgtype": msgtype},
            "m.relates_to": {"rel_type": "m.replace", "event_id": target},
        },
    )
    event["event_id"] = event_id
    return event


def run_edit(relay):
    job = relay._queue.get_nowait()
    assert job["kind"] == "edit"
    asyncio.run(relay._send_edit(job))
    return job


def test_an_edit_recalls_the_qq_copy_and_sends_the_new_text(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q1", "G", ROOM, "$orig", "REF1", "matrix")
    store.add_message("Q2", "G", ROOM, "$orig", "REF2", "matrix")
    asyncio.run(relay.handle_matrix_event(edit_event()))
    run_edit(relay)
    assert qq.deleted == ["Q1", "Q2"]
    assert qq.sent[-1][1] == "[M] Alice: fixed"
    assert [row["qq_id"] for row in store.qq_messages_for_event("$orig")] == ["Q1"]
    asyncio.run(relay.handle_matrix_event(edit_event()))
    assert relay._queue.empty(), "a redelivered edit is not sent twice"
    asyncio.run(relay.handle_matrix_event(text_event("old text") | {"event_id": "$orig"}))
    assert relay._queue.empty(), "the original's old text is never sent again"
    store.close()


def test_a_second_edit_replaces_the_first(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q0", "G", ROOM, "$orig", "REF0", "matrix")
    asyncio.run(relay.handle_matrix_event(edit_event("v1")))
    asyncio.run(relay.handle_matrix_event(edit_event("v2", event_id="$edit2")))
    run_edit(relay)
    run_edit(relay)
    assert qq.deleted == ["Q0", "Q1"] and qq.sent[-1][1] == "[M] Alice: v2"
    store.close()


def test_an_edit_qq_will_not_recall_is_marked(tmp_path):
    qq = FakeQQ()
    qq.delete_error = QQError(40054005, "too old")
    relay, store = make_relay(tmp_path, qq)
    store.add_message("QOLD", "G", ROOM, "$orig", "REFOLD", "matrix")
    asyncio.run(relay.handle_matrix_event(edit_event()))
    run_edit(relay)
    assert qq.sent[-1][1] == "[M] Alice ✎: fixed"
    assert {row["qq_id"] for row in store.qq_messages_for_event("$orig")} == {"QOLD", "Q1"}, "the old copy is still on QQ"
    store.close()


def test_a_recall_that_may_pass_later_keeps_the_edit(tmp_path):
    qq = FakeQQ()
    qq.delete_error = QQError(429, "slow down", status=429)
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q1", "G", ROOM, "$orig", "REF1", "matrix")
    asyncio.run(relay.handle_matrix_event(edit_event()))
    job = run_edit(relay)
    assert qq.sent == [] and not store.edit_done("$edit")
    store.close()


def test_an_edit_by_someone_else_is_ignored(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q1", "G", ROOM, "$orig", "REF1", "matrix")
    event = edit_event()
    event["sender"] = "@mallory:example.org"
    asyncio.run(relay.handle_matrix_event(event))
    assert relay._queue.empty() and qq.deleted == []
    store.close()


def test_an_edit_of_something_qq_never_got_is_ignored(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    asyncio.run(relay.handle_matrix_event(edit_event()))
    store.add_message("Q1", "G", ROOM, "$pic", "REF1", "matrix")
    asyncio.run(relay.handle_matrix_event(edit_event(target="$pic", msgtype="m.image", event_id="$e2")))
    assert relay._queue.empty() and qq.deleted == []
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


def test_matrix_sticker_is_sent_as_an_image(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    event = {
        "room_id": ROOM,
        "sender": "@alice:example.org",
        "type": "m.sticker",
        "event_id": "$sticker",
        "content": {
            "body": "开心",
            "url": "mxc://example.org/sticker",
            "info": {"mimetype": "image/png", "w": 128, "h": 128},
        },
    }
    asyncio.run(relay.handle_matrix_event(event))
    job = relay._queue.get_nowait()
    assert job["kind"] == "media"
    assert relay._images.get_nowait() is job
    assert job["content"]["msgtype"] == "m.image"
    assert job["content"]["url"] == "mxc://example.org/sticker"
    store.close()


def test_video_sticker_takes_the_video_lane(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    event = {
        "room_id": ROOM,
        "sender": "@telegram_99:example.org",
        "type": "m.sticker",
        "event_id": "$vsticker",
        "content": {
            "body": "wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "video/webm"},
        },
    }
    asyncio.run(relay.handle_matrix_event(event))
    job = relay._queue.get_nowait()
    assert relay._videos.get_nowait() is job
    assert job["content"]["msgtype"] == "m.video"
    store.close()


def test_sticker_without_url_becomes_a_label(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    event = {
        "room_id": ROOM,
        "sender": "@discord_9:example.org",
        "type": "m.sticker",
        "event_id": "$lottie_missing",
        "content": {
            "body": "Wave",
            "info": {"mimetype": "application/json", "w": 160, "h": 160},
        },
    }
    asyncio.run(relay.handle_matrix_event(event))
    job = relay._queue.get_nowait()
    assert job["kind"] == "text"
    assert job["text"] == "[D] Alice: [sticker: Wave]"
    assert relay._images.empty() and relay._videos.empty()
    store.close()


def test_lottie_json_sticker_becomes_a_label(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    event = {
        "room_id": ROOM,
        "sender": "@discord_9:example.org",
        "type": "m.sticker",
        "event_id": "$lottie_json",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave.json",
            "info": {"mimetype": "application/json", "w": 160, "h": 160},
        },
    }
    asyncio.run(relay.handle_matrix_event(event))
    job = relay._queue.get_nowait()
    assert job["kind"] == "text"
    assert job["text"] == "[D] Alice: [sticker: Wave]"
    assert relay._images.empty() and relay._videos.empty()
    store.close()


def test_unreadable_sticker_falls_back_to_its_name(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)

    async def go():
        relay._enqueue_media(
            {
                "kind": "media",
                "group": "G",
                "room": ROOM,
                "event_id": "$bad_sticker",
                "sender": "@discord_9:example.org",
                "content": {"msgtype": "m.image", "body": "Wave", "url": "mxc://x/y"},
                "sticker": True,
            }
        )
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result("not accepted by QQ")
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.sent[-1][1] == "[D] Alice: [sticker: Wave]"
    store.close()


def test_long_sticker_fallback_label_is_bounded(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)

    async def go():
        relay._enqueue_media(
            {
                "kind": "media",
                "group": "G",
                "room": ROOM,
                "event_id": "$long_sticker",
                "sender": "@discord_9:example.org",
                "content": {"msgtype": "m.image", "body": "x" * 3000, "url": "mxc://x/y"},
                "sticker": True,
            }
        )
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result("not accepted by QQ")
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.sent[-1][1] == f"[D] Alice: [sticker: {'x' * 200}...]"
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


def test_redaction_recalls_every_part_of_a_long_text(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    for part in ("Q1", "Q2", "Q3"):
        store.add_message(part, "G", ROOM, "$long", None, "matrix")
    event = {"room_id": ROOM, "sender": "@alice:example.org", "type": "m.room.redaction",
             "redacts": "$long", "content": {}}
    asyncio.run(relay.handle_matrix_event(event))
    assert qq.deleted == ["Q1", "Q2", "Q3"]
    asyncio.run(relay.handle_matrix_event(text_event("redacted text") | {"event_id": "$long"}))
    assert relay._queue.empty(), "a retry of the redacted original sends nothing"
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


def test_media_carries_its_sender_in_content(tmp_path):
    from qq_bridge.fit import QQ_AUDIO, QQ_IMAGE, Prepared

    qq = FakeQQ()
    relay, store = make_relay(
        tmp_path,
        qq,
        rooms=(
            RoomConfig(alias="#in:example.org", label="in"),
            RoomConfig(alias="#two:example.org", label="two"),
        ),
    )

    async def go():
        for sender, prepared in (
            ("@discord_9:example.org", Prepared(b"img", "a.png", QQ_IMAGE)),
            ("@alice:example.org", Prepared(b"aud", "a.silk", QQ_AUDIO)),
        ):
            relay._enqueue_media(
                {
                    "kind": "media",
                    "group": "G",
                    "room": ROOM,
                    "event_id": "$m",
                    "sender": sender,
                    "content": {"msgtype": "m.image", "url": "mxc://x/y"},
                }
            )
            job = relay._queue.get_nowait()
            relay._images.get_nowait()
            job["ready"].set_result(prepared)
            await relay._send_media(job)

    asyncio.run(go())
    assert qq.media_contents == ["[D] [in] Alice:", None]
    store.close()


def test_media_retries_without_caption_when_qq_refuses_it(tmp_path):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    class CaptionRefusingQQ(FakeQQ):
        async def send_group_media(
            self, group, file_info, *, content=None, reference=None, msg_id=None, msg_seq=1
        ):
            if content:
                raise QQError(304003, "URL not allowed", status=400)
            return await super().send_group_media(
                group,
                file_info,
                content=content,
                reference=reference,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

    qq = CaptionRefusingQQ()
    relay, store = make_relay(tmp_path, qq)
    _run_media(
        relay,
        {"msgtype": "m.image", "url": "mxc://x/y"},
        Prepared(b"img", "a.png", QQ_IMAGE),
    )
    assert qq.media == [("G", "FILEINFO")]
    assert qq.media_contents == [None]
    assert store.qq_for_event("$img")["qq_id"] == "QM"
    store.close()


def test_captionless_retry_still_reencodes_on_format_refusal(tmp_path):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    class CaptionThenFormatRefusingQQ(FakeQQ):
        async def send_group_media(
            self, group, file_info, *, content=None, reference=None, msg_id=None, msg_seq=1
        ):
            if content:
                raise QQError(304003, "URL not allowed", status=400)
            raise QQError(40034002, "富媒体文件格式不支持", status=400)

    relay, store = make_relay(tmp_path, CaptionThenFormatRefusingQQ())

    async def go():
        relay._enqueue_media(
            {
                "kind": "media",
                "group": "G",
                "room": ROOM,
                "event_id": "$img",
                "sender": "@alice:example.org",
                "content": {"msgtype": "m.image", "url": "mxc://x/y"},
            }
        )
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"img", "a.webp", QQ_IMAGE))
        await relay._send_media(job)
        retry = relay._queue.get_nowait()
        assert retry["force"] is True
        assert relay._images.get_nowait() is retry

    asyncio.run(go())
    store.close()


def test_welcome_sticker_reply_names_greeted_user_and_quotes_join(tmp_path):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    qq = FakeQQ()
    relay, store = make_relay(
        tmp_path,
        qq,
        rooms=(
            RoomConfig(alias="#welcome:example.org", label="歡迎"),
            RoomConfig(alias="#two:example.org", label="two"),
        ),
    )
    relay.matrix.authors["$join"] = "@discord_2:example.org"
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "ItMarki",
    }
    store.add_message("QJ", "G", ROOM, "$join", "REFJOIN", "matrix")

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$wave",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"gif", "wave.gif", QQ_IMAGE))
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.media_contents == ["[D] [歡迎] mkpoli → ItMarki:"]
    assert qq.media_references == ["REFJOIN"]
    store.close()


def test_sticker_reply_still_names_target_without_stored_qq_reference(tmp_path):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.authors["$join"] = "@discord_2:example.org"
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "ItMarki",
    }

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$wave",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"gif", "wave.gif", QQ_IMAGE))
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.media_contents == ["[D] mkpoli → ItMarki:"]
    assert qq.media_references == [None]
    store.close()


def test_sticker_reply_resolves_reference_after_target_is_sent(tmp_path):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.authors["$join"] = "@discord_2:example.org"
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "ItMarki",
    }

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$wave",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        # Simulate the preceding join message finishing delivery before the sticker sends.
        store.add_message("QJ", "G", ROOM, "$join", "REFJOIN", "matrix")
        job["ready"].set_result(Prepared(b"gif", "wave.gif", QQ_IMAGE))
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.media_contents == ["[D] mkpoli → ItMarki:"]
    assert qq.media_references == ["REFJOIN"]
    store.close()


def test_sticker_falls_back_to_single_mention_when_reply_target_unavailable(tmp_path):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "ItMarki",
    }

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$wave",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.mentions": {"user_ids": ["@discord_2:example.org"]},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"gif", "wave.gif", QQ_IMAGE))
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.media_contents == ["[D] mkpoli → ItMarki:"]
    assert qq.media_references == [None]
    store.close()


def test_media_self_reply_keeps_reference_without_duplicating_sender_name(tmp_path):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.authors["$self"] = "@discord_1:example.org"
    relay.matrix.names = {"@discord_1:example.org": "mkpoli"}
    store.add_message("QS", "G", ROOM, "$self", "REFSELF", "matrix")

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$wave_self",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$self"}},
            "m.mentions": {"user_ids": ["@discord_2:example.org"]},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"gif", "wave.gif", QQ_IMAGE))
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.media_contents == ["[D] mkpoli:"]
    assert qq.media_references == ["REFSELF"]
    store.close()


def test_sticker_text_and_unfit_fallbacks_preserve_reply_target_and_reference(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.authors["$join"] = "@discord_2:example.org"
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "ItMarki",
    }
    store.add_message("QJ", "G", ROOM, "$join", "REFJOIN", "matrix")

    unconverted = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$lottie",
        "content": {
            "body": "Wave",
            "info": {"mimetype": "application/json"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }
    asyncio.run(relay.handle_matrix_event(unconverted))
    job = relay._queue.get_nowait()
    assert job["text"] == "[D] mkpoli → ItMarki: [sticker: Wave]"
    assert job["reference"] == "REFJOIN"

    unfit = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$unfit",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }

    async def go():
        await relay.handle_matrix_event(unfit)
        media_job = relay._queue.get_nowait()
        relay._images.get_nowait()
        media_job["ready"].set_result("not accepted by QQ")
        await relay._send_media(media_job)

    asyncio.run(go())
    assert qq.sent[-1] == ("G", "[D] mkpoli → ItMarki: [sticker: Wave]", "REFJOIN")
    store.close()


def test_media_retries_without_reference_keeping_caption_when_qq_refuses_reference(
    tmp_path,
):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    class ReferenceRefusingQQ(FakeQQ):
        def __init__(self):
            super().__init__()
            self.attempts = []

        async def send_group_media(
            self, group, file_info, *, content=None, reference=None, msg_id=None, msg_seq=1
        ):
            self.attempts.append((content, reference))
            if reference:
                raise QQError(400, "message_reference not supported", status=400)
            return await super().send_group_media(
                group,
                file_info,
                content=content,
                reference=reference,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

    qq = ReferenceRefusingQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.authors["$join"] = "@discord_2:example.org"
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "ItMarki",
    }
    store.add_message("QJ", "G", ROOM, "$join", "REFJOIN", "matrix")

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$wave",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"gif", "wave.gif", QQ_IMAGE))
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.attempts == [
        ("[D] mkpoli → ItMarki:", "REFJOIN"),
        ("[D] mkpoli → ItMarki:", None),
    ]
    assert qq.media_contents == ["[D] mkpoli → ItMarki:"]
    assert qq.media_references == [None]
    store.close()


def test_audio_reply_passes_reference_without_caption_and_retries_if_refused(tmp_path):
    from qq_bridge.fit import QQ_AUDIO, Prepared

    class ReferenceRefusingQQ(FakeQQ):
        def __init__(self):
            super().__init__()
            self.attempts = []

        async def send_group_media(
            self, group, file_info, *, content=None, reference=None, msg_id=None, msg_seq=1
        ):
            self.attempts.append((content, reference))
            if reference:
                raise QQError(400, "message_reference not supported", status=400)
            return await super().send_group_media(
                group,
                file_info,
                content=content,
                reference=reference,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

    qq = ReferenceRefusingQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.authors["$join"] = "@discord_2:example.org"
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "ItMarki",
    }
    store.add_message("QJ", "G", ROOM, "$join", "REFJOIN", "matrix")

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.room.message",
        "event_id": "$aud",
        "content": {
            "msgtype": "m.audio",
            "body": "voice.ogg",
            "url": "mxc://example.org/voice",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"aud", "voice.silk", QQ_AUDIO))
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.attempts == [(None, "REFJOIN"), (None, None)]
    assert qq.media_contents == [None]
    assert qq.media_references == [None]
    store.close()


def test_media_reply_to_redacted_event_omits_reference(tmp_path):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.authors["$join"] = "@discord_2:example.org"
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "ItMarki",
    }
    store.add_message("QJ", "G", ROOM, "$join", "REFJOIN", "matrix")
    store.supersede("$join", "redacted")

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$wave",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"gif", "wave.gif", QQ_IMAGE))
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.media_contents == ["[D] mkpoli → ItMarki:"]
    assert qq.media_references == [None]
    store.close()


def test_media_caption_refusal_keeps_valid_reference(tmp_path):
    from qq_bridge.fit import QQ_IMAGE, Prepared

    class CaptionRefusingQQ(FakeQQ):
        async def send_group_media(
            self, group, file_info, *, content=None, reference=None, msg_id=None, msg_seq=1
        ):
            if content:
                raise QQError(304003, "URL not allowed", status=400)
            return await super().send_group_media(
                group,
                file_info,
                content=content,
                reference=reference,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

    qq = CaptionRefusingQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.authors["$join"] = "@discord_2:example.org"
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "foo.example",
    }
    store.add_message("QJ", "G", ROOM, "$join", "REFJOIN", "matrix")

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$wave",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result(Prepared(b"gif", "wave.gif", QQ_IMAGE))
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.media_contents == [None]
    assert qq.media_references == ["REFJOIN"]
    store.close()


def test_media_fallback_notice_retries_unquoted_when_reference_is_refused(tmp_path):
    class ReferenceRefusingTextQQ(FakeQQ):
        async def send_group_text(
            self, group, content, *, reference=None, msg_id=None, msg_seq=1
        ):
            if reference:
                raise QQError(400, "invalid reference", status=400)
            return await super().send_group_text(
                group, content, reference=reference, msg_id=msg_id, msg_seq=msg_seq
            )

    qq = ReferenceRefusingTextQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.authors["$join"] = "@discord_2:example.org"
    relay.matrix.names = {
        "@discord_1:example.org": "mkpoli",
        "@discord_2:example.org": "ItMarki",
    }
    store.add_message("QJ", "G", ROOM, "$join", "REFJOIN", "matrix")

    event = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.sticker",
        "event_id": "$unfit_ref",
        "content": {
            "body": "Wave",
            "url": "mxc://example.org/wave",
            "info": {"mimetype": "image/webp"},
            "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
        },
    }

    async def go():
        await relay.handle_matrix_event(event)
        job = relay._queue.get_nowait()
        relay._images.get_nowait()
        job["ready"].set_result("not accepted by QQ")
        await relay._send_media(job)

    asyncio.run(go())
    assert qq.sent == [("G", "[D] mkpoli → ItMarki: [sticker: Wave]", None)]
    store.close()


def test_reply_target_member_network_or_timeout_error_does_not_drop_media(tmp_path):
    import aiohttp
    from qq_bridge.fit import QQ_IMAGE, Prepared

    for idx, failure in enumerate(
        (aiohttp.ClientConnectionError("reset"), asyncio.TimeoutError())
    ):
        qq = FakeQQ()
        relay, store = make_relay(tmp_path / f"case_{idx}", qq)
        relay.matrix.authors["$join"] = "@discord_2:example.org"

        async def flaky_member(room_id, user_id, exc=failure):
            if user_id == "@discord_2:example.org":
                raise exc
            return {"displayname": "mkpoli"}

        relay.matrix.member = flaky_member
        store.add_message("QJ", "G", ROOM, "$join", "REFJOIN", "matrix")

        event = {
            "room_id": ROOM,
            "sender": "@discord_1:example.org",
            "type": "m.sticker",
            "event_id": f"$wave_flaky_{idx}",
            "content": {
                "body": "Wave",
                "url": "mxc://example.org/wave",
                "info": {"mimetype": "image/webp"},
                "m.relates_to": {"m.in_reply_to": {"event_id": "$join"}},
            },
        }

        async def go():
            await relay.handle_matrix_event(event)
            job = relay._queue.get_nowait()
            relay._images.get_nowait()
            job["ready"].set_result(Prepared(b"gif", "wave.gif", QQ_IMAGE))
            await relay._send_media(job)

        asyncio.run(go())
        assert qq.media_contents == ["[D] mkpoli:"]
        assert qq.media_references == ["REFJOIN"]
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
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
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
    assert job["kind"] == "media"
    assert job["caption"] == "[T] Alice reacted with [custom emoji]"
    assert job["fallback_text"] == "[T] Alice reacted with [custom emoji]"
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


def qq_group_message(content, *, msg_id="Q1", ref_idx=None, own_idx=None):
    data = {
        "group_openid": "G",
        "id": msg_id,
        "author": {"member_openid": "M1", "username": "Alice"},
        "content": content,
    }
    ext = [f"msg_idx={own_idx}"] if own_idx else []
    if ref_idx:
        ext.append(f"ref_msg_idx={ref_idx}")
    if ext:
        data["message_scene"] = {"ext": ext}
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


def test_a_qq_message_is_not_a_reply_to_itself_or_an_older_index(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    relay._incoming = ROOM
    store.add_message("Q0", "G", ROOM, "$parent", "REF1", "qq")
    asyncio.run(relay._on_group_message(qq_group_message("hi", own_idx="REF1")))

    (payload,) = [content for _, _, content in relay.matrix.sent]
    assert "m.relates_to" not in payload
    store.close()


def test_a_quote_message_replies_to_its_first_element(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    relay._incoming = ROOM
    store.add_message("Q0", "G", ROOM, "$parent", "REF1", "matrix")
    data = qq_group_message("translate this", own_idx="REF2")
    data["message_type"] = 103
    data["msg_elements"] = [{"msg_idx": "REF1", "content": "大肥魚: hello"}]
    asyncio.run(relay._on_group_message(data))

    (payload,) = [content for _, _, content in relay.matrix.sent]
    assert payload["m.relates_to"] == {"m.in_reply_to": {"event_id": "$parent"}}
    assert store.event_for_ref("G", "REF2")
    store.close()


def test_a_quote_message_falls_back_to_the_scene_reference(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    relay._incoming = ROOM
    store.add_message("Q0", "G", ROOM, "$parent", "REF1", "matrix")
    data = qq_group_message("hi", own_idx="REF3", ref_idx=" REF1 ")
    data["message_type"] = 103
    data["msg_elements"] = [{"msg_idx": "UNKNOWN"}]
    asyncio.run(relay._on_group_message(data))

    (payload,) = [content for _, _, content in relay.matrix.sent]
    assert payload["m.relates_to"] == {"m.in_reply_to": {"event_id": "$parent"}}
    store.close()


def test_a_quote_message_prefers_its_element_and_tolerates_empty_fields(tmp_path):
    relay, store = make_relay(tmp_path, FakeQQ())
    relay._incoming = ROOM
    store.add_message("Q0", "G", ROOM, "$element", "REF1", "matrix")
    store.add_message("Q9", "G", ROOM, "$scene", "REF9", "matrix")
    data = qq_group_message("hi", own_idx="REF3", ref_idx="REF9")
    data["message_type"] = 103
    data["msg_elements"] = [{"msg_idx": "REF1"}]
    asyncio.run(relay._on_group_message(data))
    empty = qq_group_message("hey", msg_id="Q2", own_idx="REF4")
    empty["message_scene"]["ext"].append("ref_msg_idx=")
    empty["message_type"] = 103
    asyncio.run(relay._on_group_message(empty))

    first, second = [content for _, _, content in relay.matrix.sent]
    assert first["m.relates_to"] == {"m.in_reply_to": {"event_id": "$element"}}
    assert "m.relates_to" not in second
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
            sticker_event(message_scene={"ext": ["msg_idx=IDX2", "ref_msg_idx=IDX1"]})
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

    async def fake_download(session, attachment, max_bytes=None):
        assert "name=large" in attachment.url
        return png, "image/png"

    monkeypatch.setattr("qq_bridge.relay.twitter.download", fake_download)
    asyncio.run(relay._send_tweet_post(_photo_post(), "G", "Q7"))
    assert qq.sent[0][1].startswith("[Twitter] Alice (@Alice)")
    assert qq.text_replies == [("Q7", 1)]
    assert qq.media == [("G", "FILEINFO")]
    assert qq.uploads == [("G", "tweet.png", 1)]
    assert qq.media_replies == [("Q7", 2)]
    store.close()


def _video_post(kind="video"):
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
                kind,
                "https://video.twimg.com/x.mp4",
                "https://pbs.twimg.com/thumb/1.jpg",
            ),
        ),
        quote=None,
        poll=(),
        sensitive=False,
        replying_to=None,
    )


def test_twitter_expansion_uploads_a_video(tmp_path, monkeypatch):
    from qq_bridge import twitter

    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"video"

    async def fake_download(session, attachment, max_bytes=None):
        assert attachment == twitter.Attachment(
            "video", "https://video.twimg.com/x.mp4"
        )
        return mp4, "video/mp4"

    monkeypatch.setattr("qq_bridge.relay.twitter.download", fake_download)
    asyncio.run(relay._send_tweet_post(_video_post("gif"), "G", "Q7"))
    assert qq.text_replies == [("Q7", 1)]
    assert qq.uploads == [("G", "tweet.mp4", 2)]
    assert qq.media_replies == [("Q7", 2)]
    store.close()


def test_sensitive_twitter_post_skips_its_media(tmp_path):
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

    async def fake_download(session, attachment, max_bytes=None):
        return b"image", "image/jpeg"

    monkeypatch.setattr("qq_bridge.relay.twitter.download", fake_download)
    asyncio.run(relay._send_tweet_post(post, "G", "Q7"))
    assert [seq for _, seq in qq.text_replies] == [1, 2, 3]
    assert [seq for _, seq in qq.media_replies] == [4, 5]
    store.close()


def test_an_edit_of_a_text_still_queued_replaces_it(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    asyncio.run(relay.handle_matrix_event(text_event("typo") | {"event_id": "$orig"}))
    asyncio.run(relay.handle_matrix_event(edit_event()))
    original = relay._queue.get_nowait()
    asyncio.run(relay._send_text(original))
    relay._finish(original)
    run_edit(relay)
    assert [text for _, text, _ in qq.sent] == ["[M] Alice: fixed"]
    store.close()


def test_an_edit_of_a_recent_text_that_has_not_reached_qq_replaces_it(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    relay.matrix.event = lambda room_id, event_id, user_id=None: _async(
        {"event_id": event_id, "sender": "@alice:example.org", "origin_server_ts": 1_000})
    edit = edit_event() | {"origin_server_ts": 61_000}
    asyncio.run(relay.handle_matrix_event(edit))
    run_edit(relay)
    asyncio.run(relay.handle_matrix_event(text_event("typo") | {"event_id": "$orig"}))
    assert [text for _, text, _ in qq.sent] == ["[M] Alice: fixed"] and relay._queue.empty()
    store.close()


def test_an_older_edit_retried_late_does_not_undo_a_newer_one(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q0", "G", ROOM, "$orig", "REF0", "matrix")
    asyncio.run(relay.handle_matrix_event(edit_event("v2", event_id="$e2") | {"origin_server_ts": 20}))
    run_edit(relay)
    asyncio.run(relay.handle_matrix_event(edit_event("v1", event_id="$e1") | {"origin_server_ts": 10}))
    run_edit(relay)
    assert [text for _, text, _ in qq.sent] == ["[M] Alice: v2"]
    store.close()


def test_a_redaction_while_an_edit_waits_sends_nothing(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q0", "G", ROOM, "$orig", "REF0", "matrix")
    asyncio.run(relay.handle_matrix_event(edit_event("secret v2")))
    asyncio.run(relay.handle_matrix_event({"room_id": ROOM, "sender": "@alice:example.org",
                                           "type": "m.room.redaction", "redacts": "$orig", "content": {}}))
    run_edit(relay)
    assert qq.sent == [] and qq.deleted == ["Q0"]
    store.close()


def test_an_edit_is_kept_when_the_homeserver_cannot_be_asked(tmp_path):
    from qq_bridge.matrix import MatrixError

    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q0", "G", ROOM, "$orig", "REF0", "matrix")

    async def down(room_id, event_id, user_id=None):
        raise MatrixError(502, "bad gateway")

    relay.matrix.event = down
    kept = []
    relay._retry_later = lambda job=None: kept.append(job)
    asyncio.run(relay.handle_matrix_event(edit_event()))
    assert kept == [None] and relay._queue.empty()
    store.close()


def test_a_redaction_whose_recall_may_pass_later_is_kept(tmp_path):
    qq = FakeQQ()
    qq.delete_error = QQError(500, "busy", status=503)
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q0", "G", ROOM, "$orig", "REF0", "matrix")
    kept = []
    relay._retry_later = lambda job=None: kept.append(job)
    asyncio.run(relay.handle_matrix_event({"room_id": ROOM, "sender": "@alice:example.org",
                                           "type": "m.room.redaction", "redacts": "$orig", "content": {}}))
    assert kept == [None] and store.qq_messages_for_event("$orig")
    store.close()


async def _async(value):
    return value


async def _run_media_loops(relay, until):
    tasks = [
        asyncio.create_task(relay._consume_matrix()),
        asyncio.create_task(relay._send_loop()),
        asyncio.create_task(relay._prepare_loop(relay._images)),
        asyncio.create_task(relay._prepare_loop(relay._videos)),
    ]
    for _ in range(400):
        await asyncio.sleep(0.005)
        if until():
            break
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_discord_inline_custom_emoji_sends_captioned_image(tmp_path):
    qq = FakeQQ()
    uploaded = []

    async def capture_upload(group, data, filename, file_type):
        uploaded.append((filename, file_type))
        return "FILEINFO"

    qq.upload_group_media = capture_upload
    relay, store = make_relay(tmp_path, qq)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    relay.matrix.download = lambda url, max_bytes=None: _async(png_bytes)
    ev = text_event(
        body="tte，何時加的→",
        sender="@discord_523070793400320000:example.org",
        format="org.matrix.custom.html",
        formatted_body=(
            'tte，何時加的→<img data-mx-emoticon '
            'src="mxc://glyphatlas.org/H4m00AqFfV3mR7dDaaExFXPOxZgmq24Q" '
            'alt=":fumo_reimu_lying:" title=":fumo_reimu_lying:" height="32"/>'
        ),
    )
    seq = store.accept_transaction("t-inline", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.sent == []
    assert qq.media == [("G", "FILEINFO")]
    assert qq.media_contents == ["[D] Alice: tte，何時加的→:fumo_reimu_lying:"]
    assert uploaded == [("fumo_reimu_lying.png", 1)]
    store.close()


def test_discord_emoji_only_message_is_sent_as_media_with_reply_target(tmp_path):
    qq = FakeQQ()
    uploaded = []

    async def capture_upload(group, data, filename, file_type):
        uploaded.append((filename, file_type))
        return "FILEINFO"

    qq.upload_group_media = capture_upload
    relay, store = make_relay(tmp_path, qq)
    gif_bytes = b"GIF89a" + b"\x00" * 32
    relay.matrix.download = lambda url, max_bytes=None: _async(gif_bytes)
    relay.matrix.authors["$orig"] = "@bob:example.org"
    relay.matrix.names["@bob:example.org"] = "Bob"
    store.add_message("Q1", "G", ROOM, "$orig", "REF1", "matrix")
    ev = text_event(
        body="",
        sender="@discord_42:example.org",
        format="org.matrix.custom.html",
        formatted_body=(
            '<img data-mx-emoticon data-mau-animated-emoji '
            'src="mxc://example.org/spin" alt=":spin:" title=":spin:" height="32"/>'
        ),
        **{"m.relates_to": {"m.in_reply_to": {"event_id": "$orig"}}},
    )
    seq = store.accept_transaction("t-only", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.sent == []
    assert qq.media == [("G", "FILEINFO")]
    assert qq.media_contents == ["[D] Alice → Bob:"]
    assert qq.media_references == ["REF1"]
    assert uploaded == [("spin.gif", 1)]
    store.close()


def test_multiple_custom_emojis_are_deduped_and_capped(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    imgs = "".join(
        f'<img data-mx-emoticon src="mxc://example.org/e{i}" alt=":e{i}:"/>'
        for i in (1, 1, 2, 3, 4, 5, 6)
    )
    ev = text_event(
        body="hi",
        sender="@discord_1:example.org",
        format="org.matrix.custom.html",
        formatted_body=f"hi {imgs}",
    )
    seq = store.accept_transaction("t-many", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert len(qq.media) == 4
    assert qq.media_contents[0] == "[D] Alice: hi :e1::e1::e2::e3::e4::e5::e6:"
    assert qq.media_contents[1:] == ["[D] Alice:", "[D] Alice:", "[D] Alice:"]
    store.close()


def test_long_text_with_custom_emoji_sends_chunks_then_emoji_media(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    long_text = "a" * 2000
    ev = text_event(
        body=long_text,
        sender="@discord_1:example.org",
        format="org.matrix.custom.html",
        formatted_body=f'{long_text} <img data-mx-emoticon src="mxc://example.org/e1" alt=":e1:"/>',
    )
    seq = store.accept_transaction("t-long", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert len(qq.sent) == 2
    assert qq.sent[-1][1].endswith(":e1:")
    assert qq.media == [("G", "FILEINFO")]
    assert qq.media_contents == ["[D] Alice:"]
    store.close()


def test_inline_custom_emoji_falls_back_to_text_when_download_fails(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)

    async def fail_download(url, max_bytes=None):
        raise RuntimeError("boom")

    relay.matrix.download = fail_download
    ev = text_event(
        body="look",
        sender="@discord_1:example.org",
        format="org.matrix.custom.html",
        formatted_body='look <img data-mx-emoticon src="mxc://example.org/x" alt=":wow:"/>',
    )
    seq = store.accept_transaction("t-fail", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.media == []
    assert qq.sent == [("G", "[D] Alice: look :wow:", None)]
    store.close()


def test_inline_custom_emoji_sends_text_and_image_separately_when_caption_refused(tmp_path):
    class RefuseCaptionedMediaQQ(FakeQQ):
        async def send_group_media(
            self, group, file_info, *, content=None, reference=None, msg_id=None, msg_seq=1
        ):
            if content is not None:
                raise QQError(40034001, "caption not supported", status=400)
            return await super().send_group_media(
                group, file_info, content=content, reference=reference, msg_id=msg_id, msg_seq=msg_seq
            )

    qq = RefuseCaptionedMediaQQ()
    relay, store = make_relay(tmp_path, qq)
    ev = text_event(
        body="look",
        sender="@discord_1:example.org",
        format="org.matrix.custom.html",
        formatted_body='look <img data-mx-emoticon src="mxc://example.org/x" alt=":wow:"/>',
    )
    seq = store.accept_transaction("t-split", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.sent == [("G", "[D] Alice: look :wow:", None)]
    assert qq.media == [("G", "FILEINFO")]
    assert qq.media_contents == [None]
    store.close()


def test_edit_preserves_custom_emoji_shortcode(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q1", "G", ROOM, "$orig", "REF1", "matrix")
    ev = text_event(
        body="* updated",
        **{
            "m.new_content": {
                "msgtype": "m.text",
                "body": "updated",
                "format": "org.matrix.custom.html",
                "formatted_body": 'updated <img data-mx-emoticon src="mxc://example.org/e" alt=":ok:"/>',
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
        },
    )
    ev["event_id"] = "$edit-emoji"
    asyncio.run(relay.handle_matrix_event(ev))
    run_edit(relay)
    assert qq.deleted == ["Q1"]
    assert qq.sent[-1][1] == "[M] Alice: updated :ok:"
    store.close()


def test_discord_custom_emoji_reaction_sends_image_with_shortcode_and_reference(tmp_path):
    qq = FakeQQ()
    uploaded = []

    async def capture_upload(group, data, filename, file_type):
        uploaded.append((filename, file_type))
        return "FILEINFO"

    qq.upload_group_media = capture_upload
    relay, store = make_relay(tmp_path, qq)
    gif_bytes = b"GIF89a" + b"\x00" * 32
    relay.matrix.download = lambda url, max_bytes=None: _async(gif_bytes)
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    ev = reaction_event(
        target="$orig",
        key="mxc://glyphatlas.org/Z76MM1NYIniFHNflgi1WkDl7qOGmXJ9p",
        sender="@discord_523070793400320000:example.org",
        **{
            "com.beeper.reaction.shortcode": ":kusa_lol:",
            "fi.mau.discord.reaction": {
                "id": "1090394409306177556",
                "mxc": "mxc://glyphatlas.org/Z76MM1NYIniFHNflgi1WkDl7qOGmXJ9p",
                "name": "kusa_lol",
            },
        },
    )
    seq = store.accept_transaction("t-react", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.sent == []
    assert qq.media == [("G", "FILEINFO")]
    assert qq.media_contents == ["[D] Alice reacted with :kusa_lol:"]
    assert qq.media_references == ["REF9"]
    assert uploaded == [("kusa_lol.gif", 1)]
    store.close()


def test_custom_emoji_reaction_in_same_burst_resolves_inflight_target(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    msg = text_event("hello")
    react = reaction_event(
        target="$e",
        key="mxc://example.org/kusa",
        sender="@discord_1:example.org",
        **{"com.beeper.reaction.shortcode": ":kusa:"},
    )
    seq1, seq2 = store.accept_transaction("t-burst", [msg, react])
    relay.incoming.put_nowait((seq1, msg))
    relay.incoming.put_nowait((seq2, react))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.sent == [("G", "[M] Alice: hello", None)]
    assert qq.media_contents == ["[D] Alice reacted with :kusa:"]
    assert qq.media_references == ["REF1"]
    store.close()


def test_custom_emoji_reaction_falls_back_to_text_when_download_fails(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)

    async def fail_download(url, max_bytes=None):
        raise RuntimeError("offline")

    relay.matrix.download = fail_download
    store.add_message("Q9", "G", ROOM, "$orig", "REF9", "matrix")
    ev = reaction_event(
        target="$orig",
        key="mxc://example.org/kusa",
        sender="@discord_1:example.org",
        **{"com.beeper.reaction.shortcode": ":kusa:"},
    )
    seq = store.accept_transaction("t-react-fail", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.media == []
    assert qq.sent == [("G", "[D] Alice reacted with :kusa:", "REF9")]
    store.close()


def test_telegram_forwarded_image_preserves_forwarder_and_quote_caption(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    body = "Forwarded message from Kit Tsang\n\n> 這是產出來的字"
    ev = {
        "room_id": ROOM,
        "sender": "@telegram_221229384:example.org",
        "type": "m.room.message",
        "event_id": "$fwd-img",
        "content": {
            "msgtype": "m.image",
            "url": "mxc://example.org/img",
            "filename": "image.jpg",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": (
                "Forwarded message from <strong>Kit Tsang</strong><br>"
                "<blockquote data-telegram-forward>這是產出來的字</blockquote>"
            ),
            "info": {"mimetype": "image/jpeg", "w": 1169, "h": 969},
        },
    }
    seq = store.accept_transaction("t-fwd-img", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.sent == []
    assert qq.media == [("G", "FILEINFO")]
    assert qq.media_contents == [f"[T] Alice: {body}"]
    store.close()


def test_telegram_forwarded_image_without_caption_strips_empty_quote_marker(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    ev = {
        "room_id": ROOM,
        "sender": "@telegram_221229384:example.org",
        "type": "m.room.message",
        "event_id": "$fwd-empty",
        "content": {
            "msgtype": "m.image",
            "url": "mxc://example.org/img",
            "filename": "image.jpg",
            "body": "Forwarded message from Kit Tsang\n\n> ",
            "info": {"mimetype": "image/jpeg"},
        },
    }
    seq = store.accept_transaction("t-fwd-empty", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.media_contents == ["[T] Alice: Forwarded message from Kit Tsang"]
    store.close()


def test_uncaptioned_image_and_sticker_keep_speaker_only_caption(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    img_ev = {
        "room_id": ROOM,
        "sender": "@telegram_1:example.org",
        "type": "m.room.message",
        "event_id": "$plain-img",
        "content": {
            "msgtype": "m.image",
            "url": "mxc://example.org/img",
            "filename": "image.jpg",
            "body": "image.jpg",
            "info": {"mimetype": "image/jpeg"},
        },
    }
    sticker_ev = {
        "room_id": ROOM,
        "sender": "@bigfatfish:example.org",
        "type": "m.sticker",
        "event_id": "$stk",
        "content": {
            "url": "mxc://example.org/stk",
            "filename": "fish.png",
            "body": "laughing hard",
            "info": {"mimetype": "image/png"},
        },
    }
    seqs = store.accept_transaction("t-plain-stk", [img_ev, sticker_ev])
    relay.incoming.put_nowait((seqs[0], img_ev))
    relay.incoming.put_nowait((seqs[1], sticker_ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.media_contents == ["[T] Alice:", "[M] Alice:"]
    store.close()


def test_long_media_caption_and_audio_caption_are_sent_as_text(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    long_caption = "Forwarded message from Kit Tsang\n\n> " + ("字" * 2000)
    long_ev = {
        "room_id": ROOM,
        "sender": "@telegram_1:example.org",
        "type": "m.room.message",
        "event_id": "$long-cap",
        "content": {
            "msgtype": "m.image",
            "url": "mxc://example.org/img",
            "filename": "image.jpg",
            "body": long_caption,
            "info": {"mimetype": "image/jpeg"},
        },
    }
    audio_ev = {
        "room_id": ROOM,
        "sender": "@telegram_1:example.org",
        "type": "m.room.message",
        "event_id": "$aud-cap",
        "content": {
            "msgtype": "m.audio",
            "url": "mxc://example.org/a",
            "filename": "voice.ogg",
            "body": "Forwarded message from Kit Tsang\n\n> voice note",
            "info": {"mimetype": "audio/ogg"},
        },
    }
    seqs = store.accept_transaction("t-long-aud", [long_ev, audio_ev])
    relay.incoming.put_nowait((seqs[0], long_ev))
    relay.incoming.put_nowait((seqs[1], audio_ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.media_contents == ["[T] Alice:", None]
    assert len(qq.sent) == 3
    assert qq.sent[0][1].startswith("[T] Alice: Forwarded message from Kit Tsang")
    assert qq.sent[2][1] == "[T] Alice: Forwarded message from Kit Tsang\n\n> voice note"
    store.close()


def test_forwarded_image_keeps_quote_when_media_download_fails(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)

    async def fail_download(url, max_bytes=None):
        raise RuntimeError("offline")

    relay.matrix.download = fail_download
    ev = {
        "room_id": ROOM,
        "sender": "@telegram_1:example.org",
        "type": "m.room.message",
        "event_id": "$fwd-fail",
        "content": {
            "msgtype": "m.image",
            "url": "mxc://example.org/img",
            "filename": "image.jpg",
            "body": "Forwarded message from Kit Tsang\n\n> 這是產出來的字",
            "info": {"mimetype": "image/jpeg"},
        },
    }
    seq = store.accept_transaction("t-fwd-fail", [ev])[0]
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.media == []
    assert qq.sent == [
        (
            "G",
            "[T] Alice: Forwarded message from Kit Tsang\n\n> 這是產出來的字\n[image not accepted by QQ]",
            None,
        )
    ]
    store.close()


def test_audio_caption_resumes_after_restart_when_caption_send_fails(tmp_path):
    class FailTextQQ(FakeQQ):
        async def send_group_text(self, *args, **kwargs):
            raise QQError(502, "busy", status=502)

    qq1 = FailTextQQ()
    relay1, store = make_relay(tmp_path, qq1)
    ev = {
        "room_id": ROOM,
        "sender": "@telegram_1:example.org",
        "type": "m.room.message",
        "event_id": "$aud-resume",
        "content": {
            "msgtype": "m.audio",
            "url": "mxc://example.org/a",
            "filename": "voice.ogg",
            "body": "Forwarded message from Kit Tsang\n\n> voice note",
            "info": {"mimetype": "audio/ogg"},
        },
    }
    (seq,) = store.accept_transaction("t-aud-resume", [ev])
    relay1.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay1, lambda: bool(qq1.media) and not relay1._outstanding))
    assert len(qq1.media) == 1 and _pending(store)

    qq2 = FakeQQ()
    relay2 = Relay(relay1.cfg, store, FakeMatrix(), qq2)
    relay2._labels = dict(relay1._labels)
    relay2._claim_pending(idle=0)
    asyncio.run(_run_media_loops(relay2, lambda: not _pending(store)))
    assert qq2.media == []
    assert qq2.sent == [
        ("G", "[T] Alice: Forwarded message from Kit Tsang\n\n> voice note", None)
    ]
    store.close()


def test_forwarded_caption_with_emoji_preserves_quote_and_literal_gt_caption_kept(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    fwd_body = "Forwarded message from Kit Tsang\n\n> hi :wave:"
    fwd_ev = {
        "room_id": ROOM,
        "sender": "@telegram_1:example.org",
        "type": "m.room.message",
        "event_id": "$fwd-emoji",
        "content": {
            "msgtype": "m.image",
            "url": "mxc://example.org/img",
            "filename": "image.jpg",
            "body": fwd_body,
            "format": "org.matrix.custom.html",
            "formatted_body": (
                "Forwarded message from <strong>Kit Tsang</strong><br>"
                '<blockquote data-telegram-forward>hi '
                '<img data-mx-emoticon src="mxc://example.org/w" alt=":wave:"/></blockquote>'
            ),
            "info": {"mimetype": "image/jpeg"},
        },
    }
    gt_ev = {
        "room_id": ROOM,
        "sender": "@discord_1:example.org",
        "type": "m.room.message",
        "event_id": "$gt-cap",
        "content": {
            "msgtype": "m.image",
            "url": "mxc://example.org/img2",
            "filename": "pic.png",
            "body": ">",
            "info": {"mimetype": "image/png"},
        },
    }
    seqs = store.accept_transaction("t-quote-gt", [fwd_ev, gt_ev])
    relay.incoming.put_nowait((seqs[0], fwd_ev))
    relay.incoming.put_nowait((seqs[1], gt_ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.media_contents == [f"[T] Alice: {fwd_body}", "[D] Alice: >"]
    store.close()


def test_captioned_media_fallback_retries_transient_bare_media_send(tmp_path):
    class FlakySplitQQ(FakeQQ):
        def __init__(self):
            super().__init__()
            self.bare_attempts = 0

        async def send_group_media(
            self, group, file_info, *, content=None, reference=None, msg_id=None, msg_seq=1
        ):
            if content is not None:
                raise QQError(304003, "caption refused", status=400)
            self.bare_attempts += 1
            if self.bare_attempts == 1:
                raise QQError(502, "busy", status=502)
            return await super().send_group_media(
                group, file_info, content=content, reference=reference, msg_id=msg_id, msg_seq=msg_seq
            )

    qq = FlakySplitQQ()
    relay, store = make_relay(tmp_path, qq)
    ev = {
        "room_id": ROOM,
        "sender": "@telegram_1:example.org",
        "type": "m.room.message",
        "event_id": "$split-retry",
        "content": {
            "msgtype": "m.image",
            "url": "mxc://example.org/img",
            "filename": "image.jpg",
            "body": "Forwarded message from Kit Tsang\n\n> 這是產出來的字",
            "info": {"mimetype": "image/jpeg"},
        },
    }
    (seq,) = store.accept_transaction("t-split-retry", [ev])
    relay.incoming.put_nowait((seq, ev))
    asyncio.run(_run_media_loops(relay, lambda: not _pending(store)))
    assert qq.sent == [("G", "[T] Alice: Forwarded message from Kit Tsang\n\n> 這是產出來的字", None)]
    assert qq.media == [("G", "FILEINFO")]
    assert qq.bare_attempts == 2
    store.close()
