from qq_bridge.formatting import (
    CustomEmoji,
    chunk,
    extract_custom_emojis,
    extract_face_ids,
    format_reaction_key,
    render_qq_text,
    replace_faces,
    strip_leading_mentions,
    strip_reply_fallback,
    utf16_length,
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


def test_bot_mention_is_left_out():
    mentions = [
        {"member_openid": "B0T", "username": "Glyph Atlas Matrix", "is_you": True},
        {"member_openid": "OTHER", "username": "Helper", "bot": True},
        {"member_openid": "M1", "username": "小明"},
    ]
    body, html = render_qq_text("<@B0T> 是 <@OTHER>，<@M1>", mentions, "example.org")
    assert body == "是 ，@小明"
    assert "Glyph Atlas" not in html and "Helper" not in html
    assert render_qq_text("<@B0T>", mentions, "example.org") == ("", "")
    assert render_qq_text("看这个 <@B0T>\n\n下一段", mentions, "example.org")[0] == "看这个 \n\n下一段"


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


def test_a_message_that_is_a_quote_is_kept():
    body = "> 자음\n> \n> ㄱ  1획\n> ㅋ  2획  *ㄱ에 1획을 더한 글자\n\n"
    assert strip_reply_fallback(body) == body


def test_a_crlf_or_indented_reply_fallback_is_dropped():
    assert strip_reply_fallback("> <@a:b.org> hi\r\n\r\nreply") == "reply"
    assert strip_reply_fallback(" > <@a:b.org> hi\n\nreply") == "reply"


def test_an_emote_reply_fallback_is_dropped():
    assert strip_reply_fallback("> * <@alice:example.org> waves\n\nhi") == "hi"


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


def test_is_edit():
    from qq_bridge.relay import is_edit

    assert is_edit({"m.new_content": {"body": "new"}}) is True
    assert is_edit({"m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"}}) is True
    assert is_edit({"body": "hello"}) is False
    assert is_edit({"body": "hi", "m.relates_to": {"rel_type": "m.thread"}}) is False


def test_face_markup_becomes_emoji():
    raw = '<faceType=1,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    assert replace_faces(raw) == "😲"


def test_face_markup_uses_ext_text():
    raw = '<faceType=4,faceId="",ext="eyJ0ZXh0IjoiW+a7oeWktOmXruWPt10ifQ==">'
    assert replace_faces(raw) == "[满头问号]"


def test_face_name_without_brackets_gains_them():
    raw = '<faceType=6,faceId="14",ext="eyJ0ZXh0Ijoi5pac55y856yRIn0=">'
    assert replace_faces(raw) == "[斜眼笑]"


def test_unknown_face_is_labelled():
    assert replace_faces('<faceType=6,faceId="999",ext="eyJ0ZXh0IjoiIn0=">') == "[表情]"


def test_face_survives_render():
    body, _ = render_qq_text('<faceType=1,faceId="0",ext="eyJ0ZXh0IjoiIn0="> 你好', [], "example.org")
    assert body == "😲 你好"


def test_face_ids_are_collected():
    raw = 'a <faceType=1,faceId="0",ext="eyJ0ZXh0IjoiIn0="> b <faceType=1,faceId="12",ext="">'
    assert extract_face_ids(raw) == [0, 12]


def test_sticker_ids_are_not_collected():
    raw = '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    assert extract_face_ids(raw) == []


def test_face_with_image_becomes_inline_emoticon():
    raw = '<faceType=1,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    body, html = render_qq_text(raw, [], "example.org", {0: "mxc://example.org/face"})
    assert body == "😲"
    assert 'data-mx-emoticon' in html
    assert 'src="mxc://example.org/face"' in html
    assert 'alt="😲"' in html
    assert html.endswith("😲")


def test_face_without_image_stays_text():
    raw = '<faceType=1,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    body, html = render_qq_text(raw, [], "example.org")
    assert body == "😲"
    assert "<img" not in html


def test_sticker_with_an_image_has_no_text_fallback():
    raw = '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    body, html = render_qq_text(raw, [], "example.org", sticker_image=True)
    assert body == ""
    assert html == ""


