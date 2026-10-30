from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS routines (
                    name TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    actions_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watch_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    title TEXT NOT NULL,
                    season INTEGER,
                    episode INTEGER,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages (conversation_id, id);
                CREATE INDEX IF NOT EXISTS watch_history_lookup_idx
                    ON watch_history (profile, provider, title);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    provider TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS content_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    source_url TEXT,
                    source TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE(title, provider, media_type)
                );
                CREATE INDEX IF NOT EXISTS content_links_lookup_idx
                    ON content_links (provider, title);
                CREATE TABLE IF NOT EXISTS provider_resolvers (
                    provider TEXT PRIMARY KEY,
                    verified INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS content_preferences (
                    profile TEXT NOT NULL,
                    title TEXT NOT NULL COLLATE NOCASE,
                    media_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (profile, title, media_type)
                );
                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    title TEXT NOT NULL COLLATE NOCASE,
                    media_type TEXT NOT NULL,
                    catalog TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    provider TEXT,
                    status TEXT NOT NULL DEFAULT 'suggested',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS recommendations_profile_idx
                    ON recommendations (profile, id);
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    through_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profile_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS profile_notes_profile_idx
                    ON profile_notes (profile, id);
                CREATE TABLE IF NOT EXISTS smart_devices (
                    key TEXT PRIMARY KEY,
                    backend TEXT NOT NULL,
                    name TEXT,
                    room TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tv_credentials (
                    device_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voiceprints (
                    profile TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL,
                    samples INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voiceprint_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    seconds REAL NOT NULL,
                    scheme INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS voiceprint_samples_profile
                    ON voiceprint_samples (profile);
                CREATE TABLE IF NOT EXISTS conversation_speakers (
                    conversation_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    profile TEXT,
                    confidence REAL,
                    embedding BLOB,
                    speech_seconds REAL NOT NULL DEFAULT 0,
                    asked INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    schedule_json TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    profile TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    last_result TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS automations_due_idx
                    ON automations (enabled, next_run_at);
                CREATE TABLE IF NOT EXISTS fades (
                    device_id TEXT PRIMARY KEY,
                    plan_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS list_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_name TEXT NOT NULL,
                    item TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    added_by TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS list_items_list_idx
                    ON list_items (list_name, done, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT,
                    location TEXT,
                    notes TEXT,
                    profile TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_start_idx ON events (starts_at);
                CREATE TABLE IF NOT EXISTS external_events (
                    key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    title TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT,
                    all_day INTEGER NOT NULL DEFAULT 0,
                    location TEXT,
                    notes TEXT,
                    synced_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS external_events_start_idx
                    ON external_events (starts_at);
                CREATE TABLE IF NOT EXISTS recipes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    url TEXT,
                    recipe_json TEXT NOT NULL,
                    saved_by TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'note',
                    title TEXT NOT NULL,
                    body TEXT,
                    url TEXT,
                    created_at TEXT NOT NULL,
                    read_at TEXT
                );
                CREATE TABLE IF NOT EXISTS food_favorites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    usual_order TEXT,
                    note TEXT,
                    added_by TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(recommendations)")
            }
            if "genres_json" not in columns:
                connection.execute(
                    "ALTER TABLE recommendations "
                    "ADD COLUMN genres_json TEXT NOT NULL DEFAULT '[]'"
                )
            speaker_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(conversation_speakers)")
            }
            if "pending_name" not in speaker_columns:
                connection.execute(
                    "ALTER TABLE conversation_speakers ADD COLUMN pending_name TEXT"
                )
            note_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(profile_notes)")
            }
            if "kind" not in note_columns:
                # 'taste' is what someone likes; 'style' is how they want to be
                # spoken to. Both are things a person told us, so they share a
                # table, but only one of them shapes recommendations.
                connection.execute(
                    "ALTER TABLE profile_notes ADD COLUMN kind TEXT NOT NULL DEFAULT 'taste'"
                )
            print_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(voiceprints)")
            }
            if "scheme" not in print_columns:
                # Voiceprints made before the extractor changed are scheme 1.
                # They cannot be compared with new ones, so they are labelled
                # rather than silently scored against a different scale.
                connection.execute(
                    "ALTER TABLE voiceprints ADD COLUMN scheme INTEGER NOT NULL DEFAULT 1"
                )
            if "seconds" not in print_columns:
                connection.execute(
                    "ALTER TABLE voiceprints ADD COLUMN seconds REAL NOT NULL DEFAULT 0"
                )
            # Existing single-vector prints become the first sample in their
            # own bank, so nobody loses an enrollment to this change.
            if not connection.execute("SELECT 1 FROM voiceprint_samples LIMIT 1").fetchone():
                connection.execute(
                    """
                    INSERT INTO voiceprint_samples
                        (profile, embedding, seconds, scheme, created_at)
                    SELECT profile, embedding, 0, scheme, updated_at FROM voiceprints
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def save_routine(
        self, name: str, device_id: str, description: str, actions: list[dict[str, Any]]
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO routines (name, device_id, description, actions_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    device_id = excluded.device_id,
                    description = excluded.description,
                    actions_json = excluded.actions_json,
                    updated_at = excluded.updated_at
                """,
                (name, device_id, description, json.dumps(actions), _now()),
            )

    def list_routines(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT name, device_id, description, actions_json, updated_at "
                "FROM routines ORDER BY name"
            ).fetchall()
        return [
            {
                "name": row["name"],
                "device_id": row["device_id"],
                "description": row["description"],
                "actions": json.loads(row["actions_json"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def get_routine(self, name: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT name, device_id, description, actions_json, updated_at "
                "FROM routines WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "device_id": row["device_id"],
            "description": row["description"],
            "actions": json.loads(row["actions_json"]),
            "updated_at": row["updated_at"],
        }

    def record_watch(
        self,
        profile: str,
        provider: str,
        title: str,
        status: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO watch_history
                    (profile, provider, title, season, episode, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (profile, provider, title, season, episode, status, _now()),
            )

    def find_watch_history(
        self, profile: str, provider: str | None = None, title: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["profile = ?"]
        params: list[Any] = [profile]
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if title:
            clauses.append("lower(title) = lower(?)")
            params.append(title)
        query = (
            "SELECT profile, provider, title, season, episode, status, updated_at "
            f"FROM watch_history WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 100"
        )
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_preference(
        self,
        profile: str,
        title: str,
        media_type: str,
        status: str,
        *,
        reason: str = "",
        source: str = "user",
    ) -> dict[str, Any]:
        if status not in {"liked", "disliked", "soft_no", "deferred"}:
            raise ValueError(f"Unsupported preference status: {status}")
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO content_preferences
                    (profile, title, media_type, status, reason, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile, title, media_type) DO UPDATE SET
                    status = excluded.status,
                    reason = excluded.reason,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (profile, title, media_type, status, reason, source, now),
            )
            recommendation_status = {
                "liked": "accepted",
                "disliked": "rejected",
                "soft_no": "soft_no",
                "deferred": "deferred",
            }[status]
            connection.execute(
                """
                UPDATE recommendations SET status = ?, updated_at = ?
                WHERE id = (
                    SELECT id FROM recommendations
                    WHERE profile = ? AND lower(title) = lower(?)
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (recommendation_status, now, profile, title),
            )
        preferences = self.list_preferences(profile, title=title)
        return preferences[0]

    def list_preferences(
        self, profile: str, *, title: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["profile = ?"]
        params: list[Any] = [profile]
        if title:
            clauses.append("lower(title) = lower(?)")
            params.append(title)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT profile, title, media_type, status, reason, source, updated_at "
                f"FROM content_preferences WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # How long each reaction keeps a title out of new recommendations.
    # liked/disliked are permanent; the rest are graded by how the user said no.
    DEFER_DAYS = 7  # "not right now", "maybe later"
    SOFT_NO_DAYS = 21  # "ehh, something else"
    IGNORED_WAIT_RECS = 10  # suggested, never mentioned: this many newer titles first
    ASKED_WAIT_RECS = 5  # asked about it but gave no verdict

    def recommendation_exclusions(self, profile: str) -> set[str]:
        now = datetime.now(UTC)
        deferred_since = (now - timedelta(days=self.DEFER_DAYS)).isoformat()
        soft_no_since = (now - timedelta(days=self.SOFT_NO_DAYS)).isoformat()
        with self._lock, self._connect() as connection:
            preference_rows = connection.execute(
                """
                SELECT title FROM content_preferences
                WHERE profile = ? AND (
                    status IN ('liked', 'disliked')
                    OR (status = 'deferred' AND updated_at >= ?)
                    OR (status = 'soft_no' AND updated_at >= ?)
                )
                """,
                (profile, deferred_since, soft_no_since),
            ).fetchall()
            history_rows = connection.execute(
                """
                SELECT DISTINCT title FROM watch_history
                WHERE profile = ? AND status IN ('started', 'watched')
                """,
                (profile,),
            ).fetchall()
            suggestion_rows = connection.execute(
                "SELECT id, title, status FROM recommendations WHERE profile = ? ORDER BY id",
                (profile,),
            ).fetchall()

        excluded = {
            str(row["title"]).casefold()
            for row in [*preference_rows, *history_rows]
        }

        # A title that was surfaced and drew no verdict stays out until enough
        # newer suggestions have gone by, so lists rotate instead of repeating.
        latest: dict[str, Any] = {}
        for row in suggestion_rows:
            latest[str(row["title"]).casefold()] = row
        all_ids = [row["id"] for row in suggestion_rows]
        for key, row in latest.items():
            if row["status"] == "asked":
                wait = self.ASKED_WAIT_RECS
            elif row["status"] == "suggested":
                wait = self.IGNORED_WAIT_RECS
            else:
                continue
            newer = sum(1 for identifier in all_ids if identifier > row["id"])
            if newer < wait:
                excluded.add(key)
        return excluded

    def genre_affinity(self, profile: str) -> dict[str, Any]:
        """Per-genre taste learned from feedback on past recommendations.

        Returns {"scores": {genre: -1..1}, "signals": n} where n is how many
        judged titles contributed — the caller scales trust by it.
        """
        weights = {"liked": 1.0, "disliked": -1.0, "soft_no": -0.4, "deferred": -0.1}
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.status, p.source, r.genres_json FROM content_preferences p
                JOIN recommendations r ON r.id = (
                    SELECT id FROM recommendations
                    WHERE profile = p.profile AND lower(title) = lower(p.title)
                    ORDER BY id DESC LIMIT 1
                )
                WHERE p.profile = ?
                """,
                (profile,),
            ).fetchall()
        totals: dict[str, float] = {}
        signals = 0
        for row in rows:
            weight = weights.get(str(row["status"]))
            genres = json.loads(row["genres_json"] or "[]")
            if weight is None or not genres:
                continue
            if str(row["source"]) == "playback_request" and weight > 0:
                weight *= 0.5  # pressing play is a hint, not a review
            signals += 1
            for genre in genres:
                key = str(genre).casefold()
                totals[key] = totals.get(key, 0.0) + weight
        scores = {
            genre: max(-1.0, min(1.0, total / 3.0)) for genre, total in totals.items()
        }
        return {"scores": scores, "signals": signals}

    def mark_recommendation_asked(self, profile: str, title: str) -> bool:
        """Record that the user asked about a suggested title without judging it."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recommendations SET status = 'asked', updated_at = ?
                WHERE id = (
                    SELECT id FROM recommendations
                    WHERE profile = ? AND lower(title) = lower(?)
                        AND status IN ('suggested', 'asked')
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (_now(), profile, title),
            )
            return cursor.rowcount > 0

    def record_recommendations(
        self,
        profile: str,
        candidates: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> None:
        now = _now()
        rows = []
        for candidate in candidates:
            offerings = candidate.get("offerings") or []
            rows.append(
                (
                    profile,
                    str(candidate["title"]),
                    str(candidate["media_type"]),
                    str(candidate["catalog"]),
                    str(candidate["catalog_id"]),
                    str(offerings[0]["provider"]) if offerings else None,
                    json.dumps(context),
                    json.dumps(candidate.get("genres") or []),
                    now,
                    now,
                )
            )
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO recommendations
                    (profile, title, media_type, catalog, catalog_id, provider,
                     context_json, genres_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def list_recommendations(
        self, profile: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT title, media_type, catalog, catalog_id, provider, status,
                       context_json, created_at, updated_at
                FROM recommendations WHERE profile = ? ORDER BY id DESC LIMIT ?
                """,
                (profile, limit),
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["context"] = json.loads(item.pop("context_json"))
        return result

    def new_conversation_id(self) -> str:
        return str(uuid.uuid4())

    # -- settings and profile notes ---------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, _now()),
            )

    def save_smart_device_alias(
        self, key: str, backend: str, name: str | None, room: str | None
    ) -> None:
        """User-given name and room for a smart device; None leaves the
        existing value alone so renames and room moves are independent."""
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO smart_devices (key, backend, name, room, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    name = COALESCE(excluded.name, smart_devices.name),
                    room = COALESCE(excluded.room, smart_devices.room),
                    updated_at = excluded.updated_at
                """,
                (key, backend, name, room, _now()),
            )

    def delete_smart_device_alias(self, key: str) -> None:
        """Forget a device's name and room, when the device itself has been
        unpaired: otherwise it would be reported as lost."""
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM smart_devices WHERE key = ?", (key,))

    def save_tv_credentials(
        self, device_id: str, platform: str, data: dict[str, Any]
    ) -> None:
        """What a TV handed back when it was paired: an LG client key, a
        Samsung token, a Vizio auth token. Replaces any earlier pairing,
        because re-pairing a TV invalidates the previous credential."""
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tv_credentials (device_id, platform, data_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    platform = excluded.platform,
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at
                """,
                (device_id, platform, json.dumps(data), _now()),
            )

    def tv_credentials(self, device_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM tv_credentials WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        if row is None:
            return {}
        try:
            stored = json.loads(row["data_json"])
        except (TypeError, ValueError):
            return {}
        return stored if isinstance(stored, dict) else {}

    def forget_tv_credentials(self, device_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM tv_credentials WHERE device_id = ?", (device_id,)
            )

    def smart_devices_known(self) -> list[dict[str, str | None]]:
        """Every device this house has named, whether or not a backend still lists it."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key, backend, name, room FROM smart_devices ORDER BY name"
            ).fetchall()
        return [
            {
                "key": str(row["key"]),
                "backend": str(row["backend"]),
                "name": str(row["name"]) if row["name"] else None,
                "room": str(row["room"]) if row["room"] else None,
            }
            for row in rows
        ]

    def smart_device_aliases(self) -> dict[str, dict[str, str | None]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key, name, room FROM smart_devices"
            ).fetchall()
        return {
            str(row["key"]): {
                "name": str(row["name"]) if row["name"] else None,
                "room": str(row["room"]) if row["room"] else None,
            }
            for row in rows
        }

    def add_profile_note(
        self, profile: str, note: str, kind: str = "taste"
    ) -> dict[str, Any]:
        note = " ".join(note.split())
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO profile_notes (profile, note, kind, created_at) "
                "VALUES (?, ?, ?, ?)",
                (profile, note, kind, _now()),
            )
        return {"id": int(cursor.lastrowid), "profile": profile, "note": note, "kind": kind}

    def list_style_preferences(self, profiles: list[str], limit: int = 20) -> list[dict[str, Any]]:
        """How these people have asked to be spoken to, oldest first. Standing
        rules written on the People page ride along (kind 'rule'): both are
        instructions that belong in every turn."""
        if not profiles:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, profile, note, kind FROM profile_notes "
                "WHERE kind IN ('style', 'rule') "
                f"AND profile IN ({','.join('?' * len(profiles))}) ORDER BY id LIMIT ?",
                (*profiles, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_notes(self, profile: str, limit: int = 200) -> list[dict[str, Any]]:
        """Everything written down about one person, every kind, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, profile, note, kind, created_at FROM profile_notes "
                "WHERE profile = ? ORDER BY id LIMIT ?",
                (profile, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def note_counts(self) -> dict[str, dict[str, int]]:
        """How many notes of each kind each person has."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT profile, kind, COUNT(*) AS n FROM profile_notes GROUP BY profile, kind"
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(str(row["profile"]), {})[str(row["kind"])] = int(row["n"])
        return counts

    def update_profile_note(self, note_id: int, note: str) -> dict[str, Any] | None:
        note = " ".join(note.split())
        if not note:
            return None
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE profile_notes SET note = ? WHERE id = ?", (note, note_id)
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT id, profile, note, kind, created_at FROM profile_notes WHERE id = ?",
                (note_id,),
            ).fetchone()
        return dict(row) if row else None

    def has_profile_notes(self) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM profile_notes LIMIT 1").fetchone() is not None

    def list_profile_notes(self, profile: str, limit: int = 40) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, note FROM profile_notes WHERE profile = ? AND kind = 'taste' "
                "ORDER BY id LIMIT ?",
                (profile, limit),
            ).fetchall()
        return [{"id": int(row["id"]), "note": str(row["note"])} for row in rows]

    def delete_profile_note(self, note_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM profile_notes WHERE id = ?", (note_id,))
        return cursor.rowcount > 0

    def known_profiles(self) -> list[str]:
        """Everyone the house knows: enrolled voices and anyone with notes."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT profile FROM voiceprints UNION SELECT profile FROM profile_notes "
                "ORDER BY profile"
            ).fetchall()
        return [str(row["profile"]) for row in rows]

    # -- voice identity ---------------------------------------------------------

    def list_voiceprints(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT profile, embedding, samples, seconds, scheme, updated_at "
                "FROM voiceprints ORDER BY profile"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_voiceprint_samples(self, scheme: int) -> dict[str, list[Any]]:
        """Every banked voiceprint, grouped by person, for the current scheme."""
        import numpy as np

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT profile, embedding FROM voiceprint_samples WHERE scheme = ? "
                "ORDER BY profile, id",
                (scheme,),
            ).fetchall()
        banked: dict[str, list[Any]] = {}
        for row in rows:
            banked.setdefault(str(row["profile"]), []).append(
                np.frombuffer(row["embedding"], dtype=np.float32)
            )
        return banked

    def bank_voiceprint(
        self,
        profile: str,
        embedding: Any,
        *,
        seconds: float,
        scheme: int,
        cap_seconds: float,
    ) -> dict[str, Any]:
        """Add one more recording of a voice, keeping up to cap_seconds of them.

        A single averaged vector is thin evidence -- it blurs the range a real
        voice covers across moods, distances and times of day. Keeping the
        recordings themselves lets a later utterance be compared with the
        closest one instead of with an average of all of them, and the bank
        grows on its own as the person keeps talking.
        """
        import numpy as np

        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector = vector / norm
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT scheme FROM voiceprints WHERE profile = ?", (profile,)
            ).fetchone()
            if row is not None and int(row["scheme"]) != scheme:
                # Built a different way: the old recordings cannot be compared
                # with the new ones, so this is a fresh start for that person.
                connection.execute(
                    "DELETE FROM voiceprint_samples WHERE profile = ?", (profile,)
                )
            connection.execute(
                """
                INSERT INTO voiceprint_samples
                    (profile, embedding, seconds, scheme, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (profile, vector.astype(np.float32).tobytes(), max(0.0, seconds), scheme, _now()),
            )
            connection.execute(
                "DELETE FROM voiceprint_samples WHERE scheme != ? AND profile = ?",
                (scheme, profile),
            )
            # Trim the oldest recordings until the bank fits the cap, always
            # keeping the newest one.
            kept = connection.execute(
                "SELECT id, seconds FROM voiceprint_samples WHERE profile = ? "
                "ORDER BY id DESC",
                (profile,),
            ).fetchall()
            total = 0.0
            keep_ids = []
            for item in kept:
                if keep_ids and total + float(item["seconds"]) > cap_seconds:
                    break
                total += float(item["seconds"])
                keep_ids.append(int(item["id"]))
            connection.execute(
                "DELETE FROM voiceprint_samples WHERE profile = ? AND id NOT IN "
                f"({','.join('?' * len(keep_ids))})",
                (profile, *keep_ids),
            )
            rows = connection.execute(
                "SELECT embedding FROM voiceprint_samples WHERE profile = ?", (profile,)
            ).fetchall()
            mean = np.mean(
                [np.frombuffer(item["embedding"], dtype=np.float32) for item in rows], axis=0
            )
            mean_norm = float(np.linalg.norm(mean))
            if mean_norm > 0:
                mean = mean / mean_norm
            connection.execute(
                """
                INSERT INTO voiceprints
                    (profile, embedding, samples, seconds, scheme, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile) DO UPDATE SET
                    embedding = excluded.embedding,
                    samples = excluded.samples,
                    seconds = excluded.seconds,
                    scheme = excluded.scheme,
                    updated_at = excluded.updated_at
                """,
                (
                    profile,
                    mean.astype(np.float32).tobytes(),
                    len(rows),
                    round(total, 2),
                    scheme,
                    _now(),
                ),
            )
        return {"profile": profile, "samples": len(rows), "seconds": round(total, 2)}

    def delete_voiceprint(self, profile: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM voiceprint_samples WHERE profile = ?", (profile,)
            )
            cursor = connection.execute(
                "DELETE FROM voiceprints WHERE profile = ?", (profile,)
            )
        return cursor.rowcount > 0

    def get_conversation_speaker(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_speakers WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_conversation_speaker(
        self,
        conversation_id: str,
        status: str,
        profile: str | None,
        confidence: float | None,
        embedding: bytes | None,
        speech_seconds: float,
        *,
        asked: bool | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT asked FROM conversation_speakers WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            asked_value = int(asked) if asked is not None else (int(row["asked"]) if row else 0)
            connection.execute(
                """
                INSERT INTO conversation_speakers
                    (conversation_id, status, profile, confidence, embedding,
                     speech_seconds, asked, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    status = excluded.status,
                    profile = excluded.profile,
                    confidence = excluded.confidence,
                    embedding = excluded.embedding,
                    speech_seconds = excluded.speech_seconds,
                    asked = excluded.asked,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id, status, profile, confidence, embedding,
                    speech_seconds, asked_value, _now(),
                ),
            )

    def set_speaker_pending_name(self, conversation_id: str, name: str | None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE conversation_speakers SET pending_name = ?, updated_at = ? "
                "WHERE conversation_id = ?",
                (name, _now(), conversation_id),
            )

    def mark_speaker_asked(self, conversation_id: str) -> int:
        """Count one delivery of the name question and return the new total.

        A count rather than a flag: the instruction going out is not proof the
        model acted on it, so the caller needs to know how many times it has
        tried before it gives up.
        """
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_speakers
                    (conversation_id, status, asked, updated_at)
                VALUES (?, 'listening', 1, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    asked = conversation_speakers.asked + 1,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, _now()),
            )
            row = connection.execute(
                "SELECT asked FROM conversation_speakers WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row["asked"]) if row else 1

    def seed_subscriptions(self, providers: list[tuple[str, str]]) -> None:
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO subscriptions
                    (provider, display_name, enabled, updated_at)
                VALUES (?, ?, 1, ?)
                """,
                [(provider, display_name, _now()) for provider, display_name in providers],
            )

    def replace_subscriptions(self, providers: list[tuple[str, str]]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("UPDATE subscriptions SET enabled = 0, updated_at = ?", (_now(),))
            connection.executemany(
                """
                INSERT INTO subscriptions (provider, display_name, enabled, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    display_name = excluded.display_name,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                [(provider, display_name, _now()) for provider, display_name in providers],
            )

    def list_subscriptions(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE enabled = 1" if enabled_only else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT provider, display_name, enabled, updated_at FROM subscriptions {where} "
                "ORDER BY display_name"
            ).fetchall()
        return [
            {
                "provider": row["provider"],
                "display_name": row["display_name"],
                "enabled": bool(row["enabled"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def save_content_link(
        self,
        *,
        title: str,
        provider: str,
        media_type: str,
        app_name: str,
        content_id: str,
        source_url: str | None,
        source: str,
        verified: bool,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO content_links
                    (title, provider, media_type, app_name, content_id, source_url, source,
                     verified, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(title, provider, media_type) DO UPDATE SET
                    app_name = excluded.app_name,
                    content_id = excluded.content_id,
                    source_url = excluded.source_url,
                    source = excluded.source,
                    verified = excluded.verified,
                    updated_at = excluded.updated_at
                """,
                (
                    title,
                    provider,
                    media_type,
                    app_name,
                    content_id,
                    source_url,
                    source,
                    int(verified),
                    _now(),
                ),
            )
        link = self.find_content_link(title, provider, media_type)
        assert link is not None
        return link

    def find_content_link(
        self, title: str, provider: str, media_type: str | None = None
    ) -> dict[str, Any] | None:
        clauses = ["lower(title) = lower(?)", "provider = ?"]
        params: list[Any] = [title, provider]
        if media_type:
            clauses.append("media_type = ?")
            params.append(media_type)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, title, provider, media_type, app_name, content_id, source_url, "
                "source, verified, updated_at FROM content_links WHERE "
                + " AND ".join(clauses)
                + " ORDER BY verified DESC, id DESC LIMIT 1",
                params,
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["verified"] = bool(result["verified"])
        return result

    def list_content_links(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, title, provider, media_type, app_name, content_id, source_url, "
                "source, verified, updated_at FROM content_links ORDER BY title, provider"
            ).fetchall()
        links = [dict(row) for row in rows]
        for link in links:
            link["verified"] = bool(link["verified"])
        return links

    def set_provider_resolver_verified(
        self, provider: str, verified: bool, notes: str = ""
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_resolvers (provider, verified, notes, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    verified = excluded.verified,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (provider, int(verified), notes, _now()),
            )
        return self.get_provider_resolver(provider)

    def get_provider_resolver(self, provider: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT provider, verified, notes, updated_at FROM provider_resolvers "
                "WHERE provider = ?",
                (provider,),
            ).fetchone()
        if row is None:
            return {"provider": provider, "verified": False, "notes": "", "updated_at": None}
        result = dict(row)
        result["verified"] = bool(result["verified"])
        return result

    def list_provider_resolvers(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT provider, verified, notes, updated_at FROM provider_resolvers "
                "ORDER BY provider"
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["verified"] = bool(item["verified"])
        return result

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages
                    (conversation_id, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, role, content, json.dumps(metadata or {}), _now()),
            )

    def get_history(self, conversation_id: str, limit: int = 60) -> dict[str, Any]:
        """Compacted view of a conversation: a running summary of everything
        old, plus the raw messages that arrived after it."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT summary, through_id FROM conversation_summaries "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            through_id = int(row["through_id"]) if row else 0
            rows = connection.execute(
                """
                SELECT id, role, content FROM messages
                WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT ?
                """,
                (conversation_id, through_id, limit),
            ).fetchall()
        return {
            "summary": str(row["summary"]) if row else None,
            "messages": [
                {"id": int(r["id"]), "role": str(r["role"]), "content": str(r["content"])}
                for r in rows
            ],
        }

    def save_conversation_summary(
        self, conversation_id: str, summary: str, through_id: int
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_summaries
                    (conversation_id, summary, through_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    summary = excluded.summary,
                    through_id = excluded.through_id,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, summary, through_id, _now()),
            )

    def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        """Past chats, newest activity first: what was first asked (the
        title), when it started and last moved, how many turns, and who the
        chat was locked to."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.conversation_id AS id,
                       MIN(m.id) AS first_id,
                       MIN(m.created_at) AS started_at,
                       MAX(m.created_at) AS updated_at,
                       COUNT(*) AS messages,
                       s.profile AS profile,
                       s.status AS speaker_status
                FROM messages m
                LEFT JOIN conversation_speakers s ON s.conversation_id = m.conversation_id
                GROUP BY m.conversation_id
                ORDER BY MAX(m.id) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            result = []
            for row in rows:
                first = connection.execute(
                    "SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' "
                    "ORDER BY id LIMIT 1",
                    (row["id"],),
                ).fetchone()
                title = " ".join(str(first["content"]).split()) if first else "Untitled chat"
                result.append(
                    {
                        "id": str(row["id"]),
                        "title": title[:120],
                        "started_at": str(row["started_at"]),
                        "updated_at": str(row["updated_at"]),
                        "messages": int(row["messages"]),
                        "profile": row["profile"],
                        "speaker_status": row["speaker_status"],
                    }
                )
        return result

    def conversation_messages(self, conversation_id: str, limit: int = 500) -> list[dict[str, Any]]:
        """A whole chat as it was shown, oldest first, tool traces included."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, metadata_json, created_at FROM messages
                WHERE conversation_id = ? ORDER BY id LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except ValueError:
                metadata = {}
            result.append(
                {
                    "id": int(row["id"]),
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "created_at": str(row["created_at"]),
                    "trace": metadata.get("trace") or [],
                }
            )
        return result

    def recent_traces(self, conversation_id: str, limit: int = 6) -> list[list[dict[str, Any]]]:
        """The tool traces of the last few replies in a chat, oldest first.

        get_history hands the model role and content only, so a turn that did
        its work through tools leaves nothing behind but the word "Done." —
        and the next turn's "turn them off" has no antecedent to resolve.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT metadata_json FROM messages
                WHERE conversation_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT ?
                """,
                (conversation_id, max(1, limit)),
            ).fetchall()
        traces: list[list[dict[str, Any]]] = []
        for row in reversed(rows):
            try:
                trace = (json.loads(row["metadata_json"] or "{}") or {}).get("trace")
            except ValueError:
                continue
            if isinstance(trace, list) and trace:
                traces.append(trace)
        return traces

    def delete_conversation(self, conversation_id: str) -> bool:
        """Forget a chat completely: its messages, running summary, the voice
        it was locked to, and the board of messages Friday put on its screen."""
        with self._lock, self._connect() as connection:
            removed = connection.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
            ).rowcount
            connection.execute(
                "DELETE FROM conversation_summaries WHERE conversation_id = ?", (conversation_id,)
            )
            connection.execute(
                "DELETE FROM conversation_speakers WHERE conversation_id = ?", (conversation_id,)
            )
            connection.execute("DELETE FROM inbox WHERE conversation_id = ?", (conversation_id,))
        return removed > 0

    def get_messages(self, conversation_id: str, limit: int = 30) -> list[dict[str, str]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content FROM messages
                    WHERE conversation_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (conversation_id, limit),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    # -- automations, lists, recipes -------------------------------------------

    # Fades under way, so a restart (or a power cut) picks them up where the
    # clock says they should be instead of leaving a lamp half-dimmed.
    def save_fade(self, device_id: str, plan: dict[str, Any], started_at: str, ends_at: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO fades (device_id, plan_json, started_at, ends_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    plan_json = excluded.plan_json, started_at = excluded.started_at,
                    ends_at = excluded.ends_at
                """,
                (device_id, json.dumps(plan), started_at, ends_at),
            )

    def delete_fade(self, device_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM fades WHERE device_id = ?", (device_id,))

    def list_fades(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM fades ORDER BY started_at").fetchall()
        return [
            {
                "device_id": row["device_id"],
                "plan": json.loads(row["plan_json"]),
                "started_at": row["started_at"],
                "ends_at": row["ends_at"],
            }
            for row in rows
        ]

    def add_automation(
        self,
        name: str,
        schedule: dict[str, Any],
        action: dict[str, Any],
        next_run_at: str | None,
        profile: str | None = None,
    ) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO automations
                    (name, schedule_json, action_json, profile, enabled,
                     next_run_at, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (name, json.dumps(schedule), json.dumps(action), profile, next_run_at, _now()),
            )
            return int(cursor.lastrowid or 0)

    @staticmethod
    def _automation_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "schedule": json.loads(row["schedule_json"]),
            "action": json.loads(row["action_json"]),
            "profile": row["profile"],
            "enabled": bool(row["enabled"]),
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "last_result": row["last_result"],
            "created_at": row["created_at"],
        }

    def list_automations(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM automations"
                + ("" if include_disabled else " WHERE enabled = 1")
                + " ORDER BY next_run_at IS NULL, next_run_at, id"
            ).fetchall()
        return [self._automation_row(row) for row in rows]

    def get_automation(self, automation_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM automations WHERE id = ?", (automation_id,)
            ).fetchone()
        return self._automation_row(row) if row else None

    def due_automations(self, now_iso: str) -> list[dict[str, Any]]:
        """Enabled automations whose next run is at or before the given
        local-time ISO timestamp. Timestamps are stored as ISO 8601 with a
        fixed offset, so string comparison orders them correctly as long as
        the offset is the same, which it is for one house."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automations
                WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at, id
                """,
                (now_iso,),
            ).fetchall()
        return [self._automation_row(row) for row in rows]

    def mark_automation_run(
        self,
        automation_id: int,
        ran_at: str,
        next_run_at: str | None,
        result: str | None = None,
    ) -> None:
        """Record a run. A None next run disables the automation: one-off
        reminders end here rather than lingering in the list."""
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE automations
                SET last_run_at = ?, next_run_at = ?, last_result = ?,
                    enabled = CASE WHEN ? IS NULL THEN 0 ELSE enabled END
                WHERE id = ?
                """,
                (ran_at, next_run_at, result, next_run_at, automation_id),
            )

    def set_automation_enabled(self, automation_id: int, enabled: bool, next_run_at: str | None = None) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE automations SET enabled = ?, next_run_at = COALESCE(?, next_run_at) WHERE id = ?",
                (1 if enabled else 0, next_run_at, automation_id),
            )
            return cursor.rowcount > 0

    def delete_automation(self, automation_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM automations WHERE id = ?", (automation_id,)
            )
            return cursor.rowcount > 0

    def add_list_items(
        self, list_name: str, items: list[str], added_by: str | None = None
    ) -> list[str]:
        """Add items, skipping any already on the list (case-insensitive).
        Returns the items actually added."""
        added: list[str] = []
        with self._lock, self._connect() as connection:
            existing = {
                str(row["item"]).casefold()
                for row in connection.execute(
                    "SELECT item FROM list_items WHERE list_name = ? AND done = 0",
                    (list_name,),
                )
            }
            for item in items:
                item = " ".join(item.split()).strip()
                if not item or item.casefold() in existing:
                    continue
                connection.execute(
                    "INSERT INTO list_items (list_name, item, added_by, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (list_name, item, added_by, _now()),
                )
                existing.add(item.casefold())
                added.append(item)
        return added

    def list_items(self, list_name: str, include_done: bool = False) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, item, done, added_by, created_at FROM list_items "
                "WHERE list_name = ?" + ("" if include_done else " AND done = 0") + " ORDER BY id",
                (list_name,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "item": row["item"],
                "done": bool(row["done"]),
                "added_by": row["added_by"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_names(self) -> list[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT list_name, COUNT(*) AS n FROM list_items WHERE done = 0 "
                "GROUP BY list_name ORDER BY list_name"
            ).fetchall()
        return [str(row["list_name"]) for row in rows]

    def remove_list_items(self, list_name: str, items: list[str]) -> list[str]:
        """Check off items by name. A given name matches an item that equals
        it or contains it ('eggs' clears 'a dozen eggs'), case-insensitively.
        Returns the item texts removed."""
        removed: list[str] = []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, item FROM list_items WHERE list_name = ? AND done = 0",
                (list_name,),
            ).fetchall()
            for wanted in items:
                needle = " ".join(wanted.split()).casefold()
                if not needle:
                    continue
                for row in rows:
                    text = str(row["item"])
                    if text in removed:
                        continue
                    if text.casefold() == needle or needle in text.casefold():
                        connection.execute(
                            "UPDATE list_items SET done = 1 WHERE id = ?", (row["id"],)
                        )
                        removed.append(text)
        return removed

    # -- inbox: what Friday puts on the screen instead of saying --------------
    # Links, instructions, codes: things that are useless read aloud. One-way;
    # the badge in the web client counts what nobody has opened yet.
    @staticmethod
    def _inbox_row(row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "kind": str(row["kind"]),
            "title": str(row["title"]),
            "body": str(row["body"]) if row["body"] else None,
            "url": str(row["url"]) if row["url"] else None,
            "created_at": str(row["created_at"]),
            "read": bool(row["read_at"]),
            "conversation_id": str(row["conversation_id"]) if row["conversation_id"] else None,
        }

    def add_inbox_message(
        self,
        title: str,
        body: str | None = None,
        url: str | None = None,
        kind: str = "note",
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        title = " ".join(str(title).split())[:120]
        if not title:
            raise ValueError("an inbox message needs a title")
        body = str(body).strip() or None if body else None
        url = str(url).strip() or None if url else None
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO inbox (conversation_id, kind, title, body, url, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, kind, title, body, url, _now()),
            )
            row = connection.execute(
                "SELECT * FROM inbox WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return self._inbox_row(row)

    # Each chat has its own board: a message belongs to the conversation it
    # was sent in, and a fresh chat starts empty. A message with no
    # conversation (none are sent that way today) would show everywhere.
    @staticmethod
    def _inbox_scope(conversation_id: str | None) -> tuple[str, tuple[Any, ...]]:
        if conversation_id:
            return "(conversation_id = ? OR conversation_id IS NULL)", (conversation_id,)
        return "conversation_id IS NULL", ()

    def update_inbox_message(
        self, message_id: int, url: str | None = None, body: str | None = None
    ) -> dict[str, Any] | None:
        """A message whose link or text changed underneath (a sign-in link
        renewed): same card, same read state, new contents."""
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE inbox SET url = COALESCE(?, url), body = COALESCE(?, body) WHERE id = ?",
                (url, body, int(message_id)),
            )
            row = connection.execute("SELECT * FROM inbox WHERE id = ?", (int(message_id),)).fetchone()
        return self._inbox_row(row) if row else None

    def list_inbox(self, conversation_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """This conversation's messages, newest first."""
        scope, params = self._inbox_scope(conversation_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM inbox WHERE {scope} ORDER BY id DESC LIMIT ?",
                (*params, max(1, int(limit))),
            ).fetchall()
        return [self._inbox_row(row) for row in rows]

    def unread_inbox_count(self, conversation_id: str | None = None) -> int:
        scope, params = self._inbox_scope(conversation_id)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM inbox WHERE read_at IS NULL AND {scope}", params
            ).fetchone()
        return int(row["n"])

    def mark_inbox_read(
        self, conversation_id: str | None = None, ids: list[int] | None = None
    ) -> int:
        """Mark the given messages (or every unread one on this board) read;
        returns how many on the board are still unread."""
        scope, params = self._inbox_scope(conversation_id)
        with self._lock, self._connect() as connection:
            if ids:
                wanted = [int(i) for i in ids]
                connection.execute(
                    f"UPDATE inbox SET read_at = ? WHERE read_at IS NULL AND {scope} "
                    f"AND id IN ({','.join('?' * len(wanted))})",
                    (_now(), *params, *wanted),
                )
            else:
                connection.execute(
                    f"UPDATE inbox SET read_at = ? WHERE read_at IS NULL AND {scope}",
                    (_now(), *params),
                )
        return self.unread_inbox_count(conversation_id)

    # Favorite places to order from, in the household's own words. Only what
    # people said is kept here — never a store id, a menu, or a price from
    # DoorDash, whose terms forbid storing what its CLI returns.
    def add_food_favorite(
        self,
        name: str,
        usual_order: str | None = None,
        note: str | None = None,
        added_by: str | None = None,
    ) -> dict[str, Any]:
        name = " ".join(str(name).split())
        if not name:
            raise ValueError("a favorite needs a name")
        usual_order = " ".join(usual_order.split()) if usual_order else None
        note = " ".join(note.split()) if note else None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM food_favorites WHERE lower(name) = lower(?)", (name,)
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE food_favorites SET name = ?, usual_order = COALESCE(?, usual_order), "
                    "note = COALESCE(?, note) WHERE id = ?",
                    (name, usual_order, note, int(row["id"])),
                )
                favorite_id = int(row["id"])
                updated = True
            else:
                cursor = connection.execute(
                    "INSERT INTO food_favorites (name, usual_order, note, added_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, usual_order, note, added_by, _now()),
                )
                favorite_id = int(cursor.lastrowid)
                updated = False
            saved = connection.execute(
                "SELECT id, name, usual_order, note, added_by FROM food_favorites WHERE id = ?",
                (favorite_id,),
            ).fetchone()
        return {**dict(saved), "updated": updated}

    def list_food_favorites(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, name, usual_order, note, added_by FROM food_favorites ORDER BY lower(name)"
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_food_favorite(self, name: str) -> bool:
        wanted = " ".join(str(name).split()).casefold()
        favorites = self.list_food_favorites()
        exact = [f for f in favorites if str(f["name"]).casefold() == wanted]
        matches = exact or [f for f in favorites if wanted and wanted in str(f["name"]).casefold()]
        if len(matches) != 1:
            return False
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM food_favorites WHERE id = ?", (matches[0]["id"],))
        return cursor.rowcount > 0

    def clear_list(self, list_name: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE list_items SET done = 1 WHERE list_name = ? AND done = 0",
                (list_name,),
            )
            return cursor.rowcount

    def save_recipe(
        self, title: str, url: str | None, recipe: dict[str, Any], saved_by: str | None = None
    ) -> int:
        with self._lock, self._connect() as connection:
            if url:
                row = connection.execute(
                    "SELECT id FROM recipes WHERE url = ?", (url,)
                ).fetchone()
                if row:
                    connection.execute(
                        "UPDATE recipes SET title = ?, recipe_json = ? WHERE id = ?",
                        (title, json.dumps(recipe), row["id"]),
                    )
                    return int(row["id"])
            cursor = connection.execute(
                "INSERT INTO recipes (title, url, recipe_json, saved_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, url, json.dumps(recipe), saved_by, _now()),
            )
            return int(cursor.lastrowid or 0)

    def list_recipes(self, query: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, title, url, recipe_json, saved_by, created_at FROM recipes "
                "ORDER BY id DESC"
            ).fetchall()
        needle = (query or "").casefold().strip()
        found = []
        for row in rows:
            recipe = json.loads(row["recipe_json"])
            haystack = " ".join(
                [str(row["title"])] + [str(item) for item in recipe.get("ingredients", [])]
            ).casefold()
            if needle and not all(word in haystack for word in needle.split()):
                continue
            found.append(
                {
                    "id": int(row["id"]),
                    "title": row["title"],
                    "url": row["url"],
                    "saved_by": row["saved_by"],
                    "created_at": row["created_at"],
                    **recipe,
                }
            )
        return found

    def get_recipe(self, recipe_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, title, url, recipe_json, saved_by FROM recipes WHERE id = ?",
                (recipe_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "title": row["title"],
            "url": row["url"],
            "saved_by": row["saved_by"],
            **json.loads(row["recipe_json"]),
        }

    def delete_recipe(self, recipe_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
            return cursor.rowcount > 0

    # -- calendar ----------------------------------------------------------------

    def add_event(
        self,
        title: str,
        starts_at: str,
        ends_at: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        profile: str | None = None,
    ) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (title, starts_at, ends_at, location, notes, profile, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, starts_at, ends_at, location, notes, profile, _now()),
            )
            return int(cursor.lastrowid or 0)

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "title": row["title"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "location": row["location"],
            "notes": row["notes"],
            "profile": row["profile"],
        }

    def list_events(self, start: str, end: str) -> list[dict[str, Any]]:
        """Events starting in [start, end), ISO strings with the house offset:
        Friday's own plans and everything from subscribed calendars, together,
        in time order. Subscribed ones carry their calendar as source."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE starts_at >= ? AND starts_at < ?",
                (start, end),
            ).fetchall()
            external = connection.execute(
                "SELECT * FROM external_events WHERE starts_at >= ? AND starts_at < ?",
                (start, end),
            ).fetchall()
        merged = [{**self._event_row(row), "source": "friday", "all_day": False} for row in rows]
        merged += [self._external_row(row) for row in external]
        merged.sort(key=lambda item: (item["starts_at"], str(item["id"])))
        return merged

    @staticmethod
    def _external_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["key"],
            "title": row["title"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "location": row["location"],
            "notes": row["notes"],
            "profile": None,
            "source": row["source"],
            "all_day": bool(row["all_day"]),
        }

    def replace_external_events(self, source: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Swap in the latest expansion of one subscribed calendar. Returns
        the keys that are new and the ones that vanished, so heads-ups can be
        added for the former and dropped for the latter."""
        stamp = _now()
        with self._lock, self._connect() as connection:
            before = {
                str(row["key"])
                for row in connection.execute(
                    "SELECT key FROM external_events WHERE source = ?", (source,)
                )
            }
            connection.execute("DELETE FROM external_events WHERE source = ?", (source,))
            after: set[str] = set()
            for event in events:
                after.add(event["key"])
                connection.execute(
                    """
                    INSERT OR REPLACE INTO external_events
                        (key, source, uid, title, starts_at, ends_at, all_day, location, notes, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["key"], source, event["uid"], event["title"], event["starts_at"],
                        event.get("ends_at"), 1 if event.get("all_day") else 0,
                        event.get("location"), event.get("notes"), stamp,
                    ),
                )
            gone = before - after
            for key in gone:
                connection.execute(
                    "DELETE FROM automations WHERE json_extract(action_json, '$.external_key') = ?",
                    (key,),
                )
        return {"added": sorted(after - before), "removed": sorted(gone), "total": len(after)}

    def external_sources(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT source, COUNT(*) AS n, MAX(synced_at) AS synced_at "
                "FROM external_events GROUP BY source ORDER BY source"
            ).fetchall()
        return [
            {"source": row["source"], "events": int(row["n"]), "synced_at": row["synced_at"]}
            for row in rows
        ]

    def has_automation_for_external(self, key: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM automations WHERE json_extract(action_json, '$.external_key') = ? LIMIT 1",
                (key,),
            ).fetchone()
        return row is not None

    def all_events(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY starts_at, id").fetchall()
        return [self._event_row(row) for row in rows]

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event_row(row) if row else None

    def update_event(self, event_id: int, **fields: Any) -> bool:
        """Change a plan in place. Only the named columns move, so a plan can
        be rescheduled without losing its location or notes."""
        allowed = ("title", "starts_at", "ends_at", "location", "notes")
        changes = {key: value for key, value in fields.items() if key in allowed}
        if not changes:
            return False
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE events SET {assignments} WHERE id = ?",
                (*changes.values(), event_id),
            )
            return cursor.rowcount > 0

    def delete_automations_for_event(self, event_id: int) -> int:
        """Drop whatever was scheduled on a plan's behalf — the weather
        heads-up — when the plan moves and the old timing no longer applies."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM automations WHERE json_extract(action_json, '$.event_id') = ?",
                (event_id,),
            )
            return cursor.rowcount

    def delete_event(self, event_id: int) -> bool:
        """Remove an event and any automation set up on its behalf, such as
        the weather heads-up before it."""
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM automations WHERE json_extract(action_json, '$.event_id') = ?",
                (event_id,),
            )
            cursor = connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
            return cursor.rowcount > 0
