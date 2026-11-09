"""Configuration loading for the QQ bridge."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


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
    listen_port: int = 29335
    admin_room: str | None = None

    @property
    def bot_localpart(self) -> str:
        return self.bot_mxid.split(":", 1)[0].lstrip("@")


@dataclass(frozen=True)
class QQConfig:
    app_id: str
    app_secret: str
    group: str
    incoming: str
    api_base: str = "https://api.bot.qq.com"
    intents: int = 1 << 25

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_secret)


@dataclass(frozen=True)
class RoomConfig:
    """A Matrix room mirrored into the QQ group, named by its label there."""

    alias: str
    label: str


@dataclass(frozen=True)
class Config:
    matrix: MatrixConfig
    qq: QQConfig
    rooms: tuple[RoomConfig, ...]
    ignore_senders: tuple[str, ...]
    ignore_notices: bool
    state_path: Path


def _require(section: dict, key: str, where: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}.{key} is required")
    return value


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
    matrix = MatrixConfig(
        homeserver=_require(matrix_raw, "homeserver", "matrix").rstrip("/"),
        domain=_require(matrix_raw, "domain", "matrix"),
        as_token=_require(matrix_raw, "as_token", "matrix"),
        hs_token=_require(matrix_raw, "hs_token", "matrix"),
        bot_mxid=_require(matrix_raw, "bot_mxid", "matrix"),
        listen_host=matrix_raw.get("listen_host", "127.0.0.1"),
        listen_port=int(matrix_raw.get("listen_port", 29335)),
        admin_room=matrix_raw.get("admin_room"),
    )
    if not matrix.bot_mxid.startswith("@"):
        raise ConfigError("matrix.bot_mxid must be a full user id")

    qq_raw = raw.get("qq")
    if not isinstance(qq_raw, dict):
        raise ConfigError("missing [qq] section")
    qq = QQConfig(
        app_id=qq_raw.get("app_id", ""),
        app_secret=qq_raw.get("app_secret", ""),
        group=_require(qq_raw, "group", "qq"),
        incoming=_require(qq_raw, "incoming", "qq"),
        api_base=qq_raw.get("api_base", "https://api.bot.qq.com").rstrip("/"),
        intents=int(qq_raw.get("intents", 1 << 25)),
    )
    if not qq.incoming.startswith("#"):
        raise ConfigError("qq.incoming must be a room alias")

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
    if qq.incoming not in seen_aliases:
        raise ConfigError("qq.incoming must name one of the [[room]] aliases")

    ignore_raw = raw.get("ignore_senders", [])
    if not isinstance(ignore_raw, list) or not all(isinstance(x, str) for x in ignore_raw):
        raise ConfigError("ignore_senders must be a list of strings")

    state = raw.get("state")
    state_path = Path(state) if state else path.parent / "state.sqlite3"

    return Config(
        matrix=matrix,
        qq=qq,
        rooms=tuple(rooms),
        ignore_senders=tuple(ignore_raw),
        ignore_notices=bool(raw.get("ignore_notices", True)),
        state_path=state_path,
    )
