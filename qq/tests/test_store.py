from qq_bridge.store import Store


def test_members_and_messages(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.set_member("G", "M", "小明", "member")
    assert store.member_name("G", "M") == "小明"
    store.add_message("Q1", "G", "!r:x", "$e", "REF1", "qq")
    assert store.event_for_ref("G", "REF1") == "$e"
    assert store.event_for_qq("Q1") == "$e"
    assert store.qq_for_event("$e") is None
    store.add_message("Q2", "G", "!r:x", "$e2", "REF2", "matrix")
    assert store.qq_for_event("$e2")["qq_id"] == "Q2"
    store.close()


def test_replayed_history_is_recorded_once(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    assert store.relayed_count() == 0
    store.mark_relayed("$e", "QB", "G", "!r:x")
    assert store.relayed_qq("$e") == "QB"
    assert store.relayed_count() == 1
    store.mark_relayed("$e", "QC", "G", "!r:x")
    assert store.relayed_qq("$e") == "QB"
    store.close()


def test_transaction_dedup(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    assert store.accept_transaction("t1", [{"event_id": "$a"}]) == [1]
    assert store.accept_transaction("t1", [{"event_id": "$a"}]) is None
    assert store.claim_pending(max_age=3600, max_attempts=3) == [(1, {"event_id": "$a"})]
    store.close()


def test_face_cache(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    assert store.face_mxc(0) is None
    store.set_face(0, "mxc://example.org/face")
    assert store.face_mxc(0) == "mxc://example.org/face"
    store.close()


def test_pending_events_are_claimed_until_finished_or_given_up(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    first, second, third = store.accept_transaction(
        "t", [{"event_id": "$a"}, {"event_id": "$b"}, {"event_id": "$c"}]
    )
    store.finish_pending(first)
    claim = lambda **kw: [seq for seq, _ in store.claim_pending(max_age=3600, max_attempts=1, **kw)]
    assert claim(busy={third}) == [second]
    assert claim(idle=60) == []  # third arrived just now
    assert store.dropped_pending == 1  # second has used its one attempt
    assert claim() == [third]
    store.conn.execute("UPDATE pending SET ts = ts - 7200")
    assert claim() == []
    assert store.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    store.close()
