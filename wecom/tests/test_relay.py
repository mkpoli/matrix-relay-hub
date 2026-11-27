import asyncio
import io
from pathlib import Path

from PIL import Image

from wecom_bridge.config import Config, MatrixConfig, RoomConfig, WeComConfig
from wecom_bridge.matrix import MatrixError
from wecom_bridge.relay import Relay
from wecom_bridge.store import Store
from wecom_bridge.wecom import WeComAIBotClient, WeComError, WeComWebhookClient


class FakeMatrix:
    def __init__(self, rooms: dict[str, str] | None = None) -> None:
        self.rooms = rooms or {"#main:example.org": "!main:example.org"}
        self.members: dict[tuple[str, str], str] = {}
        self.events: dict[tuple[str, str], dict] = {}
        self.media_blobs: dict[str, bytes] = {}
        self.sent: list[tuple[str, str, dict]] = []
        self.sent_txns: list[str | None] = []
        self.uploaded: list[tuple[bytes, str, str, str]] = []
        self.registered: list[tuple[str, str]] = []
        self.joined: list[tuple[str, str]] = []
        self.displaynames: list[tuple[str, str]] = []
        self.fail_member = False
        self.fail_join = False
        self.fail_send = False

    async def resolve_alias(self, alias: str) -> str:
        return self.rooms[alias]

    async def join(self, room_id: str, user_id: str) -> None:
        if self.fail_join:
            self.fail_join = False
            raise MatrixError(503, "unavailable")
        self.joined.append((room_id, user_id))

    async def is_encrypted(self, room_id: str) -> bool:
        return False

    async def register_user(self, localpart: str, displayname: str) -> str:
        self.registered.append((localpart, displayname))
        return f"@{localpart}:example.org"

    async def set_displayname(self, user_id: str, displayname: str) -> None:
        self.displaynames.append((user_id, displayname))

    async def member(self, room_id: str, user_id: str) -> dict | None:
        if self.fail_member:
            self.fail_member = False
            raise MatrixError(503, "busy")
        name = self.members.get((room_id, user_id))
        return {"displayname": name} if name else None

    async def event(self, room_id: str, event_id: str, user_id: str | None = None) -> dict | None:
        return self.events.get((room_id, event_id))

    async def send_message(
        self, room_id: str, user_id: str, content: dict, *, txn_id: str | None = None
    ) -> str:
        if self.fail_send:
            self.fail_send = False
            raise MatrixError(503, "homeserver down")
        self.sent.append((room_id, user_id, content))
        self.sent_txns.append(txn_id)
        return f"$mx_{len(self.sent)}"

    async def upload(self, data: bytes, content_type: str, filename: str, user_id: str) -> str:
        self.uploaded.append((data, content_type, filename, user_id))
        return f"mxc://example.org/up_{len(self.uploaded)}"

    async def download(self, mxc: str, max_bytes: int | None = None) -> bytes:
        return self.media_blobs[mxc]


class FakeAIBot(WeComAIBotClient):
    def __init__(self, cfg: WeComConfig) -> None:
        self.cfg = cfg
        self.sent_markdowns: list[tuple[str, str]] = []
        self.uploaded: list[tuple[str, str, bytes]] = []
        self.sent_media: list[tuple[str, str, str, str | None]] = []
        self.downloads: dict[str, tuple[bytes, str | None, str]] = {}
        self.fail_next_send = False

    async def send_markdown(self, chat_id: str, content: str) -> dict:
        if self.fail_next_send:
            self.fail_next_send = False
            raise WeComError(45009, "rate limited")
        self.sent_markdowns.append((chat_id, content))
        return {"errcode": 0}

    async def upload_media(self, media_type: str, filename: str, data: bytes) -> str:
        self.uploaded.append((media_type, filename, data))
        return f"mid-{len(self.uploaded)}"

    async def send_media(
        self, chat_id: str, media_type: str, media_id: str, *, title: str | None = None
    ) -> dict:
        self.sent_media.append((chat_id, media_type, media_id, title))
        return {"errcode": 0}

    async def download_media(self, url: str, aes_key: str | None = None, *, max_bytes: int = 0):
        return self.downloads[url]


