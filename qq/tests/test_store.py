from qq_bridge.store import Store


def test_bind_and_lookup(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.bind("!room:example.org", "GROUP1", "welcome")
    assert store.room_for_group("GROUP1") == "!room:example.org"
    assert store.group_for_room("!room:example.org") == "GROUP1"
    assert store.room_for_code("welcome") is None
    store.close()


def test_code_binding(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.set_code("!room:example.org", "welcome")
    assert store.room_for_code("welcome") == "!room:example.org"
    assert store.room_for_group("GROUP1") is None
    store.bind("!room:example.org", "GROUP1", "welcome")
    assert store.room_for_code("welcome") is None
    store.close()


def test_members_and_messages(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.set_member("G", "M", "小明", "member")
    assert store.member_name("G", "M") == "小明"
    store.add_message("Q1", "G", "!r:x", "$e", "REF1", "qq")
    assert store.event_for_ref("G", "REF1") == "$e"
    assert store.event_for_qq("Q1") == "$e"
    store.close()


def test_editions_track_the_latest_copy(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.add_message("Q1", "G", "!r:x", "$e", "REFIDX_1", "matrix")
    store.add_message("Q2", "G", "!r:x", "$e", "REFIDX_2", "matrix")
    assert [row["qq_id"] for row in store.messages_for_event("$e")] == ["Q1", "Q2"]
    assert store.reference_for_event("$e") == "REFIDX_2"
    store.remove_message("Q2")
    assert store.reference_for_event("$e") == "REFIDX_1"
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