def test_sticker_without_a_name_is_not_an_emoji():
    raw = '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    assert replace_faces(raw) == "[表情]"


def test_sticker_name_is_kept_without_an_image():
    raw = '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiW+Wkp+iDhl0ifQ==">'
    body, html = render_qq_text(raw, [], "example.org")
    assert body == "[大胆]"
    assert html == "[大胆]"


def test_sticker_does_not_borrow_a_system_face_image():
    raw = (
        '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0="> '
        '<faceType=1,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
    )
    body, html = render_qq_text(
        raw, [], "example.org", {0: "mxc://example.org/face"}
    )
    assert body == "[表情] 😲"
    assert html.count("<img") == 1
    assert 'alt="😲"' in html


def test_sticker_text_does_not_eat_surrounding_text():
    raw = '看这个 <faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0="> 好耶'
    body, html = render_qq_text(raw, [], "example.org", sticker_image=True)
    assert body == "看这个  好耶"
    assert "😲" not in body
    assert "<img" not in html


def test_format_reaction_key_handles_emojis_shortcodes_and_mxc():
    assert format_reaction_key("👍") == "👍"
    assert format_reaction_key("  ❤️\n ") == "❤️"
    assert format_reaction_key("  ") == ""
    assert format_reaction_key(None) == ""
    assert format_reaction_key(":party_blob:") == ":party_blob:"
    assert format_reaction_key("mxc://example.org/abc") == "[custom emoji]"
    assert (
        format_reaction_key(
            "mxc://example.org/abc",
            relates={"shortcode": ":party_blob:"},
        )
        == ":party_blob:"
    )
    assert (
        format_reaction_key(
            "mxc://example.org/abc",
            content={"shortcode": "blob"},
        )
        == ":blob:"
    )
    assert (
        format_reaction_key(
            "mxc://example.org/abc",
            content={"com.beeper.reaction.shortcode": ":kusa_lol:"},
        )
        == ":kusa_lol:"
    )
    assert (
        format_reaction_key(
            "mxc://example.org/abc",
            content={"fi.mau.discord.reaction": {"name": "kusa_lol", "id": "123"}},
        )
        == ":kusa_lol:"
    )
    assert format_reaction_key("x" * 150) == ("x" * 100) + "..."


def test_extract_custom_emojis_inline_discord_payload():
    html_body = (
        'tte，何時加的→<img data-mx-emoticon '
        'src="mxc://glyphatlas.org/H4m00AqFfV3mR7dDaaExFXPOxZgmq24Q" '
        'alt=":fumo_reimu_lying:" title=":fumo_reimu_lying:" height="32"/>'
    )
    text, emojis, emoji_only = extract_custom_emojis(html_body)
    assert text == "tte，何時加的→:fumo_reimu_lying:"
    assert emojis == [
        CustomEmoji(
            mxc="mxc://glyphatlas.org/H4m00AqFfV3mR7dDaaExFXPOxZgmq24Q",
            shortcode=":fumo_reimu_lying:",
            animated=False,
        )
    ]
    assert emoji_only is False


def test_extract_custom_emojis_emoji_only_and_animated():
    html_body = (
        '<img data-mx-emoticon data-mau-animated-emoji '
        'src="mxc://example.org/a1" alt=":spin:" title=":spin:" height="32"/> '
        '<img data-mx-emoticon data-mau-animated-emoji '
        'src="mxc://example.org/a1" alt=":spin:" title=":spin:" height="32"/>'
    )
    text, emojis, emoji_only = extract_custom_emojis(html_body)
    assert text == ":spin: :spin:"
    assert emojis == [
        CustomEmoji(mxc="mxc://example.org/a1", shortcode=":spin:", animated=True)
    ]
    assert emoji_only is True


def test_extract_custom_emojis_ignores_mx_reply_and_author_icon():
    html_body = (
        '<mx-reply><blockquote><a href="https://matrix.to/#/!r/$e">In reply to</a> '
        '<img data-mx-emoticon src="mxc://example.org/quoted" alt=":old:"/>'
        "</blockquote></mx-reply>"
        '<p class="discord-embed-author">'
        '<img data-mx-emoticon height="24" src="mxc://example.org/avatar" title="Author icon" alt="">'
        "<span>Author</span></p>"
        'hello <img data-mx-emoticon src="mxc://example.org/new" alt=":wave:"/>'
    )
    text, emojis, emoji_only = extract_custom_emojis(html_body)
    assert emojis == [
        CustomEmoji(mxc="mxc://example.org/new", shortcode=":wave:", animated=False)
    ]
    assert ":old:" not in text
    assert ":wave:" in text
    assert emoji_only is False


