from qq_bridge.sources import source_tag


def test_puppets_and_bots_carry_their_network():
    assert source_tag("@discord_1089404483068506122:glyphatlas.org") == "D"
    assert source_tag("@telegram_8954644733:glyphatlas.org") == "T"
    assert source_tag("@irc_alice:glyphatlas.org") == "I"
    assert source_tag("@qq_ABC123:glyphatlas.org") == "Q"
    assert source_tag("@wecom_zhangsan:glyphatlas.org") == "W"
    assert source_tag("@discordbot:glyphatlas.org") == "D"
    assert source_tag("@telegrambot:glyphatlas.org") == "T"
    assert source_tag("@heisenbridge:glyphatlas.org") == "I"
    assert source_tag("@qqbot:glyphatlas.org") == "Q"
    assert source_tag("@wecombot:glyphatlas.org") == "W"


def test_a_person_on_matrix_is_matrix():
    assert source_tag("@mkpoli:glyphatlas.org") == "M"
    # A local part that merely starts with a network name is still a person.
    assert source_tag("@ircfan:glyphatlas.org") == "M"
