import asyncio
import time

from qq_bridge.config import Config, MatrixConfig, QQConfig
from qq_bridge.qq import QQError
from qq_bridge.relay import Relay
from qq_bridge.store import Store

ROOM = "!room:example.org"


class FakeQQ:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.delete_error = None
        self.quote_error = None

    async def delete_group_message(self, group_openid, message_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(message_id)

    async def send_group_text(
        self, group_openid, content, *, reference=None, msg_id=None
    ):
        if reference and self.quote_error:
            raise self.quote_error
        self.sent.append((content, reference))
        return {
            "id": f"Q{len(self.sent)}",
            "ext_info": {"ref_idx": f"REFIDX_{len(self.sent)}"},
        }


class FakeMatrix:
    async def member(self, room_id, user_id):
        return {"displayname": "Alice"}


def make_relay(tmp_path, qq):
    cfg = Config(
        matrix=MatrixConfig(
            homeserver="http://hs",
            domain="example.org",
            as_token="as",
            hs_token="hs",
            bot_mxid="@qqbot:example.org",
        ),
        qq=QQConfig(app_id="", app_secret=""),
        bridges=(),
        ignore_senders=(),
        ignore_notices=True,
        state_path=tmp_path / "state.sqlite3",
    )
    store = Store(cfg.state_path)
    return Relay(cfg, store, FakeMatrix(), qq), store


def text_job(text="Alice: fixed", reference=None, edit_of="$orig"):
    job = {
        "kind": "text",
        "group": "G",
        "room": ROOM,
        "event_id": "$orig",
        "text": text,
        "reference": reference,
    }
    if edit_of:
        job["edit_of"] = edit_of
    return job


def edit_event(new_body="fixed"):
    return {
        "room_id": ROOM,
        "sender": "@alice:example.org",
        "type": "m.room.message",
        "event_id": "$edit",
        "content": {
            "body": f"* {new_body}",
            "msgtype": "m.text",
            "m.new_content": {"body": new_body, "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
        },
    }


def test_edit_recalls_the_previous_message(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("QOLD", "G", ROOM, "$orig", "REFIDX_OLD", "matrix")

    asyncio.run(relay._send_text(text_job()))

    assert qq.deleted == ["QOLD"]
    assert qq.sent == [("Alice: fixed", None)]
    assert [(row["qq_id"], row["event_id"]) for row in store.messages_for_event("$orig")] == [
        ("Q1", "$orig")
    ]


def test_edit_quotes_the_previous_message_when_recall_fails(tmp_path):
    qq = FakeQQ()
    qq.delete_error = QQError(40064004, "too old")
    relay, store = make_relay(tmp_path, qq)
    store.add_message("QOLD", "G", ROOM, "$orig", "REFIDX_OLD", "matrix")

    asyncio.run(relay._send_text(text_job()))

    assert qq.deleted == []
    assert qq.sent == [("Alice: fixed", "REFIDX_OLD")]
    assert {row["qq_id"] for row in store.messages_for_event("$orig")} == {"QOLD", "Q1"}


def test_edit_drops_the_quote_when_qq_rejects_it(tmp_path):
    qq = FakeQQ()
    qq.delete_error = QQError(40064004, "too old")
    qq.quote_error = QQError(304103, "message id expired")
    relay, store = make_relay(tmp_path, qq)
    store.add_message("QOLD", "G", ROOM, "$orig", "REFIDX_OLD", "matrix")

    asyncio.run(relay._send_text(text_job()))

    assert qq.sent == [("Alice: fixed", None)]


def test_edit_does_not_attempt_an_expired_recall(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("QOLD", "G", ROOM, "$orig", "REFIDX_OLD", "matrix")
    store.conn.execute(
        "UPDATE messages SET ts = ? WHERE qq_id = 'QOLD'", (int(time.time()) - 600,)
    )

    asyncio.run(relay._send_text(text_job()))

    assert qq.deleted == []
    assert qq.sent == [("Alice: fixed", "REFIDX_OLD")]


def test_matrix_edit_queues_the_new_body_for_the_original_event(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.bind(ROOM, "G", "test")

    asyncio.run(relay.handle_matrix_event(edit_event()))

    job = relay._queue.get_nowait()
    assert job["text"] == "Alice: fixed"
    assert job["event_id"] == "$orig"
    assert job["edit_of"] == "$orig"
    assert job["reference"] is None
    assert relay._queue.empty()


def test_reply_to_an_edited_message_uses_its_latest_copy(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.bind(ROOM, "G", "test")
    store.add_message("Q1", "G", ROOM, "$orig", "REFIDX_1", "matrix")
    store.add_message("Q2", "G", ROOM, "$orig", "REFIDX_2", "matrix")

    asyncio.run(
        relay.handle_matrix_event(
            {
                "room_id": ROOM,
                "sender": "@alice:example.org",
                "type": "m.room.message",
                "event_id": "$reply",
                "content": {
                    "body": "> quote\n\nanswer",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$orig"}},
                },
            }
        )
    )

    job = relay._queue.get_nowait()
    assert job["text"] == "Alice: answer"
    assert job["reference"] == "REFIDX_2"
    assert "edit_of" not in job


def test_redaction_recalls_every_copy(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("Q1", "G", ROOM, "$orig", "REFIDX_1", "matrix")
    store.add_message("Q2", "G", ROOM, "$orig", "REFIDX_2", "matrix")

    asyncio.run(relay._redact({"redacts": "$orig"}))

    assert qq.deleted == ["Q1", "Q2"]
    assert store.messages_for_event("$orig") == []


def reaction_event(target="$orig", key="👍", sender="@alice:example.org"):
    return {
        "room_id": ROOM,
        "sender": sender,
        "type": "m.reaction",
        "event_id": "$reaction",
        "content": {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target,
                "key": key,
            }
        },
    }


def test_matrix_reaction_becomes_a_quoted_reply(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.bind(ROOM, "G", "test")
    store.add_message("Q1", "G", ROOM, "$orig", "REFIDX_1", "matrix")

    async def run():
        await relay.handle_matrix_event(reaction_event())
        job = relay._queue.get_nowait()
        assert job["text"] == "Alice: 👍"
        assert job["react_to"] == "$orig"
        assert "edit_of" not in job
        await relay._send_text(job)
        return job

    job = asyncio.run(run())

    assert job["event_id"] == "$reaction"
    assert qq.sent == [("Alice: 👍", "REFIDX_1")]


def test_reaction_to_an_unbridged_message_is_skipped(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.bind(ROOM, "G", "test")

    async def run():
        await relay.handle_matrix_event(reaction_event(target="$missing"))
        await relay._send_text(relay._queue.get_nowait())

    asyncio.run(run())

    assert qq.sent == []


def test_ghost_reaction_is_skipped(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.bind(ROOM, "G", "test")
    store.add_message("Q1", "G", ROOM, "$orig", "REFIDX_1", "matrix")

    asyncio.run(
        relay.handle_matrix_event(reaction_event(sender="@qq_abc:example.org"))
    )

    assert relay._queue.empty()


def test_reaction_removal_recalls_the_quoted_reply(tmp_path):
    qq = FakeQQ()
    relay, store = make_relay(tmp_path, qq)
    store.add_message("QR", "G", ROOM, "$reaction", "REFIDX_R", "matrix")

    asyncio.run(
        relay.handle_matrix_event(
            {
                "room_id": ROOM,
                "sender": "@alice:example.org",
                "type": "m.room.redaction",
                "event_id": "$redact",
                "redacts": "$reaction",
                "content": {},
            }
        )
    )

    assert qq.deleted == ["QR"]
    assert store.messages_for_event("$reaction") == []
