from wecom_bridge.formatting import (
    chunk_utf8,
    format_reaction_key,
    ghost_localpart,
    parse_content_disposition,
    render_matrix_with_quote,
    sanitize_wecom_markdown,
    strip_bot_mention,
    strip_reply_fallback,
)
from wecom_bridge.sources import is_network_bot, source_tag


def test_chunk_utf8_respects_byte_limit_for_cjk():
    text = "你好世界" * 200  # 2400 UTF-8 bytes
    parts = chunk_utf8(text, max_bytes=500)
    assert len(parts) >= 5
    assert "".join(parts) == text
    assert all(len(p.encode("utf-8")) <= 500 for p in parts)


def test_strip_bot_mention_handles_four_per_em_space_and_dm():
    assert strip_bot_mention("@RelayBot\u2005hello world", "RelayBot", is_group=True) == "hello world"
    assert strip_bot_mention("@AnyBot hello", "", is_group=True) == "hello"
    assert strip_bot_mention("@alice keep in dm", "RelayBot", is_group=False) == "@alice keep in dm"


def test_strip_reply_fallback_preserves_markdown_quotes():
    reply = "> <@alice:example.org> hi\n> line 2\n\nactual reply"
    assert strip_reply_fallback(reply) == "actual reply"
    real_quote = "> quoted wisdom\n\ncomment"
    assert strip_reply_fallback(real_quote) == real_quote


def test_ghost_localpart_avoids_collisions():
    assert ghost_localpart("zhangsan") == "wecom_zhangsan"
    upper = ghost_localpart("ZhangSan")
    lower = ghost_localpart("zhangsan")
    assert upper != lower
    assert upper.startswith("wecom_zhangsan_")
    assert ghost_localpart("a@b") != ghost_localpart("a_b")


def test_render_matrix_with_quote():
    plain, html_body = render_matrix_with_quote(
        "sounds good",
        {"msgtype": "text", "text": {"content": "meet at 3?\nbring notes"}},
    )
    assert plain == "> meet at 3?\n> bring notes\n\nsounds good"
    assert html_body == "<blockquote>meet at 3?<br/>bring notes</blockquote>sounds good"


def test_parse_content_disposition_prevents_traversal():
    assert parse_content_disposition("attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95.pdf") == "测试.pdf"
    assert parse_content_disposition('attachment; filename="../../etc/passwd"') == "passwd"
    assert parse_content_disposition('attachment; filename="C:\\temp\\report.docx"') == "report.docx"
    assert parse_content_disposition(None) is None


def test_format_reaction_key_and_source_tags():
    assert format_reaction_key("👍") == "👍"
    assert (
        format_reaction_key(
            "mxc://example.org/abc",
            content={"fi.mau.discord.reaction": {"name": "party_blob"}},
        )
        == ":party_blob:"
    )
    assert source_tag("@wecom_alice:example.org") == "W"
    assert source_tag("@wecombot:example.org") == "W"
    assert source_tag("@appservice-irc:example.org") == "I"
    assert source_tag("@qq_123:example.org") == "Q"
    assert source_tag("@alice:example.org") == "M"
    assert is_network_bot("@heisenbridge:example.org") is True
    assert is_network_bot("@appservice-irc:example.org") is True
    assert is_network_bot("@discordbot:example.org") is True
    assert is_network_bot("@telegrambot:example.org") is True
    assert is_network_bot("@qqbot:example.org") is True
    assert is_network_bot("@wecombot:example.org") is True
    assert is_network_bot("@discord_42:example.org") is False
    assert is_network_bot("@alice:example.org") is False


def test_sanitize_wecom_markdown_neutralizes_mentions_and_font_tags():
    sanitized = sanitize_wecom_markdown('hi <@all> <font color="warning">alert</font> a < b')
    assert "<@all>" not in sanitized
    assert "<font" not in sanitized
    assert "</font>" not in sanitized
    assert "a < b" in sanitized
