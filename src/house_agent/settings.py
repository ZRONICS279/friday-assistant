"""What the house may change about itself when asked: a television's saved
address or name, and the home Wi-Fi handed to new Matter devices.

Settings live in two files that load_config reads: config.toml, and a .env
beside it whose values win. The Roku entry in particular can come from
either — ROKU_IP_ADDRESS in .env replaces every TV in config.toml — so a
change is written wherever the running value came from, and to config.toml
as well when it has an entry, so the two never disagree. Files are edited
in place, comments and all, validated, and swapped in atomically."""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


class SettingsError(RuntimeError):
    pass


_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$",
    re.IGNORECASE,
)
_TV_HEADERS = {"[[roku.devices]]", "[[tv.devices]]"}
_SPOKEN_DOT = re.compile(r"\s*\b(?:dot|point)\b\s*", re.IGNORECASE)


def clean_host(value: Any) -> str:
    """An address as people say or paste it — '192 dot 168 dot 0 dot 177',
    'http://192.168.0.5:8060/', 'friday-tv.local' — as a bare host."""
    text = str(value or "").strip()
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE).strip("/")
    text = _SPOKEN_DOT.sub(".", text).rstrip(".")
    if text.count(":") == 1 and text.rsplit(":", 1)[1].isdigit():
        text = text.rsplit(":", 1)[0]
    if not text:
        raise SettingsError("no address was given")
    if " " in text:
        raise SettingsError(f"{value!r} is not an IP address or a hostname")
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    if _HOSTNAME.match(text):
        return text
    raise SettingsError(f"{value!r} is not an IP address or a hostname")


def clean_port(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{value!r} is not a port number") from None
    if not 1 <= port <= 65535:
        raise SettingsError(f"port {port} is out of range")
    return port


def _env_literal(value: str) -> str:
    if re.search(r"""[\s#'"$\\]""", value) or value == "":
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True, slots=True)
class Saved:
    """Where one change landed."""

    file: str
    key: str
    previous: str | None
    value: str


