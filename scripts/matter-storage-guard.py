#!/usr/bin/env python3
"""Keep matter-server's pairing list from silently disappearing.

matter-server keeps the list of paired devices in <storage>/<fabric-id>.json,
with a single .backup that is overwritten on every save. If that file is ever
missing or empty when the server starts, it starts with no devices and logs
nothing above DEBUG -- every light and plug looks unpaired, though the devices
themselves still trust this controller. That is what happened here on
2026-08-30: node 17 (the bedroom lamp) was paired one evening and gone the
next morning.

This guard runs around the server:

    matter-storage-guard.py snapshot <storage>
        Copy the fabric file(s) and chip.json into <storage>/snapshots/<time>/
        -- but only when the fabric file lists at least one node, so an empty
        state can never overwrite a good snapshot. Keeps the newest KEEP.

    matter-storage-guard.py restore-if-empty <storage>
        If no fabric file lists any node, put back the newest snapshot that
        does -- unless the fabric file still carries last_node_id, which
        means it was not reset: every device was removed on purpose (to
        re-pair it, say), and the guard must not bring them back. chip.json
        (the fabric's own keys) is restored only if it is missing: a live
        chip.json must never be replaced under a running fabric.

Stdlib only, so it runs on whatever python3 the machine has.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

KEEP = 20
SNAPSHOT_DIR = "snapshots"


def log(message: str) -> None:
    print(f"matter-storage-guard: {message}", file=sys.stderr, flush=True)


def fabric_files(folder: Path) -> list[Path]:
    """The per-fabric storage files: <digits>.json, never chip.json."""
    return sorted(
        path for path in folder.glob("*.json") if path.stem.isdigit() and path.is_file()
    )


def node_count(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    nodes = data.get("nodes") if isinstance(data, dict) else None
    return len([v for v in (nodes or {}).values() if v]) if isinstance(nodes, dict) else 0


def nodes_in(folder: Path) -> int:
    return sum(node_count(path) for path in fabric_files(folder))


def ever_paired(path: Path) -> bool:
    """A fabric file that has commissioned anything carries last_node_id, and
    removing devices on purpose leaves it there (matter-server only drops the
    node entry). A file without it did not empty itself: it was reset or
    replaced, and that is what the snapshot is for."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("last_node_id"))


def snapshots(storage: Path) -> list[Path]:
    root = storage / SNAPSHOT_DIR
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def snapshot(storage: Path) -> int:
    count = nodes_in(storage)
    if count == 0:
        log("no paired nodes in storage; not snapshotting an empty state")
        return 0
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = storage / SNAPSHOT_DIR / stamp
    target.mkdir(parents=True, exist_ok=True)
    for path in fabric_files(storage):
        shutil.copy2(path, target / path.name)
    chip = storage / "chip.json"
    if chip.is_file():
        shutil.copy2(chip, target / chip.name)
    for old in snapshots(storage)[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    log(f"snapshot {stamp}: {count} paired node(s) saved")
    return 0


def restore_if_empty(storage: Path) -> int:
    live = nodes_in(storage)
    if live:
        log(f"storage lists {live} paired node(s); nothing to restore")
        return 0
    if any(ever_paired(path) for path in fabric_files(storage)):
        log(
            "storage lists no paired nodes but the fabric file is intact "
            "(its devices were removed on purpose); not restoring them"
        )
        return 0
    for candidate in reversed(snapshots(storage)):
        count = nodes_in(candidate)
        if count == 0:
            continue
        for path in fabric_files(candidate):
            stale = storage / path.name
            if stale.exists():
                stale.replace(storage / f"{path.name}.emptied-{int(time.time())}")
            shutil.copy2(path, stale)
        chip = storage / "chip.json"
        if not chip.is_file() and (candidate / "chip.json").is_file():
            shutil.copy2(candidate / "chip.json", chip)
            log("chip.json was missing; restored the fabric keys from the snapshot")
        log(
            f"storage listed no paired nodes; restored {count} node(s) from "
            f"snapshot {candidate.name}"
        )
        return 0
    log("storage lists no paired nodes and no snapshot has any; starting empty")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("snapshot", "restore-if-empty"):
        print(__doc__, file=sys.stderr)
        return 2
    storage = Path(argv[2]).expanduser()
    if not storage.is_dir():
        log(f"storage folder {storage} does not exist yet; nothing to do")
        return 0
    return snapshot(storage) if argv[1] == "snapshot" else restore_if_empty(storage)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