class FakeWebhook(WeComWebhookClient):
    def __init__(self, cfg: WeComConfig) -> None:
        self.cfg = cfg
        self.markdowns: list[str] = []
        self.texts: list[str] = []
        self.images: list[bytes] = []
        self.files: list[tuple[str, bytes, str]] = []

    async def send_markdown(self, content: str) -> dict:
        self.markdowns.append(content)
        return {"errcode": 0}

    async def send_text(self, content: str) -> dict:
        self.texts.append(content)
        return {"errcode": 0}

    async def send_image(self, data: bytes) -> dict:
        self.images.append(data)
        return {"errcode": 0}

    async def upload_file(self, filename: str, data: bytes) -> str:
        media_id = f"wh-{len(self.files) + 1}"
        self.files.append((filename, data, media_id))
        return media_id

    async def send_file(self, media_id: str) -> dict:
        return {"errcode": 0}


def _png_bytes() -> bytes:
    img = Image.new("RGB", (16, 16), color=(10, 200, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _make_config(tmp_path: Path, *, mode: str = "aibot", rooms: tuple[RoomConfig, ...] | None = None) -> Config:
    room_list = rooms or (RoomConfig("#main:example.org", "main"),)
    wecom = (
        WeComConfig(
            mode="aibot",
            bot_id="b1",
            secret="s1",
            chat_id="chat-1",
            bot_name="RelayBot",
            incoming=room_list[0].alias,
        )
        if mode == "aibot"
        else WeComConfig(mode="webhook", webhook_key="wh-key")
    )
    return Config(
        matrix=MatrixConfig(
            homeserver="http://hs:8008",
            domain="example.org",
            as_token="as",
            hs_token="hs",
            bot_mxid="@wecombot:example.org",
        ),
        wecom=wecom,
        rooms=room_list,
        members={"zhangsan": "Zhang San"},
        ignore_senders=(),
        ignore_notices=True,
        state_path=tmp_path / "state.sqlite3",
        relay_reactions=True,
    )


async def _drain(relay: Relay) -> None:
    for _ in range(50):
        if (
            relay.incoming.empty()
            and relay._queue.empty()
            and relay._images.empty()
            and relay._videos.empty()
            and not relay._outstanding
        ):
            await asyncio.sleep(0.02)
            if relay._queue.empty() and not relay._outstanding:
                return
        await asyncio.sleep(0.02)


def test_bidirectional_aibot_relay(tmp_path: Path):
    async def scenario():
        cfg = _make_config(
            tmp_path,
            mode="aibot",
            rooms=(
                RoomConfig("#main:example.org", "main"),
                RoomConfig("#dev:example.org", "dev"),
            ),
        )
        store = Store(cfg.state_path)
        matrix = FakeMatrix(
            {"#main:example.org": "!main:example.org", "#dev:example.org": "!dev:example.org"}
        )
        matrix.members[("!dev:example.org", "@discord_42:example.org")] = "Alice"
        matrix.members[("!dev:example.org", "@bob:example.org")] = "Bob"
        matrix.events[("!dev:example.org", "$orig")] = {"sender": "@bob:example.org"}
        matrix.events[("!dev:example.org", "$bob_msg")] = {
            "sender": "@bob:example.org",
            "room_id": "!dev:example.org",
        }
        png = _png_bytes()
        matrix.media_blobs["mxc://example.org/pic"] = png

        wecom = FakeAIBot(cfg.wecom)
        wecom.downloads["https://wecom.invalid/img1"] = (png, "cat.png", "image/png")

        relay = Relay(cfg, store, matrix, wecom)
        await relay.start()
        try:
            # 1. Matrix -> WeCom text with reply target, source tag, and room label.
            seqs = store.accept_transaction(
                "t1",
                [
                    {
                        "event_id": "$e1",
                        "room_id": "!dev:example.org",
                        "sender": "@discord_42:example.org",
                        "type": "m.room.message",
                        "content": {
                            "msgtype": "m.text",
                            "body": "> <@bob:example.org> prev\n\nhello from discord",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$orig"}},
                        },
                    }
                ],
            )
            relay.incoming.put_nowait((seqs[0], {
                "event_id": "$e1",
                "room_id": "!dev:example.org",
                "sender": "@discord_42:example.org",
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "> <@bob:example.org> prev\n\nhello from discord",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$orig"}},
                },
            }))
            await _drain(relay)
            assert wecom.sent_markdowns == [("chat-1", "[D] [dev] Alice → Bob: hello from discord")]
            assert store.claim_pending(max_age=3600, max_attempts=5) == []

            # 2. Matrix -> WeCom image with caption.
            relay.incoming.put_nowait((None, {
                "event_id": "$e2",
                "room_id": "!dev:example.org",
                "sender": "@bob:example.org",
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.image",
                    "body": "look at this",
                    "filename": "pic.png",
                    "url": "mxc://example.org/pic",
                    "info": {"mimetype": "image/png"},
                },
            }))
            await _drain(relay)
            assert wecom.sent_markdowns[-1] == ("chat-1", "[M] [dev] Bob: look at this")
            assert wecom.uploaded == [("image", "pic.png", png)]
            assert wecom.sent_media == [("chat-1", "image", "mid-1", None)]

            # 3. Matrix edit (valid author vs spoofed non-author) & reaction & loop prevention.
            relay.incoming.put_nowait((None, {
                "event_id": "$spoof_edit",
                "room_id": "!dev:example.org",
                "sender": "@discord_42:example.org",
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "* spoofed",
                    "m.new_content": {"msgtype": "m.text", "body": "spoofed"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$bob_msg"},
                },
            }))
            relay.incoming.put_nowait((None, {
                "event_id": "$edit1",
                "room_id": "!dev:example.org",
                "sender": "@bob:example.org",
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "* fixed",
                    "m.new_content": {"msgtype": "m.text", "body": "fixed text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$bob_msg"},
                },
            }))
            relay.incoming.put_nowait((None, {
                "event_id": "$react1",
                "room_id": "!dev:example.org",
                "sender": "@bob:example.org",
                "type": "m.reaction",
                "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$e1", "key": "🎉"}},
            }))
            # Bridge bot status reactions (e.g. Heisenbridge 🔗) must be ignored.
            relay.incoming.put_nowait((None, {
                "event_id": "$bot_react",
                "room_id": "!dev:example.org",
                "sender": "@heisenbridge:example.org",
                "type": "m.reaction",
                "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$e1", "key": "🔗"}},
            }))
            # Echo from our own WeCom ghost must be ignored.
            relay.incoming.put_nowait((None, {
                "event_id": "$loop",
                "room_id": "!main:example.org",
                "sender": "@wecom_zhangsan:example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "should not echo"},
            }))
            await _drain(relay)
            assert wecom.sent_markdowns[-2:] == [
                ("chat-1", "[M] [dev] Bob ✎: fixed text"),
                ("chat-1", "[M] [dev] Bob reacted with 🎉"),
            ]

            # 4. Transient WeCom failure retains event in pending queue for retry.
            wecom.fail_next_send = True
            ev_retry = {
                "event_id": "$retry1",
                "room_id": "!dev:example.org",
                "sender": "@bob:example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "try again"},
            }
            seqs2 = store.accept_transaction("t2", [ev_retry])
            relay.incoming.put_nowait((seqs2[0], ev_retry))
            await _drain(relay)
            pending = store.claim_pending(max_age=3600, max_attempts=5)
            assert len(pending) == 1
            assert pending[0][1]["event_id"] == "$retry1"

            # 5. WeCom -> Matrix: group text with @BotName, quote, mixed image, dedup, and chat_id filter.
            await relay.on_wecom_frame(
                {
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "r1"},
                    "body": {
                        "msgid": "wc-1",
                        "chattype": "group",
                        "chatid": "chat-1",
                        "from": {"userid": "zhangsan"},
                        "msgtype": "text",
                        "text": {"content": "@RelayBot\u2005hello matrix"},
                        "quote": {"msgtype": "text", "text": {"content": "original msg"}},
                    },
                }
            )
            # Duplicate msgid is ignored.
            await relay.on_wecom_frame(
                {
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "r1b"},
                    "body": {
                        "msgid": "wc-1",
                        "chattype": "group",
                        "chatid": "chat-1",
                        "from": {"userid": "zhangsan"},
                        "msgtype": "text",
                        "text": {"content": "@RelayBot duplicate"},
                    },
                }
            )
            # Other chat_id is ignored for isolation.
            await relay.on_wecom_frame(
                {
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "r2"},
                    "body": {
                        "msgid": "wc-other",
                        "chattype": "group",
                        "chatid": "chat-OTHER",
                        "from": {"userid": "zhangsan"},
                        "msgtype": "text",
                        "text": {"content": "@RelayBot secret"},
                    },
                }
            )
            # Mixed text + image message.
            await relay.on_wecom_frame(
                {
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "r3"},
                    "body": {
                        "msgid": "wc-2",
                        "chattype": "group",
                        "chatid": "chat-1",
                        "from": {"userid": "zhangsan"},
                        "msgtype": "mixed",
                        "mixed": {
                            "msg_item": [
                                {"msgtype": "text", "text": {"content": "@RelayBot check pic"}},
                                {"msgtype": "image", "image": {"url": "https://wecom.invalid/img1", "aeskey": "k"}},
                            ]
                        },
                    },
                }
            )

            assert len(matrix.sent) == 3
            assert matrix.sent[0][0] == "!main:example.org"
            assert matrix.sent[0][1] == "@wecom_zhangsan:example.org"
            assert matrix.sent[0][2]["body"] == "> original msg\n\nhello matrix"
            assert "<blockquote>original msg</blockquote>hello matrix" == matrix.sent[0][2]["formatted_body"]
            assert ("wecom_zhangsan", "Zhang San") in matrix.registered
            assert matrix.sent[1][2]["body"] == "check pic"
            assert matrix.sent[2][2]["msgtype"] == "m.image"
            assert matrix.sent[2][2]["filename"] == "cat.png"
        finally:
            await relay.stop()
            store.close()

    asyncio.run(scenario())