class HouseSettings:
    def __init__(self, config_path: Path | str) -> None:
        self.config_path = Path(config_path)
        self.env_path = self.config_path.with_name(".env")

    # ------------------------------------------------------------------ .env

    def env_value(self, key: str) -> str | None:
        if not self.env_path.exists():
            return None
        value = dotenv_values(self.env_path).get(key)
        return str(value) if value not in (None, "") else None

    def set_env(self, key: str, value: str, *, secret: bool = False) -> Saved:
        """Set KEY=value in .env, replacing the line if there is one and
        keeping everything else, comments included."""
        lines = self.env_path.read_text().splitlines(keepends=True) if self.env_path.exists() else []
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
        rendered = f"{key}={_env_literal(value)}\n"
        previous = self.env_value(key)
        for index, line in enumerate(lines):
            if pattern.match(line):
                lines[index] = rendered
                break
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(rendered)
        self._write(self.env_path, "".join(lines))
        shown = "•••" if secret else value
        return Saved(self.env_path.name, key, "•••" if secret and previous else previous, shown)

    def unset_env(self, key: str) -> Saved | None:
        """Comment KEY out of .env, keeping the line for the record; None
        when it was not set."""
        if not self.env_path.exists():
            return None
        lines = self.env_path.read_text().splitlines(keepends=True)
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
        previous = self.env_value(key)
        changed = False
        for index, line in enumerate(lines):
            if pattern.match(line):
                lines[index] = "# " + line.lstrip()
                changed = True
        if not changed:
            return None
        self._write(self.env_path, "".join(lines))
        return Saved(self.env_path.name, key, previous, "")

    # ------------------------------------------------------------ config.toml

    def _tv_block(self, lines: list[str], device_id: str) -> tuple[int, int] | None:
        """Line range (header, end) of the [[roku.devices]] or [[tv.devices]]
        table whose id is device_id."""
        for start, line in enumerate(lines):
            if line.strip() not in _TV_HEADERS:
                continue
            end = next(
                (j for j in range(start + 1, len(lines)) if lines[j].lstrip().startswith("[")),
                len(lines),
            )
            for j in range(start + 1, end):
                match = re.match(r'^\s*id\s*=\s*"([^"]*)"', lines[j])
                if match and match.group(1) == device_id:
                    return start, end
        return None

    def has_tv_entry(self, device_id: str) -> bool:
        if not self.config_path.exists():
            return False
        return self._tv_block(self.config_path.read_text().splitlines(keepends=True), device_id) is not None

    def set_tv_field(self, device_id: str, key: str, value: Any) -> Saved | None:
        """Change one key of a TV's table in config.toml, or add it; None
        when config.toml has no table for that TV."""
        if not self.config_path.exists():
            return None
        lines = self.config_path.read_text().splitlines(keepends=True)
        block = self._tv_block(lines, device_id)
        if block is None:
            return None
        start, end = block
        rendered = f"{key} = {_toml_literal(value)}"
        previous: str | None = None
        for j in range(start + 1, end):
            match = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*(#.*)?$", lines[j])
            if match:
                previous = match.group(1).strip().strip('"')
                comment = f"  {match.group(2)}" if match.group(2) else ""
                lines[j] = rendered + comment + "\n"
                break
        else:
            insert_at = end
            while insert_at > start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, rendered + "\n")
        text = "".join(lines)
        try:
            tomllib.loads(text)  # never write a file the next start cannot read
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(f"that would leave config.toml unreadable: {exc}") from exc
        self._write(self.config_path, text)
        return Saved(self.config_path.name, f"{device_id}.{key}", previous, str(value))

    def add_tv_entry(self, device: dict[str, Any]) -> Saved:
        """Append a [[tv.devices]] table for a new television."""
        if not self.config_path.exists():
            raise SettingsError(f"{self.config_path.name} does not exist")
        device_id = str(device["id"])
        lines = self.config_path.read_text().splitlines(keepends=True)
        if self._tv_block(lines, device_id) is not None:
            raise SettingsError(f"a TV with the id {device_id!r} is already saved")
        block = ["\n", "[[tv.devices]]\n"]
        for key in ("id", "name", "host", "port", "platform", "connected_to", "input", "mac"):
            value = device.get(key)
            if value in (None, ""):
                continue
            block.append(f"{key} = {_toml_literal(value)}\n")
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        text = "".join(lines + block)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(f"that would leave config.toml unreadable: {exc}") from exc
        self._write(self.config_path, text)
        return Saved(self.config_path.name, device_id, None, str(device.get("host") or ""))

    def remove_tv_entry(self, device_id: str) -> Saved | None:
        """Drop a television's table from config.toml; None when it has none."""
        if not self.config_path.exists():
            return None
        lines = self.config_path.read_text().splitlines(keepends=True)
        block = self._tv_block(lines, device_id)
        if block is None:
            return None
        start, end = block
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        previous = next(
            (
                m.group(1)
                for line in lines[block[0] : end]
                if (m := re.match(r'^\s*host\s*=\s*"([^"]*)"', line))
            ),
            None,
        )
        del lines[start:end]
        text = "".join(lines)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(f"that would leave config.toml unreadable: {exc}") from exc
        self._write(self.config_path, text)
        return Saved(self.config_path.name, device_id, previous, "")

    # --------------------------------------------------------- what to change

    def roku_env_override(self) -> dict[str, str] | None:
        """The TV that ROKU_IP_ADDRESS in .env defines, when it does: that
        entry replaces every TV in config.toml at load time."""
        host = self.env_value("ROKU_IP_ADDRESS")
        if not host:
            return None
        return {
            "id": self.env_value("ROKU_DEVICE_ID") or "living-room",
            "name": self.env_value("ROKU_DEVICE_NAME") or "Living Room TV",
            "host": host,
            "port": self.env_value("ROKU_PORT") or "8060",
        }

    def save_tv_address(self, device_id: str, host: str, port: int | None = None) -> list[Saved]:
        saved: list[Saved] = []
        override = self.roku_env_override()
        if override and override["id"] == device_id:
            saved.append(self.set_env("ROKU_IP_ADDRESS", host))
            if port:
                saved.append(self.set_env("ROKU_PORT", str(port)))
        change = self.set_tv_field(device_id, "host", host)
        if change:
            saved.append(change)
            if port:
                saved.append(self.set_tv_field(device_id, "port", port) or change)
        if not saved:
            raise SettingsError(
                f"no saved entry for the TV {device_id!r} in {self.config_path.name} or .env"
            )
        return saved

    def save_tv_name(self, device_id: str, name: str) -> list[Saved]:
        name = " ".join(str(name).split())
        if not name:
            raise SettingsError("the TV needs a name")
        saved: list[Saved] = []
        override = self.roku_env_override()
        if override and override["id"] == device_id:
            saved.append(self.set_env("ROKU_DEVICE_NAME", name))
        change = self.set_tv_field(device_id, "name", name)
        if change:
            saved.append(change)
        if not saved:
            raise SettingsError(f"no saved entry for the TV {device_id!r}")
        return saved

    def add_tv(self, device: dict[str, Any]) -> list[Saved]:
        """Save a new television so it loads next start alongside the others.

        ROKU_IP_ADDRESS in .env replaces every TV in config.toml at load
        time, so while it is set a second TV could never appear. The TV it
        defines is written into config.toml first (if it is not already
        there), then the override is commented out: nothing is lost, and
        both TVs load from the same file from now on."""
        saved: list[Saved] = []
        override = self.roku_env_override()
        if override:
            if self.has_tv_entry(override["id"]):
                # The file's copy may be stale: .env has been the truth.
                for key, value in (
                    ("host", override["host"]),
                    ("port", int(override["port"])),
                    ("name", override["name"]),
                ):
                    change = self.set_tv_field(override["id"], key, value)
                    if change:
                        saved.append(change)
            else:
                saved.append(self.add_tv_entry({**override, "port": int(override["port"]), "platform": "roku"}))
            for key in ("ROKU_IP_ADDRESS", "ROKU_DEVICE_ID", "ROKU_DEVICE_NAME", "ROKU_PORT"):
                change = self.unset_env(key)
                if change:
                    saved.append(change)
        saved.append(self.add_tv_entry(device))
        return saved

    def remove_tv(self, device_id: str) -> list[Saved]:
        saved: list[Saved] = []
        override = self.roku_env_override()
        if override and override["id"] == device_id:
            for key in ("ROKU_IP_ADDRESS", "ROKU_DEVICE_ID", "ROKU_DEVICE_NAME", "ROKU_PORT"):
                change = self.unset_env(key)
                if change:
                    saved.append(change)
        change = self.remove_tv_entry(device_id)
        if change:
            saved.append(change)
        if not saved:
            raise SettingsError(f"no saved entry for the TV {device_id!r}")
        return saved

    def save_wifi(self, ssid: str, password: str) -> list[Saved]:
        ssid = str(ssid).strip()
        if not ssid:
            raise SettingsError("the Wi-Fi network needs a name")
        if not password:
            raise SettingsError("the Wi-Fi password is missing")
        return [
            self.set_env("HOUSE_WIFI_SSID", ssid),
            self.set_env("HOUSE_WIFI_PASSWORD", str(password), secret=True),
        ]

    @staticmethod
    def _write(path: Path, text: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
