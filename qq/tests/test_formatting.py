from qq_bridge.formatting import (
    chunk,
    extract_face_ids,
    render_qq_text,
    replace_faces,
    strip_leading_mentions,
    strip_reply_fallback,
)


def test_mention_becomes_pill():
    body, html = render_qq_text(
        "你好 <@7A3B9C1D5E2F4A6B8C0D1E3F5A7B9C2D>",
        [{"member_openid": "7A3B9C1D5E2F4A6B8C0D1E3F5A7B9C2D", "username": "小明"}],
        "example.org",
    )
    assert body == "你好 @小明"
    assert "matrix.to/#/@qq_7a3b9c1d5e2f4a6b8c0d1e3f5a7b9c2d:example.org" in html
    assert "@小明" in html


def test_mention_all():
    body, html = render_qq_text("<@all> 开会", [], "example.org")
    assert body == "@全体成员 开会"
    assert "<strong>@全体成员</strong>" in html


def test_unknown_mention_uses_openid_prefix():
    body, _ = render_qq_text("<@DEADBEEF>", [], "example.org")
    assert body == "@DEADBEEF"


def test_newlines_become_breaks():
    body, html = render_qq_text("one\ntwo", [], "example.org")
    assert body == "one\ntwo"
    assert html == "one<br/>two"


def test_html_is_escaped():
    _, html = render_qq_text("<script>x</script> & <@all>", [], "example.org")
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


def test_strip_reply_fallback():
    body = "> <@alice:example.org> hello\n\nworld"
    assert strip_reply_fallback(body) == "world"


def test_strip_reply_fallback_leaves_quotes_in_text():
    body = ">not a fallback\nstill here"
    assert strip_reply_fallback(body) == body


def test_chunk_keeps_all_text():
    text = "a" * 100 + "\n" + "b" * 100
    parts = chunk(text, limit=120)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
    assert all(len(part) <= 120 for part in parts)


def test_strip_leading_mentions():
    assert strip_leading_mentions("<@ABC> !qq bind test") == "!qq bind test"
    assert strip_leading_mentions(" <@ABC>  <@DEF> hello") == "hello"
    assert strip_leading_mentions("@机器人1906093836 !qq id") == "!qq id"
    assert strip_leading_mentions("!qq id") == "!qq id"


def test_edit_target():
    from qq_bridge.relay import edit_target

    assert (
        edit_target({"m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"}})
        == "$orig"
    )
    assert edit_target({"m.new_content": {"body": "new"}}) is None
    assert edit_target({"body": "hello"}) is None
    assert (
        edit_target({"body": "hi", "m.relates_to": {"rel_type": "m.thread"}}) is None
    )


def test_face_markup_becomes_emoji():
    raw = '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    assert replace_faces(raw) == "😲"


def test_face_markup_uses_ext_text():
    raw = '<faceType=4,faceId="",ext="eyJ0ZXh0IjoiW+a7oeWktOmXruWPt10ifQ==">'
    assert replace_faces(raw) == "[满头问号]"


def test_unknown_face_is_labelled():
    assert replace_faces('<faceType=6,faceId="999",ext="eyJ0ZXh0IjoiIn0=">') == "[表情]"


def test_face_survives_render():
    body, _ = render_qq_text('<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0="> 你好', [], "example.org")
    assert body == "😲 你好"


def test_face_ids_are_collected():
    raw = 'a <faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0="> b <faceType=6,faceId="12",ext="">'
    assert extract_face_ids(raw) == [0, 12]


def test_face_with_image_becomes_inline_emoticon():
    raw = '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    body, html = render_qq_text(raw, [], "example.org", {0: "mxc://example.org/face"})
    assert body == "😲"
    assert 'data-mx-emoticon' in html
    assert 'src="mxc://example.org/face"' in html
    assert 'alt="😲"' in html
    assert html.endswith("😲")


def test_face_without_image_stays_text():
    raw = '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    body, html = render_qq_text(raw, [], "example.org")
    assert body == "😲"
    assert "<img" not in html