def test_extract_custom_emojis_ignores_only_author_icon():
    html_body = (
        '<p class="discord-embed-author">'
        '<img data-mx-emoticon height="24" src="mxc://example.org/avatar" title="Author icon" alt="">'
        "<span>Author</span></p>"
    )
    assert extract_custom_emojis(html_body) == ("", [], False)


def test_extract_custom_emojis_deduplicates_adjacent_alt_and_keeps_links():
    html_body = (
        'see <a href="https://example.com/x">link</a> '
        'and <a href="https://matrix.to/#/!room:example.org/$ev">original</a> '
        'by <a href="https://matrix.to/#/@alice:example.org">Alice</a> '
        'and <a href="https://matrix.to/#/%40bob%3Aexample.org">Bob</a> '
        '<img data-mx-emoticon src="mxc://example.org/f" alt="[变形]"/>[变形]'
    )
    text, emojis, emoji_only = extract_custom_emojis(html_body)
    assert (
        text
        == "see link (https://example.com/x) and original (https://matrix.to/#/!room:example.org/$ev) by Alice and Bob [变形]"
    )
    assert len(emojis) == 1
    assert emoji_only is False


def test_chunk_measures_utf16_code_units():
    text = "😀" * 120
    parts = chunk(text, limit=100)
    assert "".join(parts) == text
    assert all(utf16_length(part) <= 100 for part in parts)


def test_chunk_leaves_a_character_that_would_pass_the_limit():
    text = "a" * 1999 + "😀"
    assert chunk(text, limit=2000) == ["a" * 1999, "😀"]


def test_qq_forwarded_chat_record_deduplicates_doubled_messages_and_groups_sender():
    raw = (
        "[群聊的聊天记录]\n"
        "=== 消息 1 ===\n"
        "[消息内容] 这个的主要问题是今昔文字镜作为一个glyph集\n"
        "[发送者] baysoftware\n\n"
        "=== 消息 2 ===\n"
        "[消息内容] 它的一些字真的只是glyph\n"
        "[发送者] baysoftware\n\n"
        "=== 消息 3 ===\n"
        "[消息内容] 这个的主要问题是今昔文字镜作为一个glyph集\n"
        "[发送者] baysoftware\n\n"
        "=== 消息 4 ===\n"
        "[消息内容] 它的一些字真的只是glyph\n"
        "[发送者] baysoftware\n"
    )
    body, html = render_qq_text(raw, [], "example.org")
    assert body == (
        "[群聊的聊天记录]\n"
        "> baysoftware:\n"
        "> 这个的主要问题是今昔文字镜作为一个glyph集\n"
        "> 它的一些字真的只是glyph"
    )
    assert html == (
        "<p>[群聊的聊天记录]</p>"
        "<blockquote><p><strong>baysoftware</strong>:<br/>"
        "这个的主要问题是今昔文字镜作为一个glyph集<br/>"
        "它的一些字真的只是glyph</p></blockquote>"
    )


def test_qq_forwarded_chat_record_formats_multiple_senders_and_inline_faces():
    raw = (
        "[群聊的聊天记录]\n"
        "=== 消息 1 ===\n"
        "[消息内容] 第一句\n第二行\n"
        "[发送者] Alice\n\n"
        "=== 消息 2 ===\n"
        '[消息内容] <faceType=1,faceId="0",ext="eyJ0ZXh0IjoiIn0="> 同意\n'
        "[发送者] Bob\n"
    )
    body, html = render_qq_text(raw, [], "example.org")
    assert body == (
        "[群聊的聊天记录]\n"
        "> Alice:\n"
        "> 第一句\n"
        "> 第二行\n"
        ">\n"
        "> Bob: 😲 同意"
    )
    assert "<strong>Alice</strong>:<br/>第一句<br/>第二行" in html
    assert "<strong>Bob</strong>: 😲 同意" in html
    assert "=== 消息" not in body


