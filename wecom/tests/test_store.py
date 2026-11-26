from pathlib import Path

from wecom_bridge.store import Store


def test_store_transactions_pending_and_dedup(tmp_path: Path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        ev = {"event_id": "$1", "type": "m.room.message"}
        seqs = store.accept_transaction("txn-1", [ev])
        assert seqs is not None and len(seqs) == 1
        assert store.accept_transaction("txn-1", [ev]) is None

        claimed = store.claim_pending(max_age=3600, max_attempts=2)
        assert len(claimed) == 1
        assert claimed[0][1]["event_id"] == "$1"

        store.finish_pending(claimed[0][0])
        assert store.claim_pending(max_age=3600, max_attempts=2) == []

        assert store.claim_inbound("msg-1", "chat-1", "!r:ex") is True
        assert store.claim_inbound("msg-1", "chat-1", "!r:ex") is False
        store.release_inbound("msg-1")
        assert store.claim_inbound("msg-1", "chat-1", "!r:ex") is True
        store.set_inbound_event("msg-1", "$m1")
        store.release_inbound("msg-1")  # must not delete once event_id is recorded
        assert store.claim_inbound("msg-1", "chat-1", "!r:ex") is False

        store.supersede("$1", "edited")
        assert store.superseded("$1") == "edited"
        store.supersede("$1", "redacted")
        store.supersede("$1", "edited")
        assert store.superseded("$1") == "redacted"
    finally:
        store.close()
