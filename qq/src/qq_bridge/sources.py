"""The network a Matrix ID speaks for, as the letter the QQ group shows before a line."""

from __future__ import annotations

# A bridge puppet carries its network in the local part of its Matrix ID:
# @discord_123, @telegram_123, @irc_nick, @qq_openid, @wecom_userid. A bridge's
# own bot is named for the network too, and anything else is a person on Matrix.
NETWORK_PREFIXES = (
    ("discord_", "D"),
    ("telegram_", "T"),
    ("irc_", "I"),
    ("qq_", "Q"),
    ("wecom_", "W"),
)
NETWORK_BOTS = {
    "discordbot": "D",
    "telegrambot": "T",
    "heisenbridge": "I",
    "qqbot": "Q",
    "wecombot": "W",
}


def source_tag(mxid: str) -> str:
    """The one-letter tag of the network a Matrix ID belongs to."""
    local = mxid.removeprefix("@").split(":", 1)[0]
    if local in NETWORK_BOTS:
        return NETWORK_BOTS[local]
    for prefix, tag in NETWORK_PREFIXES:
        if local.startswith(prefix):
            return tag
    return "M"
