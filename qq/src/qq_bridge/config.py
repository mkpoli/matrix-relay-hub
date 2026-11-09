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
    api_base: str = "https://api.bot.qq.com"
    intents: int = 1 << 25

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_secret)


@dataclass(frozen=True)
class BridgeConfig:
    room: str
    code: str
    group: str | None = None


@dataclass(frozen=True)
class Config:
    matrix: MatrixConfig
    qq: QQConfig
    bridges: tuple[BridgeConfig, ...]
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

    qq_raw = raw.get("qq", {})
    if not isinstance(qq_raw, dict):
        raise ConfigError("[qq] must be a table")
    qq = QQConfig(
        app_id=qq_raw.get("app_id", ""),
        app_secret=qq_raw.get("app_secret", ""),
        api_base=qq_raw.get("api_base", "https://api.bot.qq.com").rstrip("/"),
        intents=int(qq_raw.get("intents", 1 << 25)),
    )

    bridges_raw = raw.get("bridge")
    if not isinstance(bridges_raw, list) or not bridges_raw:
        raise ConfigError("at least one [[bridge]] entry is required")
    bridges: list[BridgeConfig] = []
    seen_codes: set[str] = set()
    seen_rooms: set[str] = set()
    for index, entry in enumerate(bridges_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"bridge[{index}] must be a table")
        room = _require(entry, "room", f"bridge[{index}]")
        code = _require(entry, "code", f"bridge[{index}]")
        group = entry.get("group") or None
        if not room.startswith("#"):
            raise ConfigError(f"bridge[{index}].room must be a room alias")
        if code in seen_codes:
            raise ConfigError(f"duplicate bridge code {code!r}")
        if room in seen_rooms:
            raise ConfigError(f"duplicate bridge room {room!r}")
        seen_codes.add(code)
        seen_rooms.add(room)
        bridges.append(BridgeConfig(room=room, code=code, group=group))

    ignore_raw = raw.get("ignore_senders", [])
    if not isinstance(ignore_raw, list) or not all(isinstance(x, str) for x in ignore_raw):
        raise ConfigError("ignore_senders must be a list of strings")

    state = raw.get("state")
    state_path = Path(state) if state else path.parent / "state.sqlite3"

    return Config(
        matrix=matrix,
        qq=qq,
        bridges=tuple(bridges),
        ignore_senders=tuple(ignore_raw),
        ignore_notices=bool(raw.get("ignore_notices", True)),
        state_path=state_path,
    )
