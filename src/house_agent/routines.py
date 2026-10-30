from __future__ import annotations

import asyncio
from typing import Any

from .roku import RokuClient, normalize_key


MAX_ROUTINE_ACTIONS = 40
MAX_WAIT_MS = 5_000


def validate_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not actions or len(actions) > MAX_ROUTINE_ACTIONS:
        raise ValueError(f"A routine must contain 1 to {MAX_ROUTINE_ACTIONS} actions")
    validated: list[dict[str, Any]] = []
    for index, raw in enumerate(actions):
        if not isinstance(raw, dict):
            raise ValueError(f"Routine action {index + 1} must be an object")
        action_type = raw.get("type")
        if action_type == "key":
            validated.append({"type": "key", "key": normalize_key(str(raw.get("key", "")))})
        elif action_type == "text":
            text = str(raw.get("text", ""))
            if not text or len(text) > 256:
                raise ValueError(f"Routine text action {index + 1} is empty or too long")
            validated.append({"type": "text", "text": text})
        elif action_type == "wait":
            milliseconds = int(raw.get("milliseconds", 0))
            if not 0 <= milliseconds <= MAX_WAIT_MS:
                raise ValueError(
                    f"Routine wait action {index + 1} must be between 0 and {MAX_WAIT_MS} ms"
                )
            validated.append({"type": "wait", "milliseconds": milliseconds})
        elif action_type == "launch":
            app = str(raw.get("app", "")).strip()
            if not app or len(app) > 100:
                raise ValueError(f"Routine launch action {index + 1} needs an app name or ID")
            validated.append({"type": "launch", "app": app})
        else:
            raise ValueError(f"Unsupported routine action type at position {index + 1}")
    return validated


async def execute_routine(client: RokuClient, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated = validate_actions(actions)
    trace: list[dict[str, Any]] = []
    for action in validated:
        if action["type"] == "key":
            await client.keypress(action["key"])
        elif action["type"] == "text":
            await client.type_text(action["text"])
        elif action["type"] == "wait":
            await asyncio.sleep(action["milliseconds"] / 1_000)
        elif action["type"] == "launch":
            app = await client.find_app(action["app"])
            await client.launch(app.id)
            action = {**action, "app_id": app.id, "resolved_name": app.name}
        trace.append(action)
    return trace