def test_webhook_mode_outbound_relay(tmp_path: Path):
    async def scenario():
        cfg = _make_config(tmp_path, mode="webhook")
        store = Store(cfg.state_path)
        matrix = FakeMatrix()
        matrix.members[("!main:example.org", "@alice:example.org")] = "Alice"
        png = _png_bytes()
        matrix.media_blobs["mxc://example.org/p"] = png
        matrix.media_blobs["mxc://example.org/f"] = b"report contents here"

        wecom = FakeWebhook(cfg.wecom)
        relay = Relay(cfg, store, matrix, wecom)
        await relay.start()
        try:
            relay.incoming.put_nowait((None, {
                "event_id": "$w1",
                "room_id": "!main:example.org",
                "sender": "@alice:example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "hello webhook"},
            }))
            relay.incoming.put_nowait((None, {
                "event_id": "$w2",
                "room_id": "!main:example.org",
                "sender": "@alice:example.org",
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.image",
                    "body": "chart.png",
                    "filename": "chart.png",
                    "url": "mxc://example.org/p",
                    "info": {"mimetype": "image/png"},
                },
            }))
            relay.incoming.put_nowait((None, {
                "event_id": "$w3",
                "room_id": "!main:example.org",
                "sender": "@alice:example.org",
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.file",
                    "body": "report.txt",
                    "filename": "report.txt",
                    "url": "mxc://example.org/f",
                    "info": {"mimetype": "text/plain"},
                },
            }))
            await _drain(relay)
            # Single room: no [label] prefix.
            assert wecom.markdowns == [
                "[M] Alice: hello webhook",
                "[M] Alice sent chart.png",
                "[M] Alice sent report.txt",
            ]
            assert wecom.images == [png]
            assert wecom.files == [("report.txt", b"report contents here", "wh-1")]
        finally:
            await relay.stop()
            store.close()

    asyncio.run(scenario())


