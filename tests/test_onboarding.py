from house_agent.onboarding import (
    DEFERRED, DONE, IN_PROGRESS, NOT_STARTED, OFFERED, setup_line,
)


def test_first_run_offers_setup_once_and_records_it() -> None:
    line, status = setup_line(NOT_STARTED, first_run=True, new_conversation=True)
    assert line and "brand new" in line and "later" in line
    assert status == OFFERED
    # Same chat, next turn: keep going, no state change.
    line, status = setup_line(OFFERED, first_run=False, new_conversation=False)
    assert "in progress" in line and status is None


def test_moving_on_means_later_and_later_means_learn_as_you_go() -> None:
    line, status = setup_line(OFFERED, first_run=False, new_conversation=True)
    assert "learn as you go" in line and status == DEFERRED
    line, status = setup_line(DEFERRED, first_run=False, new_conversation=True)
    assert "never bring" in line and "edit" in line and status is None


def test_finished_setup_still_allows_editing_on_request() -> None:
    line, status = setup_line(DONE, first_run=False, new_conversation=True)
    assert "edit" in line and status is None
    line, _ = setup_line(NOT_STARTED, first_run=False, new_conversation=True)
    assert "edit" in line
    line, _ = setup_line(NOT_STARTED, first_run=True, new_conversation=False)
    assert "edit" in line
    line, _ = setup_line(IN_PROGRESS, first_run=False, new_conversation=False)
    assert "NEVER repeat an answered step" in line


def test_stale_in_progress_never_nags_a_new_chat() -> None:
    # The old chat ended mid-flow with things learned: setup is done.
    line, status = setup_line(
        IN_PROGRESS, first_run=False, new_conversation=True, has_notes=True
    )
    assert status == DONE and "Never offer" in line and "edit" in line
    # Nothing was learned: treat it as "later" and learn as you go.
    line, status = setup_line(
        IN_PROGRESS, first_run=False, new_conversation=True, has_notes=False
    )
    assert status == DEFERRED and "learn as you go" in line
