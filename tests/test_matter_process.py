"""house-agent running matter-server itself."""

import asyncio
import os
import subprocess
from pathlib import Path

import logging

from house_agent.matter_process import MatterProcess, _level, clean, is_local


def launcher(tmp_path, body: str) -> Path:
    path = tmp_path / "matter-server.sh"
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o755)
    return path


async def settle(condition, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def test_only_a_local_controller_is_ours_to_start() -> None:
    assert is_local("ws://127.0.0.1:5580/ws")
    assert is_local("ws://localhost:5580/ws")
    assert not is_local("ws://192.168.0.5:5580/ws")


async def test_a_crashed_server_is_restarted(tmp_path) -> None:
    counter = tmp_path / "starts"
    script = launcher(tmp_path, f'echo "boot $$"; echo x >> "{counter}"; exit 1')
    process = MatterProcess(script, restart_delay=0.05, max_delay=0.1)
    task = asyncio.create_task(process.run())
    await settle(lambda: counter.exists() and counter.read_text().count("x") >= 3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert process.starts >= 3 and process.gave_up is None


async def test_stop_terminates_a_running_server(tmp_path) -> None:
    script = launcher(tmp_path, "trap 'exit 0' TERM; echo started; sleep 30 & wait")
    process = MatterProcess(script)
    task = asyncio.create_task(process.run())
    await settle(lambda: process.running)
    await process.stop()
    assert not process.running
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_gives_way_to_a_server_already_running(tmp_path) -> None:
    """The systemd unit (or a terminal) has it: connect, do not fight over it."""
    script = launcher(
        tmp_path, 'echo "matter-server is already running against /x" >&2; exit 1'
    )
    process = MatterProcess(script, restart_delay=0.05)
    await asyncio.wait_for(process.run(), timeout=5)
    assert process.starts == 1
    assert "already owns" in (process.gave_up or "")


async def test_a_missing_launcher_is_reported_not_retried(tmp_path) -> None:
    process = MatterProcess(tmp_path / "nope.sh")
    await asyncio.wait_for(process.run(), timeout=2)
    assert process.starts == 0 and "not found" in process.gave_up


async def test_stop_before_start_is_harmless(tmp_path) -> None:
    await MatterProcess(tmp_path / "nope.sh").stop()
    assert os.getpid()  # nothing raised


async def test_extra_environment_reaches_the_launcher(tmp_path) -> None:
    """The Bluetooth adapter travels from config to the launcher as an
    environment variable."""
    seen = tmp_path / "seen"
    script = launcher(
        tmp_path, f'echo "${{MATTER_BLUETOOTH_ADAPTER:-unset}}" > "{seen}"; sleep 30 & wait'
    )
    process = MatterProcess(script, env={"MATTER_BLUETOOTH_ADAPTER": "0"})
    task = asyncio.create_task(process.run())
    await settle(lambda: seen.exists() and seen.read_text().strip() != "")
    assert seen.read_text().strip() == "0"
    await process.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


LAUNCHER = Path(__file__).resolve().parent.parent / "scripts" / "matter-server.sh"


def run_launcher(tmp_path, adapter: str | None) -> str:
    """Run the real launcher against a fake matter-server that echoes its
    arguments, and return them."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake = fake_bin / "matter-server"
    fake.write_text('#!/usr/bin/env bash\necho "$@"\n')
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "MATTER_STORAGE": str(tmp_path / "storage"),
    }
    env.pop("MATTER_BLUETOOTH_ADAPTER", None)
    if adapter is not None:
        env["MATTER_BLUETOOTH_ADAPTER"] = adapter
    done = subprocess.run(
        [str(LAUNCHER)], env=env, capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_launcher_passes_the_bluetooth_adapter_only_when_told(tmp_path) -> None:
    args = run_launcher(tmp_path, adapter=None)
    assert "--storage-path" in args and "--bluetooth-adapter" not in args
    args = run_launcher(tmp_path, adapter="0")
    assert args.endswith("--bluetooth-adapter 0")


def test_colored_server_lines_are_cleaned_and_classified() -> None:
    """matter-server colors its output even into a pipe; the color codes hid
    every INFO line (they were demoted to DEBUG) while errors still showed."""
    raw = (
        "2026-08-30 01:16:55.248 (MainThread) \x1b[32mINFO\x1b[0m "
        "[matter_server.server.device_controller] Loaded 1 nodes from stored configuration\n"
    )
    line = clean(raw)
    assert line == "INFO [matter_server.server.device_controller] Loaded 1 nodes from stored configuration"
    assert _level(line) == logging.INFO
    assert _level(clean("2026-08-30 01:16:55.2 (Dummy-2) \x1b[31mCHIP_ERROR\x1b[0m [chip.native.DIS] x")) == logging.WARNING
    assert _level(clean("[1788073819.15][3894:3894] CHIP:DL: Wrote settings")) == logging.DEBUG
    assert _level(clean("2026-08-30 01:16:55.2 (MainThread) INFO [chip.storage] Loading")) == logging.DEBUG
