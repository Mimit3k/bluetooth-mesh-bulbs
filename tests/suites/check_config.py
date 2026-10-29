"""Offline tests for mesh/bulb configuration export and import."""

# ruff: noqa: E402  (the path bootstrap must run before imports)

import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "src", ROOT / "phase0"):
    sys.path.insert(0, str(_path))

import meshcore


def add(state, unicast, name, uuid, key_byte, models=(0x1000, 0x1300, 0x1303, 0x1307)):
    state.add_device(
        unicast,
        name=name,
        uuid=uuid,
        address=f"AA:BB:CC:DD:EE:{unicast:02X}",
        device_key=bytes([key_byte]) * 16,
        bound_models=list(models),
    )


def make_master(root: Path):
    state = meshcore.State(root / "master.json")
    add(state, 0x0002, "Desk", "11" * 16, 0x11)
    add(state, 0x0006, "Lamp", "22" * 16, 0x22)
    state.data["seq"] = 1234
    state.data["next_unicast"] = 0x0009
    state.save()
    return state


def assert_raises(fn, contains):
    try:
        fn()
    except meshcore.MeshCoreError as exc:
        assert contains in str(exc), str(exc)
        return
    raise AssertionError(f"expected MeshCoreError containing {contains!r}")


root = Path(tempfile.mkdtemp(prefix="bulbctl-config-test-"))
master = make_master(root)

# Whole-mesh export is private, versioned and contains both bulbs.
mesh_file = root / "mesh.json"
summary = meshcore.export_config(master, mesh_file, kind="mesh")
assert "whole mesh" in summary
# POSIX mode bits are meaningless on Windows: os.stat reports 0o666 for any
# writable file, and meshcore relies on the user profile's NTFS ACLs instead.
if os.name != "nt":
    assert stat.S_IMODE(mesh_file.stat().st_mode) == 0o600, oct(
        stat.S_IMODE(mesh_file.stat().st_mode)
    )
raw = json.loads(mesh_file.read_text())
assert raw["format"] == "bulbctl-config"
assert raw["version"] == 1
assert raw["kind"] == "mesh"
assert set(raw["devices"]) == {"0x0002", "0x0006"}
assert raw["mesh"]["netkey"] == master.data["netkey"]
assert raw["mesh"]["seq"] == 1234
package = meshcore.read_config(mesh_file)
assert "2 bulbs" in meshcore.describe_config(package)
assert meshcore.same_mesh(master, package)

# A fresh computer adopts the keys and all bulbs.
fresh = meshcore.State(root / "fresh.json")
assert not meshcore.same_mesh(fresh, package)
result = meshcore.apply_config(fresh, package)
assert "imported" in result
assert fresh.data["netkey"] == master.data["netkey"]
assert fresh.data["appkey"] == master.data["appkey"]
assert fresh.data["seq"] == 1234
assert fresh.data["next_unicast"] == 0x0009
assert set(fresh.devices) == {"0x0002", "0x0006"}
assert fresh.device(0x0002)["device_key"] == "11" * 16

# A single-bulb export is self-contained (mesh keys + one device).
bulb_file = root / "desk.json"
meshcore.export_config(master, bulb_file, kind="bulb", unicast=0x0002)
bulb_package = meshcore.read_config(bulb_file)
assert bulb_package["kind"] == "bulb"
assert set(bulb_package["devices"]) == {"0x0002"}
assert "Desk" in meshcore.describe_config(bulb_package)
bulb_only = meshcore.State(root / "bulb-only.json")
meshcore.apply_config(bulb_only, bulb_package)
assert set(bulb_only.devices) == {"0x0002"}
assert bulb_only.data["netkey"] == master.data["netkey"]

# Merging the same mesh adds a missing bulb and keeps existing ones.
merged = meshcore.State(root / "merged.json")
merged.data = copy.deepcopy(master.data)
merged.data["devices"].pop("0x0002")
merged.data["seq"] = 2000
merged.save()
result = meshcore.apply_config(merged, bulb_package, replace=False)
assert "1 added, 0 updated" in result, result
assert set(merged.devices) == {"0x0002", "0x0006"}
assert merged.device(0x0006)["name"] == "Lamp"
result = meshcore.apply_config(merged, bulb_package, replace=False)
assert "0 added, 1 updated" in result, result
assert set(merged.devices) == {"0x0002", "0x0006"}
assert merged.data["seq"] == 2000

# Merge advances SEQ to the imported cursor, never backwards.
merged.data["seq"] = 10
merged.save()
meshcore.apply_config(merged, bulb_package, replace=False)
assert merged.data["seq"] == 1234

# Replacing the same mesh drops bulbs not present in the file.
extra = meshcore.State(root / "extra.json")
extra.data = copy.deepcopy(master.data)
extra.save()
add(extra, 0x000A, "Extra", "33" * 16, 0x33)
assert len(extra.devices) == 3
result = meshcore.apply_config(extra, bulb_package, replace=True)
assert "replaced" in result
assert set(extra.devices) == {"0x0002"}
assert extra.data["seq"] == 1234