def test_qq_forwarded_chat_record_preserves_distinct_messages_and_ignores_plain_text():
    distinct = (
        "[群聊的聊天记录]\n"
        "=== 消息 1 ===\n"
        "[消息内容] 早上好\n"
        "[发送者] Alice\n\n"
        "=== 消息 2 ===\n"
        "[消息内容] 晚安\n"
        "[发送者] Alice\n"
    )
    body, _ = render_qq_text(distinct, [], "example.org")
    assert body == "[群聊的聊天记录]\n> Alice:\n> 早上好\n> 晚安"

    not_forward = "[群聊的聊天记录]\n这不是转发结构"
    plain, _ = render_qq_text(not_forward, [], "example.org")
    assert plain == not_forward


def test_qq_forwarded_chat_record_edge_cases():
    # Different timestamps prevent deduplicating genuine repeated messages.
    with_times = (
        "[群聊的聊天记录]\n"
        "=== 消息 1 ===\n[消息内容] hi\n[发送者] Alice\n[时间] 09:00\n\n"
        "=== 消息 2 ===\n[消息内容] hi\n[发送者] Alice\n[时间] 10:00\n"
    )
    body, _ = render_qq_text(with_times, [], "example.org")
    assert body == "[群聊的聊天记录]\n> Alice:\n> hi\n> hi"

    # Literal [发送者] inside multiline content is preserved.
    literal_tag = (
        "[群聊的聊天记录]\n"
        "=== 消息 1 ===\n[消息内容] first\n[发送者] literal\n[发送者] Alice\n"
    )
    body, _ = render_qq_text(literal_tag, [], "example.org")
    assert body == "[群聊的聊天记录]\n> Alice:\n> first\n> [发送者] literal"

    # Nested forward / literal `=== 消息 N ===` bails out instead of misattributing.
    nested = (
        "[群聊的聊天记录]\n"
        "=== 消息 1 ===\n[消息内容] [群聊的聊天记录]\n=== 消息 1 ===\n[消息内容] inner\n[发送者] Bob\n[发送者] Alice\n"
    )
    body, _ = render_qq_text(nested, [], "example.org")
    assert "=== 消息 1 ===" in body

    # Multiline leading bot mentions are stripped before detecting the forward.
    bot_prefixed = (
        "<@B1>\n<@B2>\n[群聊的聊天记录]\n"
        "=== 消息 1 ===\n[消息内容] hi\n[发送者] Alice\n"
    )
    body, _ = render_qq_text(
        bot_prefixed,
        [{"id": "B1", "is_you": True}, {"id": "B2", "bot": True}],
        "example.org",
    )
    assert body == "[群聊的聊天记录]\n> Alice: hi"

    # Attached sticker uses [图片] placeholder instead of duplicating [表情].
    sticker_fwd = (
        "[群聊的聊天记录]\n"
        '=== 消息 1 ===\n[消息内容] <faceType=6,faceId="0",ext="">\n[发送者] Alice\n'
    )
    body, _ = render_qq_text(sticker_fwd, [], "example.org", sticker_image=True)
    assert body == "[群聊的聊天记录]\n> Alice: [图片]"


