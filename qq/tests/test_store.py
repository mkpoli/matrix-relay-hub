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
    assert store.seen_transaction("t1") is False
    assert store.seen_transaction("t1") is True
    store.close()


def test_face_cache(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    assert store.face_mxc(0) is None
    store.set_face(0, "mxc://example.org/face")
    assert store.face_mxc(0) == "mxc://example.org/face"
    store.close()
