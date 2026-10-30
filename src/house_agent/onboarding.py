"""First-time setup: what the model is told, turn by turn, while a new
household is introducing itself. State lives in the settings table so the
flow survives across turns and chats and never repeats a finished step."""

from __future__ import annotations

SETUP_KEY = "setup_status"
NOT_STARTED = "none"
OFFERED = "offered"  # the flow was put to them; not yet accepted or deferred
IN_PROGRESS = "in_progress"
DEFERRED = "deferred"
DONE = "done"

FLOW = (
    "Setup flow — one question at a time, short and conversational, never a list "
    "of questions, and never repeat a question whose answer is already in profile "
    "notes (taste_memory_get shows them):\n"
    "1. Ask whether they would like to set up some preferences and configuration "
    "now, or do it later and just get into it. If later, call household_setup with "
    "status deferred and carry on normally.\n"
    "2. If now, call household_setup with status in_progress. Ask what kinds of "
    "movies and shows they like and dislike, and a little about themselves. Save "
    "each answer with profile_note_add for their profile — one note per fact, in "
    "their words.\n"
    "3. Ask whether anyone else lives with them. For each person, ask their name "
    "and, if offered, what they like; save it with profile_note_add using that "
    "person's name as the profile. Say their voice will be learned the first time "
    "they talk to you.\n"
    "4. Ask for general household preferences everyone shares (kids in the house, "
    "things to avoid, usual viewing times); save with profile_note_add using the "
    "profile household.\n"
    "5. Call household_setup with status done and say they can change any of this "
    "any time just by telling you."
)

FIRST_RUN = (
    "First-time setup: you are brand new in this home and know nobody's voice yet. "
    "Open by greeting them warmly in one sentence, introducing yourself by name "
    "as Friday, their new house assistant, "
    "then ask their name so you can recognize their voice from now on — mention "
    "they can stay anonymous. When they give a name call voice_enroll; if they "
    "decline call voice_stay_anonymous. Then handle anything else they said, and "
    "then begin the setup flow below.\n" + FLOW
)

CONTINUE = (
    "Preferences setup is in progress. Pick up from the first step whose answer is "
    "not already in this conversation or in profile notes — NEVER repeat an "
    "answered step, and never re-ask whether they want to set up now: that was "
    "already answered. The moment the remaining steps are covered, call "
    "household_setup with status done and stop mentioning setup.\n" + FLOW
)

FINISHED = (
    "First-time setup was completed in an earlier conversation. Never offer or "
    "mention setup unprompted. "
)

DEFERRED_LINE = (
    "Preferences setup was skipped: learn as you go from reactions (taste_feedback) "
    "and from what people say about themselves (profile_note_add), and never bring "
    "setup up unprompted. If they ask to set up or edit preferences, profiles, or the "
    "household, call household_setup with status in_progress and run the setup flow, "
    "skipping anything already known.\n" + FLOW
)

ALWAYS = (
    "At any time, if someone asks to change or edit their preferences or profile, "
    "read the current notes with taste_memory_get, add what changed with "
    "profile_note_add, and remove notes that no longer hold with profile_note_remove."
)


def setup_line(
    status: str, *, first_run: bool, new_conversation: bool, has_notes: bool = False
) -> tuple[str | None, str | None]:
    """The instruction for this turn and the status to record, if any."""
    if status in (NOT_STARTED, "") and first_run and new_conversation:
        return FIRST_RUN, OFFERED
    if status == IN_PROGRESS:
        if new_conversation:
            # Setup must never leak into a fresh chat and nag again: an old
            # chat that ended mid-flow counts as finished if anything was
            # learned, and as "later" if nothing was.
            if has_notes:
                return FINISHED + ALWAYS, DONE
            return DEFERRED_LINE, DEFERRED
        return CONTINUE, None
    if status == OFFERED:
        if new_conversation:
            # They moved on without choosing: that is a "later". From here the
            # house learns as it goes and sets up only when asked.
            return DEFERRED_LINE, DEFERRED
        return CONTINUE, None
    if status == DEFERRED:
        return DEFERRED_LINE, None
    return ALWAYS, None
