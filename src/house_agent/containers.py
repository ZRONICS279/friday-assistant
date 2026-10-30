"""Start the containers house-agent needs, and stop them when it exits.

house-agent already starts matter-server itself (matter_process.py). The rest
of the house runs in containers: mosquitto and Zigbee2MQTT for the Zigbee
coordinator, kokoro and whisper for speech. Bringing those up by hand before
every session -- and remembering to stop them afterwards -- is the kind of
thing that gets forgotten. Zigbee2MQTT sat dead for ten days in September
because its container exited with the dongle and nothing noticed.

So house-agent runs `docker compose up -d` for them at start-up and
`docker compose stop` on the way out. Every compose file goes to a single
`docker compose` invocation, which is what Compose itself wants: they share
one project (named for this folder), so passing only one of them makes
Compose treat the other's containers as orphans.

`up -d` returns in about half a second and does not wait for the services to
answer -- it does not need to. Every backend here already has its own
reconnect loop, so mosquitto and whisper finish coming up (two or three
seconds; eleven for whisper's CUDA runtime) while house-agent is still
opening its database and probing the model.

A service that was already running is adopted, not restarted, and is left
running at shutdown: house-agent stops only what it actually started. That is
the rule matter_process.py already follows for a controller it did not spawn,
and it keeps a `docker compose up` someone is debugging with from dying under
them when house-agent quits.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

NOT_INSTALLED = "docker compose is not installed"
NO_DAEMON = "the docker daemon is not reachable"

# _run's stand-ins for "the command never ran", which no exit status covers.
CANNOT_RUN = -1
TIMED_OUT = -2


class Containers:
    """The compose project house-agent brings up with itself."""

    def __init__(
        self,
        files: tuple[Path, ...] | list[Path],
        *,
        command: tuple[str, ...] = ("docker", "compose"),
        cwd: Path | None = None,
        start_timeout: float = 180.0,
        stop_timeout: float = 30.0,
    ) -> None:
        self.files = [Path(path) for path in files]
        self.command = tuple(command)
        # Compose resolves a file's relative volume paths against the file,
        # but names the project after the directory it is run from.
        self.cwd = Path(cwd) if cwd else None
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        # Services this process started, and so is responsible for stopping.
        self.started: tuple[str, ...] = ()
        self.adopted: tuple[str, ...] = ()
        self.gave_up: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.files)

    def _argv(self, *args: str) -> list[str]:
        argv = [*self.command]
        for path in self.files:
            argv += ["-f", str(path)]
        return argv + list(args)

    async def _run(self, *args: str, timeout: float) -> tuple[int, str]:
        """Run one compose command, returning (code, combined output).
        CANNOT_RUN and TIMED_OUT stand for the command never finishing."""
        argv = self._argv(*args)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.cwd) if self.cwd else None,
            )
        except OSError as exc:
            return CANNOT_RUN, str(exc)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return TIMED_OUT, f"`{' '.join(args)}` took longer than {timeout:.0f}s"
        return process.returncode or 0, stdout.decode("utf-8", "replace").strip()

    async def _services(self, *args: str) -> tuple[int, list[str]]:
        code, output = await self._run(*args, timeout=30.0)
        if code != 0:
            return code, []
        return code, [line.strip() for line in output.splitlines() if line.strip()]

    async def start(self) -> None:
        """Bring the project up, remembering what was already running."""
        if not self.files:
            return
        missing_files = [path for path in self.files if not path.is_file()]
        if missing_files:
            self.gave_up = f"compose file {missing_files[0]} not found"
            log.warning("containers: %s; starting no containers", self.gave_up)
            self.files = []
            return

        # This first call doubles as the check that docker is usable at all.
        code, running = await self._services("ps", "--services", "--status", "running")
        if code != 0:
            self.gave_up = {CANNOT_RUN: NOT_INSTALLED, TIMED_OUT: "docker compose did not answer"}.get(
                code, NO_DAEMON
            )
            log.warning(
                "containers: %s; house-agent will run without them "
                "(start them yourself with `docker compose up -d`)",
                self.gave_up,
            )
            self.files = []
            return

        _, wanted = await self._services("config", "--services")
        already = set(running)
        self.adopted = tuple(sorted(already.intersection(wanted)))
        if self.adopted:
            log.info(
                "containers: %s already running; adopting %s (house-agent will leave %s running when it exits)",
                ", ".join(self.adopted),
                "it" if len(self.adopted) == 1 else "them",
                "it" if len(self.adopted) == 1 else "them",
            )
        if set(wanted) <= already:
            return

        code, output = await self._run("up", "-d", timeout=self.start_timeout)
        if code != 0:
            self.gave_up = f"`docker compose up -d` failed: {output.splitlines()[-1] if output else code}"
            log.warning("containers: %s", self.gave_up)

        _, now_running = await self._services("ps", "--services", "--status", "running")
        self.started = tuple(sorted(set(now_running).difference(already)))
        if self.started:
            log.info("containers: started %s", ", ".join(self.started))
        failed = sorted(set(wanted).difference(now_running))
        if failed:
            log.warning(
                "containers: %s did not come up; `docker compose logs %s` says why",
                ", ".join(failed),
                " ".join(failed),
            )

    async def stop(self) -> None:
        """Stop only the services this process started."""
        if not self.files or not self.started:
            return
        # `stop`, not `down`: the containers stay on disk, so the next start
        # is a couple of seconds instead of a rebuild.
        code, output = await self._run("stop", *self.started, timeout=self.stop_timeout)
        if code != 0:
            log.warning(
                "containers: could not stop %s (%s); stop them with `docker compose stop`",
                ", ".join(self.started),
                output.splitlines()[-1] if output else code,
            )
            return
        log.info("containers: stopped %s", ", ".join(self.started))
        self.started = ()

    async def recreate(self, service: str) -> bool:
        """Throw one service's container away and make a fresh one.

        A plain restart is not enough for the failure this exists for: after
        Docker restarts a crashed container it can come back detached from
        the project network, so Zigbee2MQTT cannot even resolve `mosquitto`
        and exits again, forever. A new container gets a new network
        endpoint. Its state lives in bind-mounted data, so nothing is lost."""
        if not self.files:
            return False
        code, output = await self._run(
            "up", "-d", "--force-recreate", "--no-deps", service, timeout=self.start_timeout
        )
        if code != 0:
            log.warning("containers: could not recreate %s: %s", service, output.splitlines()[-1] if output else code)
            return False
        log.warning("containers: recreated %s", service)
        return True

    def status(self) -> dict[str, object]:
        return {
            "enabled": bool(self.files),
            "started": list(self.started),
            "adopted": list(self.adopted),
            "error": self.gave_up,
        }
