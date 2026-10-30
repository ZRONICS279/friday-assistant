from pathlib import Path

import pytest

from house_agent.cli import build_parser, resolve_device


def test_direct_host_target() -> None:
    args = build_parser().parse_args(["--host", "192.168.0.150", "status"])

    device = resolve_device(args)

    assert device.host == "192.168.0.150"
    assert device.port == 8060


def test_uses_only_configured_device(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
        [[roku.devices]]
        id = "bedroom"
        name = "Bedroom TV"
        host = "192.168.0.151"
        """
    )
    args = build_parser().parse_args(["--config", str(config), "apps"])

    assert resolve_device(args).id == "bedroom"


def test_requires_selection_for_multiple_configured_devices(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
        [[roku.devices]]
        id = "one"
        host = "192.168.0.151"
        [[roku.devices]]
        id = "two"
        host = "192.168.0.152"
        """
    )
    args = build_parser().parse_args(["--config", str(config), "status"])

    with pytest.raises(ValueError, match="--device"):
        resolve_device(args)