# A different mesh is refused unless explicitly replaced.
other = meshcore.State(root / "other.json")
add(other, 0x0002, "Other", "44" * 16, 0x44)
other_seq = other.data["seq"]
other_netkey = other.data["netkey"]
assert not meshcore.same_mesh(other, package)
assert_raises(
    lambda: meshcore.apply_config(other, package),
    "different mesh",
)
assert other.data["netkey"] == other_netkey
assert set(other.devices) == {"0x0002"}
result = meshcore.apply_config(other, package, replace=True)
assert "replaced" in result
assert other.data["netkey"] == master.data["netkey"]
assert set(other.devices) == {"0x0002", "0x0006"}

# An address conflict with a different bulb is refused without partial changes.
conflict = meshcore.State(root / "conflict.json")
conflict.data = copy.deepcopy(master.data)
conflict.data["devices"]["0x0002"] = {
    **conflict.data["devices"]["0x0002"],
    "name": "Different",
    "uuid": "99" * 16,
}
conflict.save()
before = copy.deepcopy(conflict.data["devices"])
assert_raises(
    lambda: meshcore.apply_config(conflict, bulb_package),
    "different UUID",
)
assert conflict.data["devices"] == before

# A failed state save rolls the in-memory state back too.
rollback = meshcore.State(root / "rollback.json")
rollback.data = copy.deepcopy(master.data)
rollback.save()
before_rollback = copy.deepcopy(rollback.data)
original_save = rollback.save


def broken_save():
    raise OSError("disk full")


rollback.save = broken_save
try:
    meshcore.apply_config(rollback, bulb_package)
except OSError as exc:
    assert "disk full" in str(exc)
else:
    raise AssertionError("expected the save failure to propagate")
finally:
    rollback.save = original_save
assert rollback.data == before_rollback

# Moving the same UUID to a new address updates the old entry in place.
moved = meshcore.State(root / "moved.json")
moved.data = copy.deepcopy(master.data)
moved.data["devices"]["0x000A"] = moved.data["devices"].pop("0x0002")
moved.save()
meshcore.apply_config(moved, bulb_package)
assert set(moved.devices) == {"0x0006", "0x0002"}
assert moved.device(0x0002)["uuid"] == "11" * 16

# Export refuses to overwrite the live state file.
assert_raises(
    lambda: meshcore.export_config(master, master.path, kind="mesh"),
    "live state",
)

# Bad files fail clearly.
for name, text, needle in (
    ("bad-json.json", "{", "not valid JSON"),
    ("bad-format.json", json.dumps({"format": "nope"}), "not a bulbctl-config"),
    (
        "bad-version.json",
        json.dumps({"format": "bulbctl-config", "version": 99}),
        "unsupported configuration version",
    ),
    (
        "bad-kind.json",
        json.dumps(
            {
                "format": "bulbctl-config",
                "version": 1,
                "kind": "nope",
                "mesh": raw["mesh"],
                "devices": raw["devices"],
            }
        ),
        "kind must be",
    ),
):
    path = root / name
    path.write_text(text)
    assert_raises(lambda p=path: meshcore.read_config(p), needle)

# A bulb file with two bulbs is rejected.
two = copy.deepcopy(raw)
two["kind"] = "bulb"
path = root / "two-bulbs.json"
path.write_text(json.dumps(two))
assert_raises(lambda: meshcore.read_config(path), "must contain one bulb")

# CLI round trip on a temporary state.
cli = str(ROOT / "bulbctl.py")
cli_state = root / "cli.json"
shutil.copy(master.path, cli_state)
cli_export = root / "cli-export.json"
cmd = [
    sys.executable,
    cli,
    "--state",
    str(cli_state),
    "export",
    "--all",
    str(cli_export),
]
out = subprocess.run(cmd, check=True, capture_output=True, text=True)
assert "exported whole mesh" in out.stdout, out.stdout
assert cli_export.exists()

cmd = [
    sys.executable,
    cli,
    "--state",
    str(cli_state),
    "export",
    "--device",
    "Desk",
    str(root / "cli-bulb.json"),
]
out = subprocess.run(cmd, check=True, capture_output=True, text=True)
assert "bulb 'Desk'" in out.stdout, out.stdout

# Import into a new state via the CLI, non-interactive.
cmd = [
    sys.executable,
    cli,
    "--state",
    str(root / "cli-target.json"),
    "import",
    str(mesh_file),
    "--yes",
]
out = subprocess.run(cmd, check=True, capture_output=True, text=True)
assert "imported whole mesh" in out.stdout, out.stdout
assert (root / "cli-target.json").exists()

# CLI refuses a different mesh without --replace.
mismatch = meshcore.State(root / "mismatch.json")
add(mismatch, 0x0002, "Mismatch", "55" * 16, 0x55)
mismatch.release()
cmd = [
    sys.executable,
    cli,
    "--state",
    str(mismatch.path),
    "import",
    str(mesh_file),
    "--yes",
]
out = subprocess.run(cmd, capture_output=True, text=True)
assert out.returncode == 2 and "different mesh" in out.stdout + out.stderr
mismatch.release()

master.release()
print("configuration export/import tests passed")
