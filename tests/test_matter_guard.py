"""The guard that keeps matter-server's pairing list from vanishing."""

import importlib.util
import json
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "scripts" / "matter-storage-guard.py"
spec = importlib.util.spec_from_file_location("matter_storage_guard", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def storage_with(tmp_path, nodes: dict | None, chip: bool = True) -> Path:
    folder = tmp_path / "matter"
    folder.mkdir(exist_ok=True)
    data = {"vendor_info": {}}
    if nodes is not None:
        data["nodes"] = nodes
        data["last_node_id"] = max((int(k) for k in nodes), default=0)
    (folder / "7589431116316592504.json").write_text(json.dumps(data))
    if chip:
        (folder / "chip.json").write_text('{"sdk-config": {"f/1/n": "keys"}}')
    return folder


def nodes_in(folder: Path) -> list[str]:
    return list(json.loads((folder / "7589431116316592504.json").read_text()).get("nodes", {}))


def test_an_empty_list_is_never_snapshotted(tmp_path) -> None:
    folder = storage_with(tmp_path, nodes=None)
    guard.snapshot(folder)
    assert guard.snapshots(folder) == []
    # Nor when the nodes dict exists but is empty.
    folder = storage_with(tmp_path, nodes={})
    guard.snapshot(folder)
    assert guard.snapshots(folder) == []


def test_a_lost_pairing_list_is_restored_from_the_snapshot(tmp_path) -> None:
    """The exact failure: paired one evening, gone at the next start."""
    folder = storage_with(tmp_path, nodes={"17": {"node_id": 17}})
    guard.snapshot(folder)
    assert len(guard.snapshots(folder)) == 1

    storage_with(tmp_path, nodes=None)  # the file matter-server found at boot
    assert nodes_in(folder) == []
    guard.restore_if_empty(folder)
    assert nodes_in(folder) == ["17"]
    # The emptied file is kept aside rather than destroyed.
    assert list(folder.glob("*.emptied-*"))


def test_a_live_list_is_left_alone(tmp_path) -> None:
    folder = storage_with(tmp_path, nodes={"17": {"node_id": 17}})
    guard.snapshot(folder)
    storage_with(tmp_path, nodes={"18": {"node_id": 18}})
    guard.restore_if_empty(folder)
    assert nodes_in(folder) == ["18"]


def test_a_deliberately_emptied_list_is_not_restored(tmp_path) -> None:
    """Removing the last device on purpose (to re-pair it) leaves an empty
    list with last_node_id still set; the guard must not bring it back."""
    folder = storage_with(tmp_path, nodes={"17": {"node_id": 17}})
    guard.snapshot(folder)
    (folder / "7589431116316592504.json").write_text(
        json.dumps({"vendor_info": {}, "nodes": {}, "last_node_id": 17})
    )
    guard.restore_if_empty(folder)
    assert nodes_in(folder) == []
    assert not list(folder.glob("*.emptied-*"))


def test_fabric_keys_are_restored_only_when_missing(tmp_path) -> None:
    folder = storage_with(tmp_path, nodes={"17": {"node_id": 17}})
    guard.snapshot(folder)

    # chip.json gone with the list: the fabric itself comes back.
    storage_with(tmp_path, nodes=None, chip=False)
    guard.restore_if_empty(folder)
    assert "keys" in (folder / "chip.json").read_text()

    # A chip.json that is present is never replaced under a running fabric,
    # even when the list is restored around it.
    storage_with(tmp_path, nodes=None)
    (folder / "chip.json").write_text('{"live": true}')
    guard.restore_if_empty(folder)
    assert (folder / "chip.json").read_text() == '{"live": true}'
    assert nodes_in(folder) == ["17"]


def test_only_the_newest_snapshots_are_kept(tmp_path, monkeypatch) -> None:
    folder = storage_with(tmp_path, nodes={"17": {"node_id": 17}})
    stamps = iter(range(guard.KEEP + 5))
    monkeypatch.setattr(guard.time, "strftime", lambda *_: f"2026{next(stamps):04d}")
    for _ in range(guard.KEEP + 5):
        guard.snapshot(folder)
    kept = guard.snapshots(folder)
    assert len(kept) == guard.KEEP
    assert kept[-1].name == f"2026{guard.KEEP + 4:04d}"


def test_the_newest_snapshot_with_devices_wins(tmp_path, monkeypatch) -> None:
    folder = storage_with(tmp_path, nodes={"17": {"node_id": 17}})
    monkeypatch.setattr(guard.time, "strftime", lambda *_: "0001")
    guard.snapshot(folder)
    storage_with(tmp_path, nodes={"17": {"node_id": 17}, "18": {"node_id": 18}})
    monkeypatch.setattr(guard.time, "strftime", lambda *_: "0002")
    guard.snapshot(folder)
    storage_with(tmp_path, nodes=None)
    guard.restore_if_empty(folder)
    assert nodes_in(folder) == ["17", "18"]


def test_a_missing_folder_is_not_an_error(tmp_path) -> None:
    assert guard.main(["guard", "restore-if-empty", str(tmp_path / "nope")]) == 0
    assert guard.main(["guard", "bogus", str(tmp_path)]) == 2
