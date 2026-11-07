"""Validate the inventory schema illustrated in examples/topology.toml.

A topology has ``plugins`` and ``communities`` arrays. Each plugin has string
``id``, ``kind``, ``namespace`` and ``config_ref`` fields. Each community has
string ``id`` and ``room_id`` fields and an ``endpoints`` array; each endpoint
has string ``plugin`` and ``remote_id`` fields. All strings must be nonblank.

This validates inventory consistency, not bridge behavior, network access,
room ownership, secret safety, or isolation enforced by external services.
"""

from __future__ import annotations

import errno
from pathlib import Path
import re
import tomllib
from typing import Any


class ValidationError(Exception):
    """One or more safe-to-display topology validation errors."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def validate_topology(data: Any) -> list[str]:
    """Return deterministic diagnostics without including configured values."""
    errors: list[str] = []

    def table(value: Any, path: str, keys: tuple[str, ...]) -> bool:
        if type(value) is not dict:
            errors.append(f"{path}: expected a table")
            return False
        if any(key not in keys for key in value):
            errors.append(f"{path}: unknown key; allowed keys: {', '.join(keys)}")
        for key in keys:
            if key not in value:
                errors.append(f"{path}.{key}: required")
        return True

    def array(value: Any, path: str) -> bool:
        if type(value) is not list:
            errors.append(f"{path}: expected an array")
            return False
        return True

    def string(obj: dict, key: str, path: str) -> str | None:
        if key not in obj:
            return None
        value = obj[key]
        if type(value) is not str or not value.strip():
            errors.append(f"{path}.{key}: expected a nonblank string")
            return None
        return value

    def unique(seen: dict[str, str], value: str | None, path: str) -> None:
        if value is None:
            return
        if value in seen:
            errors.append(f"{path}: duplicates {seen[value]}")
        else:
            seen[value] = path

    if not table(data, "topology", ("plugins", "communities")):
        return errors

    plugin_ids: dict[str, str] = {}
    namespaces: dict[str, str] = {}
    plugins = data.get("plugins")
    if "plugins" in data and array(plugins, "plugins"):
        for i, plugin in enumerate(plugins):
            path = f"plugins[{i}]"
            if not table(plugin, path, ("id", "kind", "namespace", "config_ref")):
                continue
            plugin_id = string(plugin, "id", path)
            string(plugin, "kind", path)
            namespace = string(plugin, "namespace", path)
            string(plugin, "config_ref", path)
            unique(plugin_ids, plugin_id, f"{path}.id")
            unique(namespaces, namespace, f"{path}.namespace")

    community_ids: dict[str, str] = {}
    room_ids: dict[str, str] = {}
    remotes: dict[tuple[str, str], str] = {}
    communities = data.get("communities")
    if "communities" in data and array(communities, "communities"):
        for i, community in enumerate(communities):
            path = f"communities[{i}]"
            if not table(community, path, ("id", "room_id", "endpoints")):
                continue
            community_id = string(community, "id", path)
            room_id = string(community, "room_id", path)
            unique(community_ids, community_id, f"{path}.id")
            if room_id is not None:
                # Check the recognizable Matrix room ID shape, not server validity.
                local, separator, server = room_id.partition(":")
                if (not local.startswith("!") or (not separator and len(local) < 2)
                        or (separator and (not server or any(c.isspace() for c in server)))
                        or "\0" in room_id):
                    errors.append(f"{path}.room_id: expected a Matrix room ID starting with !")
                unique(room_ids, room_id, f"{path}.room_id")
            endpoints = community.get("endpoints")
            community_plugins: dict[str, str] = {}
            if "endpoints" not in community or not array(endpoints, f"{path}.endpoints"):
                continue
            for j, endpoint in enumerate(endpoints):
                endpoint_path = f"{path}.endpoints[{j}]"
                if not table(endpoint, endpoint_path, ("plugin", "remote_id")):
                    continue
                plugin_id = string(endpoint, "plugin", endpoint_path)
                remote_id = string(endpoint, "remote_id", endpoint_path)
                if plugin_id is not None:
                    if plugin_id not in plugin_ids:
                        errors.append(f"{endpoint_path}.plugin: references an unknown plugin id")
                    unique(community_plugins, plugin_id, f"{endpoint_path}.plugin")
                    if remote_id is not None:
                        pair = (plugin_id, remote_id)
                        if pair in remotes:
                            errors.append(f"{endpoint_path}.remote_id: duplicates {remotes[pair]} for the same plugin")
                        else:
                            remotes[pair] = f"{endpoint_path}.remote_id"
    return errors


def load_topology(path: str | Path) -> dict[str, Any]:
    """Read a UTF-8 TOML inventory and raise ValidationError if invalid.

    The config_ref fields are never opened. Neither values nor file paths are
    included in errors, including TOML parse errors.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        code = errno.errorcode.get(exc.errno, "OS_ERROR")
        raise ValidationError([f"file: cannot read ({code})"]) from None
    except ValueError:
        raise ValidationError(["file: invalid path"]) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(["file: expected UTF-8 encoding"]) from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # Only copy the parser's numeric location, never its message or input.
        location = re.search(r"\(at line (\d+), column (\d+)\)$", str(exc))
        suffix = f" at line {location[1]}, column {location[2]}" if location else ""
        raise ValidationError([f"file: invalid TOML{suffix}"]) from None
    except (ValueError, RecursionError):
        # Extremely large numbers or deep nesting can exceed parser limits.
        raise ValidationError(["file: TOML exceeds parser limits"]) from None
    errors = validate_topology(data)
    if errors:
        raise ValidationError(errors)
    return data
