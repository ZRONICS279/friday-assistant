from pathlib import Path

from house_agent.config import load_config


def test_default_config_uses_local_endpoints(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")

    assert config.llm.base_url == "http://127.0.0.1:11434/v1"
    assert config.llm.model == "house-qwen"
    assert config.speech.base_url == "http://127.0.0.1:8001/v1"
    assert config.text_to_speech.base_url == "http://127.0.0.1:8880/v1"
    assert config.text_to_speech.voice == "bf_isabella"
    assert config.catalog.exact_episode_playback_enabled is False
    assert config.tv_devices == ()


def test_loads_tv_devices(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [[roku.devices]]
        id = "den"
        name = "Den TV"
        host = "192.168.0.125"
        """
    )

    config = load_config(path)

    assert config.tv_devices[0].id == "den"
    assert config.tv_devices[0].port == 8060


def test_loads_roku_ip_from_dotenv_without_gateway(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    (tmp_path / ".env").write_text(
        "ROKU_IP_ADDRESS=192.168.0.177\nROKU_GATEWAY_ADDRESS=192.168.0.1\n"
    )

    config = load_config(config_path)

    assert len(config.tv_devices) == 1
    assert config.tv_devices[0].host == "192.168.0.177"
    assert config.tv_devices[0].host != "192.168.0.1"


def test_enables_exact_episode_playback_from_dotenv(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    (tmp_path / ".env").write_text("EXACT_EPISODE_PLAYBACK_ENABLED=true\n")

    config = load_config(config_path)

    assert config.catalog.exact_episode_playback_enabled is True


def test_loads_roku_input_aliases_from_dotenv(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    (tmp_path / ".env").write_text(
        "ROKU_INPUT_XBOX=InputHDMI2\n"
        "ROKU_INPUT_SWITCH=InputHDMI4\n"
        "ROKU_INPUT_TV=Home\n"
    )

    config = load_config(config_path)

    assert dict(config.roku_input_aliases) == {
        "switch": "InputHDMI4",
        "tv": "Home",
        "xbox": "InputHDMI2",
    }


def test_prefers_tmdb_read_access_token(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    (tmp_path / ".env").write_text(
        "TMDB_API_TOKEN=legacy-key\nTMDB_READ_ACCESS_TOKEN=read-token\n"
    )

    config = load_config(config_path)

    assert config.catalog.tmdb_token == "read-token"


def test_loads_text_to_speech_voice_and_speed_from_dotenv(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    (tmp_path / ".env").write_text("TTS_VOICE=bf_alice\nTTS_SPEED=1.1\n")

    config = load_config(config_path)

    assert config.text_to_speech.voice == "bf_alice"
    assert config.text_to_speech.speed == 1.1


def test_complex_request_gating() -> None:
    from house_agent.llm import LocalAgent

    assert not LocalAgent._complex_request("turn off the bedroom lamp")
    assert not LocalAgent._complex_request("dim the kitchen light to 20")
    assert LocalAgent._complex_request(
        "find something like the thing we watched last week but shorter"
    )
    assert LocalAgent._complex_request(
        "recommend a movie my wife hasn't seen that is similar to Knives Out"
    )


def test_compact_trace_clips_large_results() -> None:
    from house_agent.llm import _compact_trace

    trace = [{"tool": "content_play", "arguments": {"title": "Loki"}, "result": {"blob": "x" * 5000}}]
    compact = _compact_trace(trace)
    assert compact[0]["tool"] == "content_play"
    assert compact[0]["arguments"] == {"title": "Loki"}
    assert len(compact[0]["result"]) <= 1502 and compact[0]["result"].endswith("…")


def test_weather_defaults_to_no_location_and_imperial_units(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")

    assert config.weather.latitude is None
    assert config.weather.units == "imperial"
    assert config.weather.alerts_enabled is True


def test_weather_location_loads_from_dotenv(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    (tmp_path / ".env").write_text(
        "HOUSE_LATITUDE=39.7392\nHOUSE_LONGITUDE=-104.9903\nWEATHER_UNITS=metric\n"
    )

    config = load_config(config_path)

    assert config.weather.latitude == 39.7392
    assert config.weather.longitude == -104.9903
    assert config.weather.units == "metric"


def test_unparseable_weather_coordinates_are_dropped_not_crashed(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    (tmp_path / ".env").write_text("HOUSE_LATITUDE=somewhere\n")

    assert load_config(config_path).weather.latitude is None
