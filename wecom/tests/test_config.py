from pathlib import Path

import pytest

from wecom_bridge.config import ConfigError, load

BASE_AIBOT = """
[matrix]
homeserver = "http://hs:8008/"
domain = "example.org"
as_token = "as"
hs_token = "hs"
bot_mxid = "@wecombot:example.org"

[wecom]
mode = "aibot"
bot_id = "bot-1"
secret = "sec-1"
chat_id = "chat-1"
incoming = "#main:example.org"

[members]
zhangsan = "Zhang San"

[[room]]
alias = "#main:example.org"
label = "main"
"""


def test_load_aibot_config(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(BASE_AIBOT, encoding="utf-8")
    cfg = load(path)
    assert cfg.matrix.homeserver == "http://hs:8008"
    assert cfg.matrix.listen_port == 29336
    assert cfg.wecom.mode == "aibot"
    assert cfg.wecom.aibot_enabled is True
    assert cfg.wecom.can_send is True
    assert cfg.members == {"zhangsan": "Zhang San"}
    assert cfg.state_path == tmp_path / "state.sqlite3"


def test_load_webhook_config_via_url_and_key(tmp_path: Path):
    path = tmp_path / "webhook.toml"
    path.write_text(
        """
[matrix]
homeserver = "http://hs:8008"
domain = "example.org"
as_token = "as"
hs_token = "hs"
bot_mxid = "@wecombot:example.org"

[wecom]
mode = "webhook"
webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"

[[room]]
alias = "#main:example.org"
label = "main"
""",
        encoding="utf-8",
    )
    cfg = load(path)
    assert cfg.wecom.mode == "webhook"
    assert cfg.wecom.can_send is True
    assert cfg.relay_reactions is False
    assert cfg.wecom.effective_rate_limit == 18
    assert cfg.wecom.resolved_webhook_key == "abc-123"
    assert cfg.wecom.resolved_upload_url == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc-123&type=file"
    )

    path2 = tmp_path / "webhook_key.toml"
    path2.write_text(
        """
[matrix]
homeserver = "http://hs:8008"
domain = "example.org"
as_token = "as"
hs_token = "hs"
bot_mxid = "@wecombot:example.org"

[wecom]
mode = "webhook"
webhook_key = "xyz-789"
api_base = "http://127.0.0.1:9999/"
msgtype = "text"

[[room]]
alias = "#main:example.org"
label = "main"
""",
        encoding="utf-8",
    )
    cfg2 = load(path2)
    assert cfg2.wecom.resolved_webhook_url == "http://127.0.0.1:9999/cgi-bin/webhook/send?key=xyz-789"
    assert cfg2.wecom.resolved_upload_url == "http://127.0.0.1:9999/cgi-bin/webhook/upload_media?key=xyz-789&type=file"
    assert cfg2.wecom.msgtype == "text"


def test_invalid_configs_rejected(tmp_path: Path):
    cases = [
        ("bad mode", BASE_AIBOT.replace('mode = "aibot"', 'mode = "custom"')),
        ("missing webhook", BASE_AIBOT.replace('mode = "aibot"', 'mode = "webhook"')),
        ("incoming not in rooms", BASE_AIBOT.replace('incoming = "#main:example.org"', 'incoming = "#other:example.org"')),
        ("bad bot_mxid", BASE_AIBOT.replace("@wecombot:example.org", "wecombot")),
        ("duplicate label", BASE_AIBOT + '\n[[room]]\nalias = "#two:example.org"\nlabel = "main"\n'),
        ("bad ignore regex", 'ignore_senders = ["[unclosed"]\n' + BASE_AIBOT),
        (
            "mismatched webhook keys",
            BASE_AIBOT.replace(
                'mode = "aibot"',
                'mode = "webhook"\nwebhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k1"\nwebhook_key = "k2"',
            ),
        ),
        ("bad rate limit", BASE_AIBOT.replace('mode = "aibot"', 'mode = "aibot"\nrate_limit_per_minute = 0')),
    ]
    for name, body in cases:
        path = tmp_path / f"{name.replace(' ', '_')}.toml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigError, match=".+"):
            load(path)
