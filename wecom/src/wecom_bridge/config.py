"""Configuration loading for the WeCom bridge."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
DEFAULT_API_BASE = "https://qyapi.weixin.qq.com"
VALID_MODES = ("aibot", "webhook")
VALID_WEBHOOK_MSGTYPES = ("markdown", "text")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class MatrixConfig:
    homeserver: str
    domain: str
    as_token: str
    hs_token: str
    bot_mxid: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 29336

    @property
    def bot_localpart(self) -> str:
        return self.bot_mxid.split(":", 1)[0].lstrip("@")


@dataclass(frozen=True)
class WeComConfig:
    mode: str
    bot_id: str = ""
    secret: str = ""
    chat_id: str = ""
    bot_name: str = ""
    incoming: str = ""
    ws_url: str = DEFAULT_WS_URL
    webhook_url: str = ""
    webhook_key: str = ""
    api_base: str = DEFAULT_API_BASE
    msgtype: str = "markdown"
    rate_limit_per_minute: int | None = None

    @property
    def effective_rate_limit(self) -> int:
        if self.rate_limit_per_minute is not None:
            return self.rate_limit_per_minute
        # Group Robot Webhooks enforce 20 msg/min; keep headroom at 18/min.
        # AI Bot WS does not publish a fixed per-minute cap; 50/min is a conservative default.
        return 18 if self.mode == "webhook" else 50

    @property
    def aibot_enabled(self) -> bool:
        return self.mode == "aibot" and bool(self.bot_id and self.secret)

    @property
    def can_send(self) -> bool:
        if self.mode == "webhook":
            return bool(self.resolved_webhook_url)
        return self.aibot_enabled and bool(self.chat_id)

    @property
    def resolved_webhook_key(self) -> str:
        if self.webhook_key:
            return self.webhook_key
        if self.webhook_url:
            parsed = urlparse(self.webhook_url)
            keys = parse_qs(parsed.query).get("key")
            if keys and keys[0]:
                return keys[0]
        return ""

    @property
    def resolved_webhook_url(self) -> str:
        if self.webhook_url:
            return self.webhook_url
        if self.webhook_key:
            return f"{self.api_base}/cgi-bin/webhook/send?key={self.webhook_key}"
        return ""

    @property
    def resolved_upload_url(self) -> str:
        key = self.resolved_webhook_key
        if not key:
            return ""
        if self.webhook_url:
            parsed = urlparse(self.webhook_url)
            base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else self.api_base
        else:
            base = self.api_base
        return f"{base}/cgi-bin/webhook/upload_media?key={key}&type=file"


@dataclass(frozen=True)
class RoomConfig:
    """A Matrix room mirrored into the WeCom group, named by its label there."""

    alias: str
    label: str


@dataclass(frozen=True)
class Config:
    matrix: MatrixConfig
    wecom: WeComConfig
    rooms: tuple[RoomConfig, ...]
    members: dict[str, str]
    ignore_senders: tuple[str, ...]
    ignore_notices: bool
    state_path: Path
    relay_reactions: bool = True


def _require(section: dict, key: str, where: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} is required")
    return value.strip()


def _optional_str(section: dict, key: str, where: str, default: str = "") -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a string")
    return value.strip()


def load(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    matrix_raw = raw.get("matrix")
    if not isinstance(matrix_raw, dict):
        raise ConfigError("missing [matrix] section")
    listen_port = matrix_raw.get("listen_port", 29336)
    if isinstance(listen_port, bool) or not isinstance(listen_port, int) or not (1 <= listen_port <= 65535):
        raise ConfigError("matrix.listen_port must be an integer between 1 and 65535")
    matrix = MatrixConfig(
        homeserver=_require(matrix_raw, "homeserver", "matrix").rstrip("/"),
        domain=_require(matrix_raw, "domain", "matrix"),
        as_token=_require(matrix_raw, "as_token", "matrix"),
        hs_token=_require(matrix_raw, "hs_token", "matrix"),
        bot_mxid=_require(matrix_raw, "bot_mxid", "matrix"),
        listen_host=_optional_str(matrix_raw, "listen_host", "matrix", "127.0.0.1") or "127.0.0.1",
        listen_port=listen_port,
    )
    if not matrix.bot_mxid.startswith("@") or ":" not in matrix.bot_mxid:
        raise ConfigError("matrix.bot_mxid must be a full user id")

    wecom_raw = raw.get("wecom")
    if not isinstance(wecom_raw, dict):
        raise ConfigError("missing [wecom] section")
    mode = _optional_str(wecom_raw, "mode", "wecom", "aibot")
    if mode not in VALID_MODES:
        raise ConfigError(f"wecom.mode must be one of {', '.join(VALID_MODES)}")
    msgtype = _optional_str(wecom_raw, "msgtype", "wecom", "markdown")
    if msgtype not in VALID_WEBHOOK_MSGTYPES:
        raise ConfigError(f"wecom.msgtype must be one of {', '.join(VALID_WEBHOOK_MSGTYPES)}")

    rate_limit_raw = wecom_raw.get("rate_limit_per_minute")
    if rate_limit_raw is not None:
        if isinstance(rate_limit_raw, bool) or not isinstance(rate_limit_raw, int) or rate_limit_raw < 1:
            raise ConfigError("wecom.rate_limit_per_minute must be an integer >= 1")

    wecom = WeComConfig(
        mode=mode,
        bot_id=_optional_str(wecom_raw, "bot_id", "wecom"),
        secret=_optional_str(wecom_raw, "secret", "wecom"),
        chat_id=_optional_str(wecom_raw, "chat_id", "wecom"),
        bot_name=_optional_str(wecom_raw, "bot_name", "wecom"),
        incoming=_optional_str(wecom_raw, "incoming", "wecom"),
        ws_url=_optional_str(wecom_raw, "ws_url", "wecom", DEFAULT_WS_URL) or DEFAULT_WS_URL,
        webhook_url=_optional_str(wecom_raw, "webhook_url", "wecom"),
        webhook_key=_optional_str(wecom_raw, "webhook_key", "wecom"),
        api_base=(_optional_str(wecom_raw, "api_base", "wecom", DEFAULT_API_BASE) or DEFAULT_API_BASE).rstrip("/"),
        msgtype=msgtype,
        rate_limit_per_minute=rate_limit_raw,
    )
    if mode == "webhook":
        if not wecom.resolved_webhook_url:
            raise ConfigError("wecom.webhook_url or wecom.webhook_key is required in webhook mode")
        if wecom.webhook_url and wecom.webhook_key:
            url_keys = parse_qs(urlparse(wecom.webhook_url).query).get("key")
            url_key = url_keys[0] if url_keys else ""
            if url_key and url_key != wecom.webhook_key:
                raise ConfigError("wecom.webhook_url key does not match wecom.webhook_key")
    else:
        if not wecom.incoming:
            raise ConfigError("wecom.incoming is required in aibot mode")
        if not wecom.incoming.startswith("#"):
            raise ConfigError("wecom.incoming must be a room alias")

    rooms_raw = raw.get("room")
    if not isinstance(rooms_raw, list) or not rooms_raw:
        raise ConfigError("at least one [[room]] entry is required")
    rooms: list[RoomConfig] = []
    seen_aliases: set[str] = set()
    seen_labels: set[str] = set()
    for index, entry in enumerate(rooms_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"room[{index}] must be a table")
        alias = _require(entry, "alias", f"room[{index}]")
        label = _require(entry, "label", f"room[{index}]")
        if not alias.startswith("#"):
            raise ConfigError(f"room[{index}].alias must be a room alias")
        if alias in seen_aliases:
            raise ConfigError(f"duplicate room alias {alias!r}")
        if label in seen_labels:
            raise ConfigError(f"duplicate room label {label!r}")
        seen_aliases.add(alias)
        seen_labels.add(label)
        rooms.append(RoomConfig(alias=alias, label=label))
    if wecom.incoming and wecom.incoming not in seen_aliases:
        raise ConfigError("wecom.incoming must name one of the [[room]] aliases")

    members_raw = raw.get("members", {})
    if not isinstance(members_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in members_raw.items()
    ):
        raise ConfigError("[members] must map userid strings to display-name strings")
    members = {k.strip(): v.strip() for k, v in members_raw.items() if k.strip() and v.strip()}

    ignore_raw = raw.get("ignore_senders", [])
    if not isinstance(ignore_raw, list) or not all(isinstance(x, str) for x in ignore_raw):
        raise ConfigError("ignore_senders must be a list of strings")
    for pattern in ignore_raw:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"invalid regex in ignore_senders {pattern!r}: {exc}") from exc

    ignore_notices = raw.get("ignore_notices", True)
    if not isinstance(ignore_notices, bool):
        raise ConfigError("ignore_notices must be a boolean")
    default_reactions = mode != "webhook"
    relay_reactions = raw.get("relay_reactions", default_reactions)
    if not isinstance(relay_reactions, bool):
        raise ConfigError("relay_reactions must be a boolean")

    state = raw.get("state")
    if state is not None and (not isinstance(state, str) or not state.strip()):
        raise ConfigError("state must be a non-empty path string")
    state_path = Path(state) if state else path.parent / "state.sqlite3"

    return Config(
        matrix=matrix,
        wecom=wecom,
        rooms=tuple(rooms),
        members=members,
        ignore_senders=tuple(ignore_raw),
        ignore_notices=ignore_notices,
        state_path=state_path,
        relay_reactions=relay_reactions,
    )
