from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .config import RokuDeviceConfig, load_config
from .roku import (
    RokuClient,
    RokuError,
    discover_roku_devices,
    normalize_key,
    resolve_input_key,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roku-cli",
        description="Test a Roku over its local ECP control API.",
    )
    parser.add_argument("--config", type=Path, help="Config file (default: config.toml)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--host", help="Roku LAN IP or .local hostname")
    target.add_argument("--device", help="Roku device ID from config.toml")
    parser.add_argument("--port", type=int, default=8060, help="Roku ECP port (default: 8060)")

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="Find Roku devices with SSDP; sends no control command")
    commands.add_parser("info", help="Print full device information and the active app")
    commands.add_parser("status", help="Print a compact power/app status")
    commands.add_parser("apps", help="List installed apps and their IDs")

    key = commands.add_parser("key", help="Press a remote key")
    key.add_argument("key", help="For example Home, Down, Select, PowerOn, or VolumeDown")
    key.add_argument("--count", type=int, default=1, choices=range(1, 11))

    text = commands.add_parser("text", help="Type into the currently focused Roku text field")
    text.add_argument("text")

    launch = commands.add_parser("launch", help="Launch an installed app by name or ID")
    launch.add_argument("app")

    roku_input = commands.add_parser("input", help="Switch to a configured console or TV input")
    roku_input.add_argument("input")

    deep_link = commands.add_parser("deep-link", help="Launch an exact provider content ID")
    deep_link.add_argument("app")
    deep_link.add_argument("content_id")
    deep_link.add_argument(
        "media_type",
        choices=["movie", "episode", "season", "series", "shortFormVideo", "special", "live"],
    )

    commands.add_parser("interactive", help="Open an interactive terminal remote")
    return parser


def resolve_device(args: argparse.Namespace) -> RokuDeviceConfig:
    if args.host:
        return RokuDeviceConfig(id="direct", name=f"Roku at {args.host}", host=args.host, port=args.port)

    config = load_config(args.config)
    devices = {device.id: device for device in config.tv_devices}
    if args.device:
        try:
            return devices[args.device]
        except KeyError:
            known = ", ".join(devices) or "none"
            raise ValueError(f"Unknown configured device '{args.device}'. Configured: {known}") from None
    if len(devices) == 1:
        return next(iter(devices.values()))
    if not devices:
        raise ValueError("No Roku configured. Pass --host ROKU_IP or add one to config.toml.")
    raise ValueError("Multiple Rokus are configured. Select one with --device DEVICE_ID.")


async def run_command(args: argparse.Namespace) -> None:
    if args.command == "discover":
        devices = await asyncio.to_thread(discover_roku_devices, 2.0)
        _print_json(devices)
        if not devices:
            print("No Roku answered SSDP. Find its IP under Settings > Network > About.", file=sys.stderr)
        return

    client = RokuClient(resolve_device(args))
    if args.command == "info":
        info, active = await asyncio.gather(client.device_info(), client.active_app())
        _print_json(
            {
                "target": asdict(client.device),
                "device_info": info,
                "active_app": asdict(active) if active else None,
            }
        )
    elif args.command == "status":
        _print_json(await client.status())
    elif args.command == "apps":
        applications = await client.apps()
        width = max((len(app.id) for app in applications), default=2)
        for app in applications:
            print(f"{app.id:<{width}}  {app.name}")
    elif args.command == "key":
        key = normalize_key(args.key)
        for _ in range(args.count):
            await client.keypress(key)
        print(f"OK  {client.device.name}: {key} x{args.count}")
    elif args.command == "text":
        await client.type_text(args.text)
        print(f"OK  typed {len(args.text)} characters on {client.device.name}")
    elif args.command == "launch":
        app = await client.find_app(args.app)
        await client.launch(app.id)
        print(f"OK  launched {app.name} ({app.id}) on {client.device.name}")
    elif args.command == "input":
        aliases = dict(load_config(args.config).roku_input_aliases)
        key = resolve_input_key(args.input, aliases)
        await client.keypress(key)
        print(f"OK  switched {client.device.name} to {args.input} ({key})")
    elif args.command == "deep-link":
        app = await client.find_app(args.app)
        await client.deep_link(app.id, args.content_id, args.media_type)
        print(f"OK  sent {args.media_type} deep link to {app.name} on {client.device.name}")
    elif args.command == "interactive":
        await interactive(client)


async def interactive(client: RokuClient) -> None:
    print(f"Connected target: {client.device.name} ({client.device.host}:{client.device.port})")
    print("Type 'help' for commands. App names with spaces should be quoted.")
    while True:
        try:
            raw = input("roku> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
            command = parts[0].lower()
            if command in {"quit", "exit", "q"}:
                return
            if command == "help":
                print(_INTERACTIVE_HELP)
            elif command == "status":
                _print_json(await client.status())
            elif command == "info":
                _print_json(await client.device_info())
            elif command == "apps":
                for app in await client.apps():
                    print(f"{app.id:>8}  {app.name}")
            elif command == "launch" and len(parts) >= 2:
                app = await client.find_app(" ".join(parts[1:]))
                await client.launch(app.id)
                print(f"OK  launched {app.name} ({app.id})")
            elif command == "type" and len(parts) >= 2:
                text = " ".join(parts[1:])
                await client.type_text(text)
                print(f"OK  typed {len(text)} characters")
            elif command == "key" and len(parts) in {2, 3}:
                await _interactive_key(client, parts[1], int(parts[2]) if len(parts) == 3 else 1)
            elif len(parts) == 1:
                await _interactive_key(client, command, 1)
            else:
                print("Unknown command. Type 'help'.", file=sys.stderr)
        except (RokuError, ValueError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)


async def _interactive_key(client: RokuClient, key: str, count: int) -> None:
    if not 1 <= count <= 10:
        raise ValueError("Key repeat count must be between 1 and 10")
    normalized = normalize_key(key)
    for _ in range(count):
        await client.keypress(normalized)
    print(f"OK  {normalized} x{count}")


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


_INTERACTIVE_HELP = """Commands:
  up | down | left | right | select | back | home | play
  power_on | power_off | volume_up | volume_down | mute
  key KEY [COUNT]       press any supported key up to 10 times
  type TEXT             type into the currently focused text field
  launch APP            launch an installed app by name or ID
  apps                  list installed apps and IDs
  status                show power state and active app
  info                  show full Roku device information
  quit                   exit
"""


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(run_command(args))
    except (RokuError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    main()
