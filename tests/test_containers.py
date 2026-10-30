"""house-agent starting its own containers, and stopping them again.

The containers are the half of the house that is not Python: mosquitto and
Zigbee2MQTT for Zigbee, kokoro and whisper for speech. What matters here is
the bookkeeping -- house-agent stops what it started and nothing else -- so
these run against a stand-in `docker` that keeps its "running" list in a file.
"""

from pathlib import Path

from house_agent.config import DockerConfig, load_config
from house_agent.containers import Containers

SERVICES = ("kokoro", "mosquitto", "whisper", "zigbee2mqtt")


def fake_docker(tmp_path: Path, *, running: tuple[str, ...] = (), exit_code: int = 0) -> Path:
    """A `docker compose` that understands ps/config/up/stop against a file."""
    state = tmp_path / "running"
    state.write_text("\n".join(running))
    script = tmp_path / "docker"
    script.write_text(
        f'''#!/usr/bin/env python3
import pathlib, sys
state = pathlib.Path({str(state)!r})
every = {list(SERVICES)!r}
argv = sys.argv[1:]
if argv[:1] == ["compose"]:
    argv = argv[1:]
while argv[:1] == ["-f"]:
    argv = argv[2:]
running = [line for line in state.read_text().split() if line]
if argv[:1] == ["ps"]:
    print("\\n".join(running))
elif argv[:1] == ["config"]:
    print("\\n".join(every))
elif argv[:1] == ["up"] and "--force-recreate" in argv:
    (state.parent / "recreated").write_text(argv[-1])
elif argv[:1] == ["up"]:
    state.write_text("\\n".join(every))
elif argv[:1] == ["stop"]:
    targets = argv[1:]
    state.write_text("\\n".join(s for s in running if s not in targets))
sys.exit({exit_code})
'''
    )
    script.chmod(0o755)
    return script


def compose_files(tmp_path: Path) -> tuple[Path, ...]:
    files = (tmp_path / "docker-compose.zigbee.yml", tmp_path / "docker-compose.tts.yml")
    for path in files:
        path.write_text("services: {}\n")
    return files


def still_running(tmp_path: Path) -> set[str]:
    return {line for line in (tmp_path / "running").read_text().split() if line}


async def test_a_cold_house_is_started_and_then_stopped(tmp_path) -> None:
    docker = fake_docker(tmp_path)
    containers = Containers(compose_files(tmp_path), command=(str(docker), "compose"))

    await containers.start()
    assert containers.started == SERVICES
    assert still_running(tmp_path) == set(SERVICES)
    assert containers.gave_up is None

    await containers.stop()
    assert still_running(tmp_path) == set()


async def test_containers_already_running_are_adopted_and_left_running(tmp_path) -> None:
    """Someone had the stack up before house-agent booted -- Ctrl-C should
    not take their containers down with it."""
    docker = fake_docker(tmp_path, running=SERVICES)
    containers = Containers(compose_files(tmp_path), command=(str(docker), "compose"))

    await containers.start()
    assert containers.started == ()
    assert containers.adopted == SERVICES

    await containers.stop()
    assert still_running(tmp_path) == set(SERVICES)


async def test_only_the_containers_it_started_are_stopped(tmp_path) -> None:
    """The half-up case: mosquitto was already there, the rest were not."""
    docker = fake_docker(tmp_path, running=("mosquitto",))
    containers = Containers(compose_files(tmp_path), command=(str(docker), "compose"))

    await containers.start()
    assert containers.started == ("kokoro", "whisper", "zigbee2mqtt")
    assert containers.adopted == ("mosquitto",)

    await containers.stop()
    assert still_running(tmp_path) == {"mosquitto"}


async def test_no_docker_at_all_does_not_stop_house_agent(tmp_path) -> None:
    """A checkout without Docker still runs; the lights just need the
    containers started some other way."""
    containers = Containers(
        compose_files(tmp_path), command=(str(tmp_path / "there-is-no-docker"),)
    )
    await containers.start()
    assert containers.gave_up == "docker compose is not installed"
    assert containers.started == ()
    await containers.stop()  # and shutting down is still quiet


async def test_a_stopped_docker_daemon_is_reported_not_raised(tmp_path) -> None:
    docker = fake_docker(tmp_path, exit_code=1)
    containers = Containers(compose_files(tmp_path), command=(str(docker), "compose"))
    await containers.start()
    assert containers.gave_up == "the docker daemon is not reachable"
    assert containers.started == ()


async def test_a_missing_compose_file_is_named(tmp_path) -> None:
    docker = fake_docker(tmp_path)
    gone = tmp_path / "docker-compose.gone.yml"
    containers = Containers((gone,), command=(str(docker), "compose"))
    await containers.start()
    assert containers.gave_up is not None and "docker-compose.gone.yml" in containers.gave_up
    assert containers.started == ()


async def test_nothing_happens_when_management_is_off(tmp_path) -> None:
    """[docker] manage = false hands Containers an empty file list."""
    containers = Containers(())
    await containers.start()
    await containers.stop()
    assert containers.started == () and containers.gave_up is None
    assert containers.status()["enabled"] is False


def test_docker_section_is_read_from_the_config_file(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[docker]
manage = true
compose_files = ["docker-compose.zigbee.yml"]
command = ["podman", "compose"]
start_timeout_seconds = 42
"""
    )
    config = load_config(config_path)
    assert config.docker.manage is True
    assert config.docker.command == ("podman", "compose")
    assert config.docker.start_timeout_seconds == 42.0
    # Relative names resolve against the config file, not the shell's cwd.
    assert config.docker.compose_files == (tmp_path / "docker-compose.zigbee.yml",)


def test_docker_management_is_off_until_asked_for() -> None:
    assert DockerConfig().manage is False
    assert [path.name for path in DockerConfig().compose_files] == [
        "docker-compose.zigbee.yml",
        "docker-compose.tts.yml",
    ]


async def test_a_stuck_service_is_recreated_on_its_own(tmp_path) -> None:
    docker = fake_docker(tmp_path, running=SERVICES)
    containers = Containers(compose_files(tmp_path), command=(str(docker), "compose"))
    await containers.start()
    assert await containers.recreate("zigbee2mqtt") is True
    assert (tmp_path / "recreated").read_text() == "zigbee2mqtt"
    assert still_running(tmp_path) == set(SERVICES)  # nothing else was touched


async def test_recreate_is_a_no_op_when_management_is_off(tmp_path) -> None:
    assert await Containers(()).recreate("zigbee2mqtt") is False
