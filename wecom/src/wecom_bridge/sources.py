"""The network a Matrix ID speaks for, as the letter shown before a relayed line."""

from __future__ import annotations

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
    "appservice-irc": "I",
    "qqbot": "Q",
    "wecombot": "W",
}


def _localpart(mxid: str) -> str:
    return mxid.removeprefix("@").split(":", 1)[0]


def is_network_bot(mxid: str) -> bool:
    """Whether a Matrix ID is a bridge's own bot rather than a person or puppet."""
    return _localpart(mxid) in NETWORK_BOTS


def source_tag(mxid: str) -> str:
    """The one-letter tag of the network a Matrix ID belongs to."""
    local = _localpart(mxid)
    if local in NETWORK_BOTS:
        return NETWORK_BOTS[local]
    for prefix, tag in NETWORK_PREFIXES:
        if local.startswith(prefix):
            return tag
    return "M"