def test_multipart_retry_and_inbound_retry_and_join_retry(tmp_path: Path):
    async def scenario():
        cfg = _make_config(tmp_path, mode="aibot")
        store = Store(cfg.state_path)
        matrix = FakeMatrix()
        matrix.members[("!main:example.org", "@alice:example.org")] = "Alice"
        matrix.fail_join = True
        wecom = FakeAIBot(cfg.wecom)
        relay = Relay(cfg, store, matrix, wecom)
        await relay.start()
        try:
            # Failed join did not mark the room as joined, so retry succeeds.
            assert "#main:example.org" not in relay._joined_rooms
            await relay._resolve_room(cfg.rooms[0])
            assert "#main:example.org" in relay._joined_rooms

            # Multipart message where chunk 0 fails: chunk 1 must be skipped, and retry sends 0 then 1.
            long_body = ("A" * 2500) + "\n" + ("B" * 2500)
            ev = {
                "event_id": "$long1",
                "room_id": "!main:example.org",
                "sender": "@alice:example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": long_body},
            }
            wecom.fail_next_send = True
            seqs = store.accept_transaction("t-long", [ev])
            relay.incoming.put_nowait((seqs[0], ev))
            await _drain(relay)
            assert wecom.sent_markdowns == []
            pending = store.claim_pending(max_age=3600, max_attempts=5)
            assert len(pending) == 1
            relay.incoming.put_nowait(pending[0])
            await _drain(relay)
            assert len(wecom.sent_markdowns) == 2
            assert "A" * 100 in wecom.sent_markdowns[0][1]
            assert "B" * 100 in wecom.sent_markdowns[1][1]

            # Inbound Matrix 503 persists callback for retry with deterministic txn_id.
            matrix.fail_send = True
            frame = {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "in-1"},
                "body": {
                    "msgid": "wc-retry-1",
                    "chattype": "group",
                    "chatid": "chat-1",
                    "from": {"userid": "zhangsan"},
                    "msgtype": "text",
                    "text": {"content": "@RelayBot survives outage"},
                },
            }
            await relay.on_wecom_frame(frame)
            assert matrix.sent == []
            await relay.retry_inbound_once()
            assert len(matrix.sent) == 1
            assert matrix.sent[0][2]["body"] == "survives outage"
            assert matrix.sent_txns[0] == "wecom_wc-retry-1_0"
        finally:
            await relay.stop()
            store.close()

    asyncio.run(scenario())