def test_qq_forwarded_chat_record_deduplicates_volatile_rkey_attachments_and_strips_urls():
    from qq_bridge.formatting import extract_forwarded_attachments, is_trusted_qq_media_url

    raw = (
        "[群聊的聊天记录]\n"
        "=== 消息 1 ===\n"
        "[消息内容] 又是没法避免，汉大第一版检字表字形和正文字头字形不符的情况，𠋾\n"
        "[发送者] 玄晖allen\n"
        "[附件1] 类型:图片 文件名:424C880940CE2A22930A4C581C9E268C.jpg 尺寸:1084x2087 大小:555.6KB "
        "URL:https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=FILE1&rkey=KEY_A&spec=0\n"
        "[附件2] 类型:图片 文件名:E855CB976A8DD1EBA9F12C1D8ED78EFE.jpg 尺寸:1205x670 大小:125.2KB "
        "URL:https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=FILE2&rkey=KEY_B&spec=0\n\n"
        "=== 消息 2 ===\n"
        "[消息内容] 又是没法避免，汉大第一版检字表字形和正文字头字形不符的情况，𠋾\n"
        "[发送者] 玄晖allen\n"
        "[附件1] 类型:图片 文件名:424C880940CE2A22930A4C581C9E268C.jpg 尺寸:1084x2087 大小:555.6KB "
        "URL:https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=FILE1&rkey=KEY_C&spec=0\n"
        "[附件2] 类型:图片 文件名:E855CB976A8DD1EBA9F12C1D8ED78EFE.jpg 尺寸:1205x670 大小:125.2KB "
        "URL:https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=FILE2&rkey=KEY_D&spec=0\n"
    )
    body, html = render_qq_text(raw, [], "example.org")
    assert body == (
        "[群聊的聊天记录]\n"
        "> 玄晖allen: 又是没法避免，汉大第一版检字表字形和正文字头字形不符的情况，𠋾"
    )
    assert "multimedia.nt.qq.com.cn" not in body
    assert "multimedia.nt.qq.com.cn" not in html

    atts = extract_forwarded_attachments(raw)
    assert len(atts) == 2
    assert atts[0]["filename"] == "424C880940CE2A22930A4C581C9E268C.jpg"
    assert atts[0]["width"] == 1084 and atts[0]["height"] == 2087
    assert atts[0]["content_type"] == "image/jpeg"
    assert atts[0]["trusted_qq_media"] is True
    assert atts[1]["filename"] == "E855CB976A8DD1EBA9F12C1D8ED78EFE.jpg"

    # Untrusted URLs are never extracted for download (SSRF guard) and render as safe labels.
    assert is_trusted_qq_media_url("https://multimedia.nt.qq.com.cn/download?fileid=1")
    assert not is_trusted_qq_media_url("http://multimedia.nt.qq.com.cn/download?fileid=1")
    assert not is_trusted_qq_media_url("https://127.0.0.1/secret.jpg")
    assert not is_trusted_qq_media_url("https://evil.example.com/nt.qq.com.cn/a.jpg")
    untrusted_raw = (
        "[群聊的聊天记录]\n"
        "=== 消息 1 ===\n"
        "[消息内容] look\n"
        "[发送者] Eve\n"
        "[附件1] 类型:图片 文件名:x.jpg URL:https://127.0.0.1/secret.jpg\n"
    )
    assert extract_forwarded_attachments(untrusted_raw) == []
    ubody, _ = render_qq_text(untrusted_raw, [], "example.org")
    assert "127.0.0.1" not in ubody
    assert "[图片: x.jpg]" in ubody

    # Mixed valid + URL-less trailing attachment does not leak rkey, malformed port does not crash,
    # query params do not collide, and attachments beyond cap 16 keep filename labels.
    from qq_bridge.formatting import normalize_media_url_key

    assert not is_trusted_qq_media_url("https://qpic.cn:badport/a.jpg")
    assert normalize_media_url_key("https://qpic.cn/f?id=one%26page%3Dtwo") != normalize_media_url_key(
        "https://qpic.cn/f?id=one&page=two"
    )
    mixed = (
        "[群聊的聊天记录]\n=== 消息 1 ===\n[消息内容] hi\n[发送者] Alice\n"
        "[附件1] 类型:图片 文件名:one.jpg URL:https://qpic.cn/one?rkey=secret\n"
        "[附件2] 类型:文件 文件名:missing.txt\n"
    )
    assert len(extract_forwarded_attachments(mixed)) == 1
    mbody, _ = render_qq_text(mixed, [], "example.org")
    assert "rkey=secret" not in mbody and "[文件: missing.txt]" in mbody

    many = "[群聊的聊天记录]\n=== 消息 1 ===\n[消息内容] hi\n[发送者] Alice\n" + "".join(
        f"[附件{i}] 类型:图片 文件名:{i}.jpg URL:https://qpic.cn/{i}\n" for i in range(1, 18)
    )
    assert len(extract_forwarded_attachments(many)) == 16
    assert "[图片: 17.jpg]" in render_qq_text(many, [], "example.org")[0]
