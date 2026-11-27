from qq_bridge.sources import is_network_bot, source_tag


def test_puppets_and_bots_carry_their_network():
    assert source_tag("@discord_1089404483068506122:glyphatlas.org") == "D"
    assert source_tag("@telegram_8954644733:glyphatlas.org") == "T"
    assert source_tag("@irc_alice:glyphatlas.org") == "I"
    assert source_tag("@qq_ABC123:glyphatlas.org") == "Q"
    assert source_tag("@wecom_zhangsan:glyphatlas.org") == "W"
    assert source_tag("@discordbot:glyphatlas.org") == "D"
    assert source_tag("@telegrambot:glyphatlas.org") == "T"
    assert source_tag("@heisenbridge:glyphatlas.org") == "I"
    assert source_tag("@appservice-irc:glyphatlas.org") == "I"
    assert source_tag("@qqbot:glyphatlas.org") == "Q"
    assert source_tag("@wecombot:glyphatlas.org") == "W"


def test_a_person_on_matrix_is_matrix():
    assert source_tag("@mkpoli:glyphatlas.org") == "M"
    # A local part that merely starts with a network name is still a person.
    assert source_tag("@ircfan:glyphatlas.org") == "M"


def test_is_network_bot_distinguishes_bridge_bots_from_users_and_puppets():
    for bot in (
        "@heisenbridge:glyphatlas.org",
        "@appservice-irc:glyphatlas.org",
        "@discordbot:glyphatlas.org",
        "@telegrambot:glyphatlas.org",
        "@qqbot:glyphatlas.org",
        "@wecombot:glyphatlas.org",
    ):
        assert is_network_bot(bot) is True
    for user in (
        "@mkpoli:glyphatlas.org",
        "@discord_1089404483068506122:glyphatlas.org",
        "@telegram_8954644733:glyphatlas.org",
        "@irc_alice:glyphatlas.org",
        "@qq_ABC123:glyphatlas.org",
        "@wecom_zhangsan:glyphatlas.org",
    ):
        assert is_network_bot(user) is False
