"""Run matter-server as part of house-agent.

The Matter controller is a separate program (its native CHIP wheels need
their own Python), but it is small -- tens of megabytes and a few seconds of
CPU a day -- and pointless without house-agent, so house-agent starts it,
restarts it if it dies, streams its log into its own, and stops it on exit.
The launcher is scripts/matter-server.sh, which takes a lock on the storage
folder and runs the pairing-list guard before the real server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)
server_log = logging.getLogger("matter-server")

ALREADY_RUNNING = "already running"
HEALTHY_RUN_SECONDS = 60.0  # ran at least this long: not a crash loop

# matter-server logs through coloredlogs, which colors the output even into
# a pipe; and every line repeats a full date, time and thread name that
# house-agent's own log line already carries.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SERVER_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ \(\S+\) ")


def clean(line: str) -> str:
    """A server log line as house-agent should show it: no colors, no
    duplicate timestamp."""
    return _SERVER_PREFIX.sub("", _ANSI.sub("", line)).rstrip()


def is_local(url: str) -> bool:
    """Only a controller on this machine can be ours to start."""
    return urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"}


def _level(line: str) -> int:
    """For a line already passed through clean()."""
    if line.startswith(("CHIP_ERROR", "ERROR", "WARNING")):
        return logging.WARNING
    if line.startswith("INFO [matter_server."):
        return logging.INFO  # the server's own events: pairing, nodes, startup
    return logging.DEBUG  # CHIP stack chatter


class MatterProcess:
    def __init__(
        self,
        launcher: Path,
        *,
        env: dict[str, str] | None = None,
        restart_delay: float = 5.0,
        max_delay: float = 60.0,
    ) -> None:
        self.launcher = Path(launcher)
        # Extra environment for the launcher (MATTER_BLUETOOTH_ADAPTER...),
        # on top of house-agent's own.
        self.env = dict(env or {})
        self.restart_delay = restart_delay
        self.max_delay = max_delay
        self.process: asyncio.subprocess.Process | None = None
        self.starts = 0
        self.gave_up: str | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def run(self) -> None:
        """Supervise the server until cancelled."""
        if not self.launcher.is_file():
            self.gave_up = f"launcher {self.launcher} not found"
            log.warning("matter: %s; not starting a controller", self.gave_up)
            return
        delay = self.restart_delay
        while True:
            started = time.monotonic()
            self.starts += 1
            try:
                self.process = await asyncio.create_subprocess_exec(
                    str(self.launcher),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env={**os.environ, **self.env} if self.env else None,
                    start_new_session=True,  # so stop() can signal the whole group
                )
            except OSError as exc:
                self.gave_up = f"could not start {self.launcher}: {exc}"
                log.warning("matter: %s", self.gave_up)
                return
            log.info("matter: started matter-server (pid %d)", self.process.pid)
            elsewhere = False
            assert self.process.stdout is not None
            async for raw in self.process.stdout:
                line = clean(raw.decode("utf-8", "replace"))
                if ALREADY_RUNNING in line:
                    elsewhere = True
                server_log.log(_level(line), "%s", line)
            code = await self.process.wait()
            if elsewhere:
                self.gave_up = "another matter-server already owns the storage folder"
                log.info(
                    "matter: %s (the systemd unit?); house-agent will just connect to it",
                    self.gave_up,
                )
                return
            ran = time.monotonic() - started
            delay = self.restart_delay if ran >= HEALTHY_RUN_SECONDS else min(delay * 2, self.max_delay)
            log.warning(
                "matter: matter-server exited with code %s after %.0fs; restarting in %.0fs",
                code,
                ran,
                delay,
            )
            await asyncio.sleep(delay)

    async def stop(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("matter: matter-server did not stop in 10s; killing it")
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        log.info("matter: matter-server stopped")