def test_media_upload_failure_does_not_duplicate_header(tmp_path: Path):
    async def scenario():
        cfg = _make_config(tmp_path, mode="aibot")
        store = Store(cfg.state_path)
        matrix = FakeMatrix()
        matrix.members[("!main:example.org", "@alice:example.org")] = "Alice"
        matrix.media_blobs["mxc://example.org/pic"] = _png_bytes()

        class FlakyUploadAIBot(FakeAIBot):
            def __init__(self, c: WeComConfig) -> None:
                super().__init__(c)
                self.fail_upload = True

            async def upload_media(self, media_type: str, filename: str, data: bytes) -> str:
                if self.fail_upload:
                    self.fail_upload = False
                    raise WeComError(846616, "upload busy")
                return await super().upload_media(media_type, filename, data)

        wecom = FlakyUploadAIBot(cfg.wecom)
        relay = Relay(cfg, store, matrix, wecom)
        await relay.start()
        try:
            ev = {
                "event_id": "$m1",
                "room_id": "!main:example.org",
                "sender": "@alice:example.org",
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.image",
                    "body": "cat.png",
                    "filename": "cat.png",
                    "url": "mxc://example.org/pic",
                    "info": {"mimetype": "image/png"},
                },
            }
            seqs = store.accept_transaction("t-m1", [ev])
            relay.incoming.put_nowait((seqs[0], ev))
            await _drain(relay)
            # Upload failed before header, so zero headers were sent.
            assert wecom.sent_markdowns == []
            pending = store.claim_pending(max_age=3600, max_attempts=5)
            assert len(pending) == 1
            relay.incoming.put_nowait(pending[0])
            await _drain(relay)
            assert wecom.sent_markdowns == [("chat-1", "[M] Alice sent cat.png")]
            assert len(wecom.sent_media) == 1
        finally:
            await relay.stop()
            store.close()

    asyncio.run(scenario())
