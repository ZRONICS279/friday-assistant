"""Wake-on-LAN.

An LG or Samsung TV that is properly off answers nothing on the network, so
there is no socket left to send a power-on command down. The only way back
is a magic packet, which needs the TV's MAC address and the TV's own setting
for it turned on — "Quick Start+" on LG, "Network Standby" on Samsung."""

from __future__ import annotations

import asyncio
import re
import socket

_MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def normalize_mac(mac: str) -> bytes:
    if not _MAC.match(mac.strip()):
        raise ValueError(f"'{mac}' is not a MAC address like 1a:2b:3c:4d:5e:6f")
    return bytes.fromhex(mac.strip().replace(":", "").replace("-", ""))


async def wake(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    """Send the magic packet: six 0xFF bytes then the MAC sixteen times."""
    payload = b"\xff" * 6 + normalize_mac(mac) * 16

    def send() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(payload, (broadcast, port))

    await asyncio.to_thread(send)
