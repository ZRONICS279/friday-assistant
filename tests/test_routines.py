import pytest

from house_agent.routines import MAX_ROUTINE_ACTIONS, validate_actions


def test_validates_and_normalizes_actions() -> None:
    actions = validate_actions(
        [
            {"type": "key", "key": "down"},
            {"type": "wait", "milliseconds": 900},
            {"type": "launch", "app": "Netflix"},
        ]
    )

    assert actions[0] == {"type": "key", "key": "Down"}
    assert actions[2]["app"] == "Netflix"


def test_rejects_unbounded_routines() -> None:
    with pytest.raises(ValueError):
        validate_actions([{"type": "key", "key": "Down"}] * (MAX_ROUTINE_ACTIONS + 1))


def test_rejects_long_waits_and_unknown_actions() -> None:
    with pytest.raises(ValueError):
        validate_actions([{"type": "wait", "milliseconds": 10_000}])
    with pytest.raises(ValueError):
        validate_actions([{"type": "open_the_pod_bay_doors"}])

